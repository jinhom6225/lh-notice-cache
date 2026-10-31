#!/usr/bin/env python3
"""Build docs/lh_cache_v1.json — a shared cache of LH lease-notice supply and
schedule data for the LH청약알리미 app.

Data source: 한국토지주택공사 공공데이터 (data.go.kr). Standard library only.

Steps per run:
1. List current notices (types 06/05/39/13, status 공고중/접수중/정정공고중,
   posted within the last 90 days), all pages.
2. Load the previous cache file. For notices that are new, (newly) 정정공고중,
   incomplete, or fetched more than 7 days ago, call the supply-info and
   detail-info APIs. Everything else reuses the previous value (incremental).
3. Error responses (HTTP errors, non-JSON / XML OpenAPI_ServiceResponse,
   resHeader SS_CODE != "Y") keep the previous value and are counted.
4. Notices no longer listed are dropped 30 days after they were last seen.

The raw response blocks are stored unchanged so the app's existing Dart
parsers read them as-is:
  supply   = {"dsList01": [...], "dsList01Nm": [...], ...}   ({} = no blocks)
  schedule = {"dsSplScdl": [...], "dsSplScdlNm": [...]}      ({} = no blocks)
  null     = never fetched successfully (the app falls back to the API)

The API key comes from the DATA_GO_KR_API_KEY environment variable and is
never printed or written; any error text is redacted before logging.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

KST = timezone(timedelta(hours=9))

LIST_URL = "https://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"
SUPPLY_URL = "https://apis.data.go.kr/B552555/lhLeaseNoticeSplInfo1/getLeaseNoticeSplInfo1"
DETAIL_URL = "https://apis.data.go.kr/B552555/lhLeaseNoticeDtlInfo1/getLeaseNoticeDtlInfo1"

SCHEMA = 1
SOURCE = "한국토지주택공사 공공데이터(data.go.kr) — 출처: 한국토지주택공사"
LICENSE = "공공데이터 이용허락범위 제한 없음"

UPP_TYPES = ("06", "05", "39", "13")  # 임대 · 분양 · 신혼희망 · 주거복지
STATUSES = ("공고중", "접수중", "정정공고중")
CORRECTION = "정정공고중"
SUPPLY_ELIGIBLE = frozenset({"05", "06", "39"})  # same rule as the app's isSupplyEligible
CODE_FIELDS = ("UPP_AIS_TP_CD", "CCR_CNNT_SYS_DS_CD", "SPL_INF_TP_CD", "AIS_TP_CD")
# DTL_URL query names used as a fallback when a list row lacks a code
URL_CODE_NAMES = {
    "UPP_AIS_TP_CD": "uppAisTpCd",
    "CCR_CNNT_SYS_DS_CD": "ccrCnntSysDsCd",
    "SPL_INF_TP_CD": "splInfTpCd",
    "AIS_TP_CD": "aisTpCd",
}
SUPPLY_BLOCK = re.compile(r"^dsList\d+(nm)?$", re.IGNORECASE)
SCHEDULE_BLOCK = re.compile(r"^dsSplScdl(nm)?$", re.IGNORECASE)

Transport = Callable[[str, dict], str]


@dataclass(frozen=True)
class Config:
    max_calls: int = 600
    page_size: int = 100
    max_pages: int = 20
    window_days: int = 90
    refresh_days: int = 7
    correction_refresh_hours: int = 24
    drop_days: int = 30
    sleep_seconds: float = 0.3


class ApiError(Exception):
    """A response that must not overwrite the previous value."""


# ---------------------------------------------------------------- utilities


def redact(text: str, key: str) -> str:
    """Remove the key (raw and percent-encoded forms) from any text."""
    if not key:
        return text
    for form in {key, urllib.parse.quote(key, safe=""), urllib.parse.quote_plus(key)}:
        text = text.replace(form, "***")
    return text


def iso(dt: datetime) -> str:
    return dt.astimezone(KST).isoformat(timespec="seconds")


def parse_iso(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)


def parse_lh_date(value) -> datetime | None:
    match = re.search(r"(\d{4})[.\-/]?(\d{2})[.\-/]?(\d{2})", str(value or ""))
    if not match:
        return None
    try:
        return datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=KST)
    except ValueError:
        return None


def http_get(url: str, params: dict, timeout: float = 20.0) -> str:
    """Real transport. The URL contains the key, so it is never logged."""
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"Accept": "application/json", "User-Agent": "lh-notice-cache"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def check_response(text: str):
    """Decode a response; raise ApiError for gateway/LH error responses."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        match = re.search(r"<returnAuthMsg>([^<]*)</returnAuthMsg>", text or "")
        raise ApiError(match[1] if match else "not JSON") from None
    if not isinstance(data, (list, dict)):
        raise ApiError(f"unexpected type {type(data).__name__}")
    blocks = data if isinstance(data, list) else [data]
    for block in blocks:
        if not isinstance(block, dict):
            continue
        header = block.get("resHeader")
        first = header[0] if isinstance(header, list) and header else header
        if isinstance(first, dict) and first.get("SS_CODE") is not None:
            code = str(first["SS_CODE"]).strip()
            if code != "Y":
                raise ApiError(f"SS_CODE {code}")
    return data


