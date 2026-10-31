#!/usr/bin/env python3
"""Build docs/lh_cache_v1.json — a shared cache of LH lease-notice supply and
schedule data for the LH청약알리미 app.

Data source: 한국토지주택공사 공공데이터 (data.go.kr). Standard library only.

Steps per run:
1. List current notices (types 06/05/39/13, status 공고중/접수중/정정공고중),
   all pages. The status bounds the range; the date params are as wide as the
   app's own list call.
2. Load the previous cache file. Refresh supply-info and detail-info for notices
   that are new, incomplete, whose status differs from the status they were
   last fetched under, 정정공고중 (every 24 h), with empty blocks (every 24 h),
   or fetched more than 7 days ago. Everything else reuses the previous value.
3. Error responses (HTTP/transport errors, non-JSON / XML
   OpenAPI_ServiceResponse, resHeader SS_CODE != "Y") keep the previous value
   and are counted. A run deadline stops refreshing and writes what is done.
4. Notices no longer listed are dropped 30 days after they were last seen.
5. The file is rewritten only when the notices changed, or as a heartbeat when
   the previous generated_at is 20 h old (the app treats generated_at older
   than 48 h as stale). Unchanged runs leave the file — and its ETag — alone.

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
import http.client
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
    max_pages: int = 20          # per status/type combo, raised up to max_pages_hard by ALL_CNT
    max_pages_hard: int = 60
    refresh_days: int = 7
    recheck_hours: int = 24      # 정정공고중 and empty-block entries
    drop_days: int = 30
    heartbeat_hours: int = 20    # rewrite an unchanged file once its generated_at is this old
    deadline_seconds: float = 1200.0
    fail_ratio: float = 0.8      # exit 1 when this share of detail calls fail ...
    fail_min_count: int = 5      # ... and at least this many failed
    sleep_seconds: float = 0.3


class ApiError(Exception):
    """A response that must not overwrite the previous value."""


class DeadlineReached(Exception):
    """The run deadline passed; stop calling the API."""


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
                 sleep: Callable[[float], None], log: Callable[[str], None],
                 clock: Callable[[], float]):
        self.key = key
        self.transport = transport
        self.now = now
        self.config = config
        self.sleep = sleep
        self.log = log
        self.clock = clock
        self.started = clock()
        self.calls = 0
        self.stats = {
            "listed": 0, "notices": 0, "refreshed": 0,
            "list_calls": 0, "supply_calls": 0, "schedule_calls": 0,
            "failures": {"list": 0, "supply": 0, "schedule": 0},
            "dropped": 0, "skipped_by_cap": 0, "skipped_by_deadline": 0,
        }

    # -- transport --------------------------------------------------------

    def past_deadline(self) -> bool:
        return self.clock() - self.started >= self.config.deadline_seconds

    def call(self, url: str, params: dict):
        if self.past_deadline():
            raise DeadlineReached()
        if self.calls > 0 and self.config.sleep_seconds > 0:
            self.sleep(self.config.sleep_seconds)
        self.calls += 1
        try:
            text = self.transport(url, {"serviceKey": self.key, **params})
        except urllib.error.HTTPError as e:
            e.close()
            raise ApiError(f"HTTP {e.code}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            raise ApiError(redact(f"{type(e).__name__}: {e}", self.key)) from None
        return check_response(text)

    # -- listing ----------------------------------------------------------

    def list_notices(self) -> tuple[dict, int, int]:
        """Return ({PAN_ID: row}, ok_combos, failed_combos)."""
        rows: dict = {}
        ok = failed = 0
        for upp in UPP_TYPES:
            for status in STATUSES:
                try:
                    for r in self.list_combo(upp, status):
                        rows.setdefault(str(r.get("PAN_ID", "")).strip(), r)
                    ok += 1
                except (ApiError, DeadlineReached) as e:
                    failed += 1
                    self.stats["failures"]["list"] += 1
                    reason = "deadline" if isinstance(e, DeadlineReached) else str(e)
                    self.log(f"list {upp}/{status} failed: {redact(reason, self.key)}")
        rows.pop("", None)
        return rows, ok, failed

    def list_combo(self, upp: str, status: str) -> list:
        cfg = self.config
        year = self.now.astimezone(KST).year
        params = {
            "PG_SZ": cfg.page_size,
            "UPP_AIS_TP_CD": upp,
            "PAN_SS": status,
            "PAN_NT_ST_DT": f"{year - 1}.01.01",
            "CLSG_DT": f"{year + 1}.12.31",
        }
        collected: list = []
        page_cap = cfg.max_pages
        page = 0
        while page < page_cap:
            page += 1
            if self.calls >= cfg.max_calls:
                raise ApiError("call cap reached while listing")
            self.stats["list_calls"] += 1
            data = self.call(LIST_URL, {**params, "PAGE": page})
            page_rows = [r for r in merge_envelope(data).get("dsList") or [] if isinstance(r, dict)]
            collected.extend(page_rows)
            if not page_rows:
                break
            total = _int(page_rows[0].get("ALL_CNT"))
            if total is None:
                if len(page_rows) < cfg.page_size:
                    break
                continue
            if len(collected) >= total:
                break
            needed = math.ceil(total / len(page_rows))
            page_cap = min(max(page_cap, needed), cfg.max_pages_hard)
        return collected

    # -- refresh ----------------------------------------------------------

    def refresh_reason(self, prev: dict | None, status: str) -> str | None:
        if prev is None:
            return "new"
        fetched = parse_iso(prev.get("fetched_at"))
        if fetched is None:
            return "incomplete"
        if status != prev.get("fetched_status"):
            return "status_changed"
        age = self.now - fetched
        recheck = timedelta(hours=self.config.recheck_hours)
        if status == CORRECTION and age >= recheck:
            return "correction"
        if age >= recheck and _has_empty_block(prev):
            return "empty"
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
            entry["fetched_status"] = entry["status"]
        self.stats["refreshed"] += 1

    def calls_needed(self, entry: dict) -> int:
        return 2 if entry["codes"].get("UPP_AIS_TP_CD") in SUPPLY_ELIGIBLE else 1

    def refresh_queue(self, queue: list, notices: dict) -> None:
        for _prio, _fetched, pan_id in sorted(queue):
            entry = notices[pan_id]
            if self.past_deadline():
                self.stats["skipped_by_deadline"] += 1
                continue
            if self.calls + self.calls_needed(entry) > self.config.max_calls:
                self.stats["skipped_by_cap"] += 1
                continue
            try:
                self.refresh(entry, pan_id)
            except DeadlineReached:
                self.stats["skipped_by_deadline"] += 1

    # -- whole run --------------------------------------------------------

    def build(self, previous: dict | None) -> tuple[dict, bool]:
        prev_notices = _previous_notices(previous)
        listed, ok_combos, failed_combos = self.list_notices()
        if ok_combos == 0:
            return {}, False
        self.stats["listed"] = len(listed)
        today = self.now.astimezone(KST).date()

        notices: dict = {}
        queue: list = []
        for pan_id, r in listed.items():
            prev = prev_notices.get(pan_id)
            status = str(r.get("PAN_SS", "")).strip()
            entry = {
                "fetched_at": None, "fetched_status": None, "supply": None, "schedule": None,
                **(prev or {}),
                "status": status,
                "codes": _codes(r),
                "last_seen_at": today.isoformat(),
            }
            notices[pan_id] = entry
            reason = self.refresh_reason(prev, status)
            if reason:
                queue.append((_PRIORITY[reason], (prev or {}).get("fetched_at") or "", pan_id))

        self.refresh_queue(queue, notices)

        drop_before = today - timedelta(days=self.config.drop_days)
        for pan_id, prev in prev_notices.items():
            if pan_id in notices:
                continue
            seen = _seen_date(prev.get("last_seen_at"))
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


_PRIORITY = {"new": 0, "incomplete": 1, "status_changed": 2, "correction": 3, "empty": 4, "expired": 5}


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _has_empty_block(entry: dict) -> bool:
    eligible = (entry.get("codes") or {}).get("UPP_AIS_TP_CD") in SUPPLY_ELIGIBLE
    return entry.get("schedule") == {} or (eligible and entry.get("supply") == {})


def _seen_date(value) -> date | None:
    parsed = parse_iso(value)
    return parsed.date() if parsed else None


def _dtl_query(url) -> dict:
    try:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(str(url or "")).query)
    except ValueError:  # e.g. "Invalid IPv6 URL"
        return {}


def _codes(r: dict) -> dict:
    query = _dtl_query(r.get("DTL_URL"))
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
          log: Callable[[str], None] = lambda _m: None,
          clock: Callable[[], float] = time.monotonic) -> tuple[dict, bool]:
    """Pure entry point used by tests. Returns (document, listing_ok)."""
    return Builder(key, transport, now, config, sleep, log, clock).build(previous)


def should_write(previous, doc: dict, now: datetime, config: Config) -> bool:
    """Rewrite only when notices changed, or as a heartbeat (generated_at 20 h old)."""
    if not isinstance(previous, dict) or previous.get("schema") != SCHEMA:
        return True
    if previous.get("notices") != doc["notices"]:
        return True
    generated = parse_iso(previous.get("generated_at"))
    return generated is None or now - generated >= timedelta(hours=config.heartbeat_hours)


def too_many_failures(stats: dict, config: Config) -> bool:
    attempted = stats["supply_calls"] + stats["schedule_calls"]
    failed = stats["failures"]["supply"] + stats["failures"]["schedule"]
    return failed >= config.fail_min_count and failed >= attempted * config.fail_ratio


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
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
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
    parser.add_argument("--deadline-seconds", type=float, default=Config.deadline_seconds)
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
    config = Config(max_calls=args.max_calls, sleep_seconds=args.sleep,
                    deadline_seconds=args.deadline_seconds)

    def log(message: str) -> None:
        if args.verbose:
            print(redact(message, key), file=sys.stderr)

    previous = load_previous(output)
    now = datetime.now(KST)
    doc, listing_ok = build(previous, key, transport, now, config, log=log)
    if not listing_ok:
        print("ERROR: every notice-list call failed; previous cache left unchanged.", file=sys.stderr)
        return 1

    summary = json.dumps(doc["stats"], ensure_ascii=False)
    if too_many_failures(doc["stats"], config):
        print(f"ERROR: most supply/detail calls failed; cache not updated. stats={summary}", file=sys.stderr)
        return 1

    if not should_write(previous, doc, now, config):
        print(f"cache unchanged; {output} left as is. stats={summary}")
        return 0
    write_atomic(output, doc)
    print(redact(f"wrote {output} stats={summary}", key))
    return 0


if __name__ == "__main__":
    sys.exit(main())
