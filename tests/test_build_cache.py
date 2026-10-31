"""Unit tests for scripts/build_cache.py (stdlib unittest, no network)."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_cache  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
KEY = "f05eTESTKEY+abc/def=="  # fake key with characters that get percent-encoded
NOW = datetime(2026, 11, 1, 6, 0, tzinfo=build_cache.KST)


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


SUPPLY_063 = fixture_text("happy_housing_063.json")
SUPPLY_060 = fixture_text("public_rental_060.json")
SCHEDULE_063 = fixture_text("schedule_063.json")
XML_ERROR = fixture_text("gateway_error.xml")


def row(pan_id, upp="06", status="공고중", posted="2026.10.30", spl="063", **extra):
    base = {
        "PAN_ID": pan_id,
        "PAN_NM": f"공고 {pan_id}",
        "UPP_AIS_TP_CD": upp,
        "AIS_TP_CD": "10",
        "PAN_SS": status,
        "PAN_NT_ST_DT": posted,
        "CCR_CNNT_SYS_DS_CD": "03",
        "SPL_INF_TP_CD": spl,
    }
    base.update(extra)
    return base


def list_body(rows, all_cnt=None):
    if all_cnt is not None:
        rows = [dict(r, ALL_CNT=str(all_cnt)) for r in rows]
    return json.dumps(
        [
            {"dsSch": [{"PG_SZ": "100"}]},
            {"dsList": rows},
            {"resHeader": [{"SS_CODE": "Y"}]},
        ],
        ensure_ascii=False,
    )


EMPTY_LIST = list_body([])


class FakeTransport:
    """Routes calls by URL. Values are a body string or an exception to raise."""

    def __init__(self, listing=None, supply=None, detail=None, list_error=None):
        # listing: {(upp, status): [page1_body, page2_body, ...]}
        self.listing = listing or {}
        self.supply = supply or {}
        self.detail = detail or {}
        self.list_error = list_error
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if url == build_cache.LIST_URL:
            if self.list_error is not None:
                raise self.list_error
            pages = self.listing.get((params["UPP_AIS_TP_CD"], params["PAN_SS"]), [])
            index = int(params["PAGE"]) - 1
            return pages[index] if index < len(pages) else EMPTY_LIST
        table = self.supply if url == build_cache.SUPPLY_URL else self.detail
        value = table.get(params["PAN_ID"], SCHEDULE_063 if url == build_cache.DETAIL_URL else SUPPLY_063)
        if isinstance(value, BaseException):
            raise value
        return value

    def detail_calls(self, url):
        return [p["PAN_ID"] for u, p in self.calls if u == url]


def run_build(transport, prev=None, now=NOW, **cfg):
    config = build_cache.Config(sleep_seconds=0, **cfg)
    return build_cache.build(prev, KEY, transport, now, config, sleep=lambda _s: None)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def prev_entry(fetched, status="공고중", upp="06", last_seen=None):
    return {
        "fetched_at": iso(fetched) if fetched else None,
        "last_seen_at": iso(last_seen or fetched or NOW),
        "status": status,
        "codes": {"UPP_AIS_TP_CD": upp, "CCR_CNNT_SYS_DS_CD": "03", "SPL_INF_TP_CD": "063"},
        "supply": {"dsList01": [{"DDO_AR": "old"}], "dsList01Nm": [{"DDO_AR": "전용면적"}]},
        "schedule": {"dsSplScdl": [{"SBSC_ACP_ST_DT": "old"}]},
    }


def prev_doc(notices):
    return {"schema": 1, "generated_at": iso(NOW - timedelta(hours=6)), "notices": notices}


class ListingTests(unittest.TestCase):
    def test_lists_every_type_and_status_with_window_params(self):
        t = FakeTransport()
        run_build(t)
        list_calls = [p for u, p in t.calls if u == build_cache.LIST_URL]
        combos = {(p["UPP_AIS_TP_CD"], p["PAN_SS"]) for p in list_calls}
        self.assertEqual(
            combos,
            {(u, s) for u in ("06", "05", "39", "13") for s in ("공고중", "접수중", "정정공고중")},
        )
        first = list_calls[0]
        self.assertEqual(first["PG_SZ"], 100)
        self.assertEqual(first["PAGE"], 1)
        self.assertEqual(first["PAN_NT_ST_DT"], "2026.08.03")  # 90 days back
        self.assertEqual(first["serviceKey"], KEY)

    def test_pages_until_all_cnt_is_reached(self):
        page1 = list_body([row(f"a{i}") for i in range(100)], all_cnt=150)
        page2 = list_body([row(f"b{i}") for i in range(50)], all_cnt=150)
        t = FakeTransport(listing={("06", "공고중"): [page1, page2]})
        doc, ok = run_build(t, max_calls=2000)
        self.assertTrue(ok)
        pages = [p["PAGE"] for u, p in t.calls
                 if u == build_cache.LIST_URL and p["UPP_AIS_TP_CD"] == "06" and p["PAN_SS"] == "공고중"]
        self.assertEqual(pages, [1, 2])
        self.assertEqual(len(doc["notices"]), 150)

    def test_keeps_paging_when_server_caps_page_size_below_request(self):
        # server returns 50 rows although 100 were asked; ALL_CNT says there are more
        page1 = list_body([row(f"a{i}") for i in range(50)], all_cnt=80)
        page2 = list_body([row(f"b{i}") for i in range(30)], all_cnt=80)
        t = FakeTransport(listing={("06", "공고중"): [page1, page2]})
        doc, _ = run_build(t, max_calls=2000)
        self.assertEqual(len(doc["notices"]), 80)

    def test_drops_rows_posted_before_the_90_day_window(self):
        page = list_body([row("recent"), row("ancient", posted="2026.01.05")])
        t = FakeTransport(listing={("06", "공고중"): [page]})
        doc, _ = run_build(t)
        self.assertEqual(set(doc["notices"]), {"recent"})

    def test_reads_the_crafted_list_fixture(self):
        t = FakeTransport(listing={("06", "공고중"): [fixture_text("list_page.json")]})
        doc, ok = run_build(t)
        self.assertTrue(ok)
        self.assertEqual(set(doc["notices"]), {"2015122300019236", "2015122300019001"})
        self.assertEqual(doc["notices"]["2015122300019001"]["codes"]["SPL_INF_TP_CD"], "060")

    def test_all_list_calls_failing_reports_not_ok(self):
        t = FakeTransport(list_error=urllib.error.URLError("down"))
        _doc, ok = run_build(t, prev=prev_doc({"p1": prev_entry(NOW - timedelta(days=1))}))
        self.assertFalse(ok)


class EntryShapeTests(unittest.TestCase):
    def test_supply_keeps_only_dslist_blocks_and_schedule_only_dssplscdl(self):
        page = list_body([row("2015122300019236")])
        t = FakeTransport(listing={("06", "공고중"): [page]})
        doc, _ = run_build(t)
        entry = doc["notices"]["2015122300019236"]
        self.assertEqual(set(entry["supply"]), {"dsList01", "dsList01Nm"})
        self.assertEqual(entry["supply"]["dsList01"][0]["DDO_AR"], "16.98")
        self.assertEqual(set(entry["schedule"]), {"dsSplScdl"})
        self.assertEqual(entry["codes"]["SPL_INF_TP_CD"], "063")
        self.assertEqual(entry["status"], "공고중")
        self.assertEqual(entry["fetched_at"], "2026-11-01T06:00:00+09:00")

    def test_multi_block_supply_keeps_all_blocks_including_empty_ones(self):
        page = list_body([row("2015122300019001", spl="060")])
        t = FakeTransport(listing={("06", "공고중"): [page]}, supply={"2015122300019001": SUPPLY_060})
        doc, _ = run_build(t)
        supply = doc["notices"]["2015122300019001"]["supply"]
        self.assertEqual(
            set(supply),
            {f"dsList0{i}" for i in range(1, 6)} | {f"dsList0{i}Nm" for i in range(1, 6)},
        )

    def test_detail_request_uses_codes_from_the_list(self):
        page = list_body([row("p1")])
        t = FakeTransport(listing={("06", "공고중"): [page]})
        run_build(t)
        supply_params = [p for u, p in t.calls if u == build_cache.SUPPLY_URL][0]
        self.assertEqual(supply_params["PAN_ID"], "p1")
        self.assertEqual(supply_params["CCR_CNNT_SYS_DS_CD"], "03")
        self.assertEqual(supply_params["SPL_INF_TP_CD"], "063")
        self.assertEqual(supply_params["UPP_AIS_TP_CD"], "06")

    def test_codes_fall_back_to_dtl_url_query(self):
        r = {"PAN_ID": "p9", "PAN_SS": "공고중", "PAN_NT_ST_DT": "2026.10.30",
             "DTL_URL": "https://apply.lh.or.kr/x.do?panId=p9&ccrCnntSysDsCd=03&uppAisTpCd=06&splInfTpCd=063"}
        t = FakeTransport(listing={("06", "공고중"): [list_body([r])]})
        doc, _ = run_build(t)
        self.assertEqual(doc["notices"]["p9"]["codes"]["UPP_AIS_TP_CD"], "06")
        self.assertEqual(doc["notices"]["p9"]["codes"]["SPL_INF_TP_CD"], "063")

    def test_welfare_notice_gets_schedule_only(self):
        page = list_body([row("w1", upp="13")])
        t = FakeTransport(listing={("13", "공고중"): [page]})
        doc, _ = run_build(t)
        self.assertEqual(t.detail_calls(build_cache.SUPPLY_URL), [])
        self.assertEqual(t.detail_calls(build_cache.DETAIL_URL), ["w1"])
        self.assertIsNone(doc["notices"]["w1"]["supply"])
        self.assertIsNotNone(doc["notices"]["w1"]["schedule"])

    def test_successful_response_without_blocks_is_stored_as_empty_map(self):
        empty = json.dumps([{"dsSch": []}, {"resHeader": [{"SS_CODE": "Y"}]}])
        page = list_body([row("p1")])
        t = FakeTransport(listing={("06", "공고중"): [page]}, supply={"p1": empty}, detail={"p1": empty})
        doc, _ = run_build(t)
        self.assertEqual(doc["notices"]["p1"]["supply"], {})
        self.assertEqual(doc["notices"]["p1"]["schedule"], {})


class IncrementalTests(unittest.TestCase):
    def test_only_new_corrected_and_week_old_notices_are_refetched(self):
        listing = list_body([
            row("fresh"),                               # fetched 1 day ago -> reuse
            row("old"),                                 # fetched 8 days ago -> refetch
            row("new"),                                 # not in previous file -> fetch
            row("corr", status="정정공고중"),             # became 정정공고중 -> refetch
            row("corr_recent", status="정정공고중"),      # already 정정, fetched 2h ago -> reuse
        ])
        prev = prev_doc({
            "fresh": prev_entry(NOW - timedelta(days=1)),
            "old": prev_entry(NOW - timedelta(days=8)),
            "corr": prev_entry(NOW - timedelta(days=1), status="공고중"),
            "corr_recent": prev_entry(NOW - timedelta(hours=2), status="정정공고중"),
        })
        t = FakeTransport(listing={("06", "공고중"): [listing]})
        doc, ok = run_build(t, prev=prev)
        self.assertTrue(ok)
        self.assertEqual(sorted(t.detail_calls(build_cache.SUPPLY_URL)), ["corr", "new", "old"])
        self.assertEqual(sorted(t.detail_calls(build_cache.DETAIL_URL)), ["corr", "new", "old"])
        # reused entries keep their old blocks and fetched_at
        self.assertEqual(doc["notices"]["fresh"]["supply"]["dsList01"][0]["DDO_AR"], "old")
        self.assertEqual(doc["notices"]["fresh"]["fetched_at"], iso(NOW - timedelta(days=1)))
        self.assertEqual(doc["notices"]["fresh"]["last_seen_at"], iso(NOW))
        self.assertEqual(doc["notices"]["old"]["supply"]["dsList01"][0]["DDO_AR"], "16.98")
        self.assertEqual(doc["stats"]["refreshed"], 3)

    def test_correction_notice_is_refetched_again_after_24_hours(self):
        listing = list_body([row("corr", status="정정공고중")])
        prev = prev_doc({"corr": prev_entry(NOW - timedelta(hours=25), status="정정공고중")})
        t = FakeTransport(listing={("06", "공고중"): [listing]})
        run_build(t, prev=prev)
        self.assertEqual(t.detail_calls(build_cache.SUPPLY_URL), ["corr"])

    def test_unlisted_notice_is_kept_for_30_days_then_dropped(self):
        prev = prev_doc({
            "gone_recent": prev_entry(NOW - timedelta(days=3), last_seen=NOW - timedelta(days=29)),
            "gone_old": prev_entry(NOW - timedelta(days=3), last_seen=NOW - timedelta(days=31)),
        })
        t = FakeTransport(listing={("06", "공고중"): [list_body([row("p1")])]})
        doc, _ = run_build(t, prev=prev)
        self.assertIn("gone_recent", doc["notices"])
        self.assertNotIn("gone_old", doc["notices"])
        self.assertEqual(doc["stats"]["dropped"], 1)

    def test_nothing_is_dropped_when_a_list_call_failed(self):
        prev = prev_doc({"gone_old": prev_entry(NOW - timedelta(days=3), last_seen=NOW - timedelta(days=40))})

        class PartialList(FakeTransport):
            def __call__(self, url, params):
                if url == build_cache.LIST_URL and params["UPP_AIS_TP_CD"] == "05":
                    self.calls.append((url, dict(params)))
                    return XML_ERROR
                return super().__call__(url, params)

        t = PartialList(listing={("06", "공고중"): [list_body([row("p1")])]})
        doc, ok = run_build(t, prev=prev)
        self.assertTrue(ok)
        self.assertIn("gone_old", doc["notices"])
        self.assertEqual(doc["stats"]["failures"]["list"], 3)

    def test_call_cap_limits_detail_calls_and_counts_skipped(self):
        listing = list_body([row(f"n{i}") for i in range(10)])
        t = FakeTransport(listing={("06", "공고중"): [listing]})
        doc, _ = run_build(t, max_calls=12 + 6)  # 12 list calls + 3 notices x 2
        detail = [u for u, _p in t.calls if u != build_cache.LIST_URL]
        self.assertEqual(len(detail), 6)
        self.assertEqual(len(t.calls), 18)
        self.assertEqual(doc["stats"]["skipped_by_cap"], 7)
        # skipped new notices are listed but carry no data yet
        skipped = [e for e in doc["notices"].values() if e["fetched_at"] is None]
        self.assertEqual(len(skipped), 7)


class ErrorHandlingTests(unittest.TestCase):
    def _run_with(self, supply_value=None, detail_value=None):
        listing = list_body([row("old")])
        prev = prev_doc({"old": prev_entry(NOW - timedelta(days=8))})
        supply = {"old": supply_value} if supply_value is not None else {}
        detail = {"old": detail_value} if detail_value is not None else {}
        t = FakeTransport(listing={("06", "공고중"): [listing]}, supply=supply, detail=detail)
        return run_build(t, prev=prev)

    def assert_supply_kept(self, doc):
        entry = doc["notices"]["old"]
        self.assertEqual(entry["supply"]["dsList01"][0]["DDO_AR"], "old")
        # not fully refreshed -> fetched_at unchanged, retried next run
        self.assertEqual(entry["fetched_at"], iso(NOW - timedelta(days=8)))
        self.assertEqual(doc["stats"]["failures"]["supply"], 1)

    def test_xml_gateway_error_keeps_previous_value(self):
        doc, _ = self._run_with(supply_value=XML_ERROR)
        self.assert_supply_kept(doc)
        # the other part still updates
        self.assertEqual(doc["notices"]["old"]["schedule"]["dsSplScdl"][0]["SBSC_ACP_ST_DT"], "2026.11.10")

    def test_ss_code_not_y_keeps_previous_value(self):
        bad = json.dumps([{"resHeader": [{"SS_CODE": "N", "RS_DTTM": "x"}]}])
        doc, _ = self._run_with(supply_value=bad)
        self.assert_supply_kept(doc)

    def test_http_error_keeps_previous_value(self):
        err = urllib.error.HTTPError("https://example.invalid", 500, "boom", {}, None)
        doc, _ = self._run_with(supply_value=err)
        self.assert_supply_kept(doc)

    def test_non_json_garbage_keeps_previous_value(self):
        doc, _ = self._run_with(supply_value="<html>maintenance</html>")
        self.assert_supply_kept(doc)

    def test_schedule_error_keeps_previous_schedule(self):
        doc, _ = self._run_with(detail_value=XML_ERROR)
        entry = doc["notices"]["old"]
        self.assertEqual(entry["schedule"]["dsSplScdl"][0]["SBSC_ACP_ST_DT"], "old")
        self.assertEqual(doc["stats"]["failures"]["schedule"], 1)


class OutputTests(unittest.TestCase):
    def test_document_has_schema_kst_timestamp_attribution_and_stats(self):
        t = FakeTransport(listing={("06", "공고중"): [list_body([row("p1")])]})
        doc, _ = run_build(t)
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["generated_at"], "2026-11-01T06:00:00+09:00")
        self.assertIn("한국토지주택공사", doc["source"])
        self.assertIn("data.go.kr", doc["source"])
        self.assertIn("이용허락범위 제한 없음", doc["license"])
        for name in ("listed", "notices", "refreshed", "list_calls", "supply_calls",
                     "schedule_calls", "failures", "dropped", "skipped_by_cap"):
            self.assertIn(name, doc["stats"])
        self.assertEqual(doc["stats"]["listed"], 1)

    def test_generated_at_is_converted_to_kst(self):
        from datetime import timezone
        utc = datetime(2026, 11, 1, 21, 3, tzinfo=timezone.utc)
        doc, _ = run_build(FakeTransport(), now=utc)
        self.assertEqual(doc["generated_at"], "2026-11-02T06:03:00+09:00")


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "docs" / "lh_cache_v1.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, transport, env_key=KEY, extra_args=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {"DATA_GO_KR_API_KEY": env_key} if env_key is not None else {}
        with mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = build_cache.main(
                ["--output", str(self.out), "--sleep", "0", *extra_args],
                transport=transport,
            )
        return code, stdout.getvalue() + stderr.getvalue()

    def test_missing_key_fails_clearly_without_writing(self):
        code, logs = self.run_main(FakeTransport(), env_key=None)
        self.assertNotEqual(code, 0)
        self.assertIn("DATA_GO_KR_API_KEY", logs)
        self.assertFalse(self.out.exists())

    def test_blank_key_fails_clearly(self):
        code, _logs = self.run_main(FakeTransport(), env_key="   ")
        self.assertNotEqual(code, 0)

    def test_writes_file_and_reads_it_back_as_previous_on_next_run(self):
        t = FakeTransport(listing={("06", "공고중"): [list_body([row("p1")])]})
        code, _ = self.run_main(t)
        self.assertEqual(code, 0)
        first = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertIn("p1", first["notices"])
        t2 = FakeTransport(listing={("06", "공고중"): [list_body([row("p1")])]})
        code, _ = self.run_main(t2)
        self.assertEqual(code, 0)
        self.assertEqual(t2.detail_calls(build_cache.SUPPLY_URL), [])  # reused

    def test_malformed_previous_file_is_ignored(self):
        self.out.parent.mkdir(parents=True)
        self.out.write_text("{not json", encoding="utf-8")
        t = FakeTransport(listing={("06", "공고중"): [list_body([row("p1")])]})
        code, _ = self.run_main(t)
        self.assertEqual(code, 0)
        self.assertIn("p1", json.loads(self.out.read_text(encoding="utf-8"))["notices"])

    def test_list_outage_exits_nonzero_and_keeps_previous_file(self):
        self.out.parent.mkdir(parents=True)
        self.out.write_text(json.dumps(prev_doc({})), encoding="utf-8")
        before = self.out.read_text(encoding="utf-8")
        code, _ = self.run_main(FakeTransport(list_error=urllib.error.URLError("down")))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.out.read_text(encoding="utf-8"), before)

    def test_all_detail_calls_failing_exits_nonzero_without_bumping_file(self):
        t = FakeTransport(
            listing={("06", "공고중"): [list_body([row("p1"), row("p2")])]},
            supply={"p1": XML_ERROR, "p2": XML_ERROR},
            detail={"p1": XML_ERROR, "p2": XML_ERROR},
        )
        code, _ = self.run_main(t)
        self.assertNotEqual(code, 0)
        self.assertFalse(self.out.exists())

    def test_key_never_appears_in_logs_or_output(self):
        encoded = build_cache.urllib.parse.quote(KEY, safe="")
        leaky = urllib.error.URLError(f"failed for https://x?serviceKey={KEY}&k={encoded}")
        t = FakeTransport(
            listing={("06", "공고중"): [list_body([row("p1"), row("p2")])]},
            supply={"p1": leaky},
        )

        code, logs = self.run_main(t, extra_args=("--verbose",))
        self.assertEqual(code, 0)
        written = self.out.read_text(encoding="utf-8")
        for secret in (KEY, encoded, build_cache.urllib.parse.quote_plus(KEY)):
            self.assertNotIn(secret, logs)
            self.assertNotIn(secret, written)
        self.assertIn("***", logs)  # the failure was logged, redacted

    def test_percent_encoded_key_is_decoded_before_use(self):
        encoded = build_cache.urllib.parse.quote(KEY, safe="")
        t = FakeTransport()
        self.run_main(t, env_key=encoded)
        self.assertEqual(t.calls[0][1]["serviceKey"], KEY)


class RedactTests(unittest.TestCase):
    def test_redacts_raw_and_encoded_forms(self):
        text = f"a {KEY} b {build_cache.urllib.parse.quote(KEY, safe='')} c"
        self.assertEqual(build_cache.redact(text, KEY), "a *** b *** c")


if __name__ == "__main__":
    unittest.main()