def merge_envelope(data) -> dict:
    """[{dsSch:..}, {dsList01:..}, ..] or a dict -> one dict."""
    merged: dict = {}
    for block in data if isinstance(data, list) else [data]:
        if isinstance(block, dict):
            merged.update(block)
    return merged


def pick_blocks(data, pattern: re.Pattern) -> dict:
    return {k: v for k, v in merge_envelope(data).items() if pattern.match(k)}


# ---------------------------------------------------------------- builder


class Builder:
    def __init__(self, key: str, transport: Transport, now: datetime, config: Config,
                 sleep: Callable[[float], None], log: Callable[[str], None]):
        self.key = key
        self.transport = transport
        self.now = now
        self.config = config
        self.sleep = sleep
        self.log = log
        self.calls = 0
        self.stats = {
            "listed": 0, "notices": 0, "refreshed": 0,
            "list_calls": 0, "supply_calls": 0, "schedule_calls": 0,
            "failures": {"list": 0, "supply": 0, "schedule": 0},
            "dropped": 0, "skipped_by_cap": 0,
        }

    # -- transport --------------------------------------------------------

    def call(self, url: str, params: dict):
        if self.calls > 0 and self.config.sleep_seconds > 0:
            self.sleep(self.config.sleep_seconds)
        self.calls += 1
        try:
            text = self.transport(url, {"serviceKey": self.key, **params})
        except urllib.error.HTTPError as e:
            e.close()
            raise ApiError(f"HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(redact(f"{type(e).__name__}: {e}", self.key)) from None
        return check_response(text)

    # -- listing ----------------------------------------------------------

    def list_notices(self) -> tuple[dict, int, int]:
        """Return ({PAN_ID: row}, ok_combos, failed_combos)."""
        start = (self.now.astimezone(KST) - timedelta(days=self.config.window_days)).date()
        rows: dict = {}
        ok = failed = 0
        for upp in UPP_TYPES:
            for status in STATUSES:
                try:
                    for r in self.list_combo(upp, status, start):
                        rows.setdefault(str(r.get("PAN_ID", "")).strip(), r)
                    ok += 1
                except ApiError as e:
                    failed += 1
                    self.stats["failures"]["list"] += 1
                    self.log(f"list {upp}/{status} failed: {redact(str(e), self.key)}")
        rows.pop("", None)
        return rows, ok, failed

    def list_combo(self, upp: str, status: str, start) -> list:
        cfg = self.config
        params = {
            "PG_SZ": cfg.page_size,
            "UPP_AIS_TP_CD": upp,
            "PAN_SS": status,
            "PAN_NT_ST_DT": start.strftime("%Y.%m.%d"),
            "CLSG_DT": f"{self.now.astimezone(KST).year + 1}.12.31",
        }
        collected: list = []
        for page in range(1, cfg.max_pages + 1):
            if self.calls >= cfg.max_calls:
                raise ApiError("call cap reached while listing")
            self.stats["list_calls"] += 1
            data = self.call(LIST_URL, {**params, "PAGE": page})
            page_rows = [r for r in merge_envelope(data).get("dsList") or [] if isinstance(r, dict)]
            collected.extend(page_rows)
            if not page_rows:
                break
            total = _int(page_rows[0].get("ALL_CNT"))
            if total is not None:
                if len(collected) >= total:
                    break
            elif len(page_rows) < cfg.page_size:
                break
        cutoff = datetime.combine(start, datetime.min.time(), tzinfo=KST)
        return [r for r in collected if (parse_lh_date(r.get("PAN_NT_ST_DT")) or cutoff) >= cutoff]

    # -- refresh ----------------------------------------------------------

    def refresh_reason(self, prev: dict | None, status: str) -> str | None:
        if prev is None:
            return "new"
        fetched = parse_iso(prev.get("fetched_at"))
        if fetched is None:
            return "incomplete"
        age = self.now - fetched
        if status == CORRECTION and (
            prev.get("status") != CORRECTION
            or age >= timedelta(hours=self.config.correction_refresh_hours)
        ):
            return "correction"
        if age >= timedelta(days=self.config.refresh_days):
            return "expired"
        return None

    def fetch_part(self, kind: str, url: str, codes: dict, pan_id: str, pattern) -> dict | None:
        params = {"PAN_ID": pan_id, **{k: v for k, v in codes.items() if v}}
        self.stats[f"{kind}_calls"] += 1
        try:
            return pick_blocks(self.call(url, params), pattern)
        except ApiError as e:
            self.stats["failures"][kind] += 1
            self.log(f"{kind} {pan_id} failed: {redact(str(e), self.key)}")
            return None

    def refresh(self, entry: dict, pan_id: str) -> None:
        codes = entry["codes"]
        complete = True
        if codes.get("UPP_AIS_TP_CD") in SUPPLY_ELIGIBLE:
            supply = self.fetch_part("supply", SUPPLY_URL, codes, pan_id, SUPPLY_BLOCK)
            if supply is None:
                complete = False
            else:
                entry["supply"] = supply
        else:
            entry["supply"] = None
        schedule = self.fetch_part("schedule", DETAIL_URL, codes, pan_id, SCHEDULE_BLOCK)
        if schedule is None:
            complete = False
        else:
            entry["schedule"] = schedule
        if complete:
            entry["fetched_at"] = iso(self.now)
        self.stats["refreshed"] += 1

    def calls_needed(self, entry: dict) -> int:
        return 2 if entry["codes"].get("UPP_AIS_TP_CD") in SUPPLY_ELIGIBLE else 1

    # -- whole run --------------------------------------------------------

    def build(self, previous: dict | None) -> tuple[dict, bool]:
        prev_notices = _previous_notices(previous)
        listed, ok_combos, failed_combos = self.list_notices()
        if ok_combos == 0:
            return {}, False
        self.stats["listed"] = len(listed)

        notices: dict = {}
        queue: list = []
        for pan_id, r in listed.items():
            prev = prev_notices.get(pan_id)
            status = str(r.get("PAN_SS", "")).strip()
            entry = {
                "fetched_at": None, "supply": None, "schedule": None,
                **(prev or {}),
                "status": status,
                "codes": _codes(r),
                "last_seen_at": iso(self.now),
            }
            notices[pan_id] = entry
            reason = self.refresh_reason(prev, status)
            if reason:
                queue.append((_PRIORITY[reason], (prev or {}).get("fetched_at") or "", pan_id))

        for _prio, _fetched, pan_id in sorted(queue):
            entry = notices[pan_id]
            if self.calls + self.calls_needed(entry) > self.config.max_calls:
                self.stats["skipped_by_cap"] += 1
                continue
            self.refresh(entry, pan_id)

        drop_before = self.now - timedelta(days=self.config.drop_days)
        for pan_id, prev in prev_notices.items():
            if pan_id in notices:
                continue
            seen = parse_iso(prev.get("last_seen_at"))
            if failed_combos == 0 and (seen is None or seen < drop_before):
                self.stats["dropped"] += 1
                continue
            notices[pan_id] = prev

        self.stats["notices"] = len(notices)
        doc = {
            "schema": SCHEMA,
            "generated_at": iso(self.now),
            "source": SOURCE,
            "license": LICENSE,
            "source_apis": [LIST_URL, SUPPLY_URL, DETAIL_URL],
            "stats": self.stats,
            "notices": dict(sorted(notices.items())),
        }
        return doc, True


_PRIORITY = {"new": 0, "incomplete": 1, "correction": 2, "expired": 3}


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _codes(r: dict) -> dict:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(r.get("DTL_URL") or "")).query)
    codes = {}
    for field in CODE_FIELDS:
        value = str(r.get(field) or "").strip()
        if not value:
            value = (query.get(URL_CODE_NAMES[field]) or [""])[0].strip()
        codes[field] = value
    return codes


def _previous_notices(previous) -> dict:
    if not isinstance(previous, dict) or previous.get("schema") != SCHEMA:
        return {}
    notices = previous.get("notices")
    if not isinstance(notices, dict):
        return {}
    return {k: v for k, v in notices.items() if isinstance(v, dict)}


def build(previous, key: str, transport: Transport, now: datetime, config: Config,
          sleep: Callable[[float], None] = time.sleep,
          log: Callable[[str], None] = lambda _m: None) -> tuple[dict, bool]:
    """Pure entry point used by tests. Returns (document, listing_ok)."""
    return Builder(key, transport, now, config, sleep, log).build(previous)


# ---------------------------------------------------------------- CLI


def load_previous(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"warning: previous cache unreadable, starting fresh ({type(e).__name__})", file=sys.stderr)
        return None


def write_atomic(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".lh_cache_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_key() -> str:
    key = os.environ.get("DATA_GO_KR_API_KEY", "").strip()
    # data.go.kr also issues a percent-encoded ("Encoding") key; urlencode re-encodes it
    return urllib.parse.unquote(key) if "%" in key else key


def main(argv=None, transport: Transport = http_get) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", default="docs/lh_cache_v1.json")
    parser.add_argument("--max-calls", type=int, default=Config.max_calls)
    parser.add_argument("--sleep", type=float, default=Config.sleep_seconds)
    parser.add_argument("--verbose", action="store_true", help="log each failed call (redacted)")
    args = parser.parse_args(argv)

    key = read_key()
    if not key:
        print(
            "ERROR: DATA_GO_KR_API_KEY is not set. Add it as a repository secret "
            "(Settings > Secrets and variables > Actions) or export it locally.",
            file=sys.stderr,
        )
        return 2

    output = Path(args.output)
    config = Config(max_calls=args.max_calls, sleep_seconds=args.sleep)

    def log(message: str) -> None:
        if args.verbose:
            print(redact(message, key), file=sys.stderr)

    doc, listing_ok = build(load_previous(output), key, transport,
                            datetime.now(KST), config, log=log)
    if not listing_ok:
        print("ERROR: every notice-list call failed; previous cache left unchanged.", file=sys.stderr)
        return 1

    stats = doc["stats"]
    attempted = stats["supply_calls"] + stats["schedule_calls"]
    failed = stats["failures"]["supply"] + stats["failures"]["schedule"]
    summary = json.dumps(stats, ensure_ascii=False)
    if attempted > 0 and failed == attempted:
        print(f"ERROR: every supply/detail call failed; cache not updated. stats={summary}", file=sys.stderr)
        return 1

    write_atomic(output, doc)
    print(redact(f"wrote {output} stats={summary}", key))
    return 0


if __name__ == "__main__":
    sys.exit(main())
