"""Unit tests for scripts/build_cache.py (stdlib unittest, no network)."""

import contextlib
import http.client
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
KEY = "TESTKEY+abc/def=="  # fake key with characters that get percent-encoded
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


def run_build(transport, prev=None, now=NOW, clock=None, **cfg):
    config = build_cache.Config(sleep_seconds=0, **cfg)
    return build_cache.build(prev, KEY, transport, now, config, sleep=lambda _s: None,
                             clock=clock or (lambda: 0.0))


def iso(dt):
    return dt.isoformat(timespec="seconds")


def prev_entry(fetched, status="공고중", upp="06", last_seen=None, fetched_status=None,
               schedule=None):
    return {
        "fetched_at": iso(fetched) if fetched else None,
        "fetched_status": fetched_status or status,
        "last_seen_at": (last_seen or fetched or NOW).date().isoformat(),
        "status": status,
        "codes": {"UPP_AIS_TP_CD": upp, "CCR_CNNT_SYS_DS_CD": "03", "SPL_INF_TP_CD": "063"},
        "supply": {"dsList01": [{"DDO_AR": "old"}], "dsList01Nm": [{"DDO_AR": "전용면적"}]},
        "schedule": schedule if schedule is not None else {"dsSplScdl": [{"SBSC_ACP_ST_DT": "old"}]},
    }


def prev_doc(notices):
    return {"schema": 1, "generated_at": iso(NOW - timedelta(hours=6)), "notices": notices}


class ListingTests(unittest.TestCase):
    def test_lists_every_type_and_status_with_wide_date_params(self):
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
        # status bounds the range (H1a): same wide window as the app, no 90-day cut
        self.assertEqual(first["PAN_NT_ST_DT"], "2025.01.01")
        self.assertEqual(first["CLSG_DT"], "2027.12.31")
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

    def test_keeps_active_notices_posted_long_ago(self):
        page = list_body([row("recent"), row("ancient", posted="2025.03.05")])
        t = FakeTransport(listing={("06", "공고중"): [page]})
        doc, _ = run_build(t)
        self.assertEqual(set(doc["notices"]), {"recent", "ancient"})

    def test_raises_page_cap_when_all_cnt_is_large(self):
        pages = [list_body([row(f"p{n}_{i}") for i in range(100)], all_cnt=350) for n in range(3)]
        pages.append(list_body([row(f"p3_{i}") for i in range(50)], all_cnt=350))
        t = FakeTransport(listing={("06", "공고중"): pages})
        doc, _ = run_build(t, max_pages=2, max_calls=5000)
        self.assertEqual(doc["stats"]["listed"], 350)

    def test_page_cap_is_still_bounded_by_the_hard_limit(self):
        huge = list_body([row(f"x{i}") for i in range(100)], all_cnt=100000)
        t = FakeTransport(listing={("06", "공고중"): [huge] * 500})
        run_build(t, max_calls=5000)
        pages = [p for u, p in t.calls if u == build_cache.LIST_URL and p["UPP_AIS_TP_CD"] == "06"
                 and p["PAN_SS"] == "공고중"]
        self.assertEqual(len(pages), build_cache.Config().max_pages_hard)

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


class FlakyList(FakeTransport):
    """Fails the first `times` list calls of one (type, status) combo with a transport error."""

    def __init__(self, fail, times, **kwargs):
        super().__init__(**kwargs)
        self.fail = fail
        self.left = times

    def __call__(self, url, params):
        if (url == build_cache.LIST_URL and self.left > 0
                and (params["UPP_AIS_TP_CD"], params["PAN_SS"]) == self.fail):
            self.left -= 1
            self.calls.append((url, dict(params)))
            raise urllib.error.URLError("_ssl.c:994: The handshake operation timed out")
        return super().__call__(url, params)

    def list_combos(self):
        return [(p["UPP_AIS_TP_CD"], p["PAN_SS"]) for u, p in self.calls if u == build_cache.LIST_URL]


class ListRetryTests(unittest.TestCase):
    """Plan A11: a list combo that fails is asked once more after the others."""

    COMBO = ("39", "공고중")
    LISTING = {("06", "공고중"): [list_body([row("p1")])],
               ("39", "공고중"): [list_body([row("late", upp="39")])]}

    def test_a_failed_combo_is_retried_once_and_its_notices_are_kept(self):
        t = FlakyList(self.COMBO, 1, listing=self.LISTING)
        doc, ok = run_build(t)
        self.assertTrue(ok)
        self.assertEqual(set(doc["notices"]), {"p1", "late"})
        self.assertEqual(t.list_combos().count(self.COMBO), 2)
        self.assertEqual(doc["stats"]["failures"]["list"], 0)
        self.assertEqual(doc["stats"]["list_retried"], 1)

    def test_a_combo_that_fails_twice_counts_as_one_failure_and_is_not_asked_a_third_time(self):
        t = FlakyList(self.COMBO, 99, listing=self.LISTING)
        doc, ok = run_build(t)
        self.assertTrue(ok)
        self.assertEqual(set(doc["notices"]), {"p1"})
        self.assertEqual(t.list_combos().count(self.COMBO), 2)
        self.assertEqual(doc["stats"]["failures"]["list"], 1)
        self.assertEqual(doc["stats"]["list_retried"], 1)

    def test_the_retry_comes_after_every_other_combo(self):
        t = FlakyList(self.COMBO, 1, listing=self.LISTING)
        run_build(t)
        combos = t.list_combos()
        self.assertEqual(combos[-1], self.COMBO)
        self.assertEqual(len(combos), 13)  # 12 combos + 1 retry

    def test_pauses_once_before_retrying_and_not_at_all_without_failures(self):
        def pauses(transport):
            slept = []
            config = build_cache.Config(sleep_seconds=0)
            build_cache.build(None, KEY, transport, NOW, config, sleep=slept.append, clock=lambda: 0.0)
            return slept

        self.assertEqual(pauses(FlakyList(self.COMBO, 1, listing=self.LISTING)),
                         [build_cache.Config().list_retry_pause_seconds])
        self.assertEqual(pauses(FakeTransport(listing=self.LISTING)), [])
        self.assertEqual(build_cache.Config().list_retry_pause_seconds, 5.0)

    def test_no_retry_statistic_when_nothing_failed(self):
        doc, _ = run_build(FakeTransport(listing=self.LISTING))
        self.assertEqual(doc["stats"]["list_retried"], 0)
        self.assertEqual(doc["stats"]["failures"]["list"], 0)

    def test_combos_stopped_by_the_deadline_are_not_retried(self):
        ticks = iter([0.0, 0.0, 0.0] + [100.0] * 100)  # start + 2 calls in time, then past the deadline
        t = FakeTransport(listing=self.LISTING)
        doc, ok = run_build(t, clock=lambda: next(ticks), deadline_seconds=10)
        self.assertTrue(ok)
        self.assertEqual(len([u for u, _p in t.calls if u == build_cache.LIST_URL]), 2)
        self.assertEqual(doc["stats"]["list_retried"], 0)
        self.assertEqual(doc["stats"]["failures"]["list"], 10)

    def test_combos_stopped_by_the_call_cap_are_not_retried(self):
        t = FakeTransport()
        doc, ok = run_build(t, max_calls=2)
        self.assertTrue(ok)
        self.assertEqual(len([u for u, _p in t.calls if u == build_cache.LIST_URL]), 2)
        self.assertEqual(doc["stats"]["list_retried"], 0)
        self.assertEqual(doc["stats"]["failures"]["list"], 10)

    def test_a_retry_that_hits_the_call_cap_is_a_failure(self):
        # 12 first attempts use the whole cap; the retry has no call left
        t = FlakyList(("06", "공고중"), 1, listing=self.LISTING)
        doc, ok = run_build(t, max_calls=12)
        self.assertTrue(ok)
        self.assertEqual(len(t.list_combos()), 12)
        self.assertEqual(doc["stats"]["list_retried"], 0)  # never asked again: no call was left
        self.assertEqual(doc["stats"]["failures"]["list"], 1)

    def test_a_combo_saved_by_the_retry_does_not_block_the_30_day_drop(self):
        prev = prev_doc({"gone_old": prev_entry(NOW - timedelta(days=3), last_seen=NOW - timedelta(days=40))})
        doc, ok = run_build(FlakyList(self.COMBO, 1, listing=self.LISTING), prev=prev)
        self.assertTrue(ok)
        self.assertNotIn("gone_old", doc["notices"])
        self.assertEqual(doc["stats"]["dropped"], 1)

    def test_a_combo_that_fails_twice_still_blocks_the_30_day_drop(self):
        prev = prev_doc({"gone_old": prev_entry(NOW - timedelta(days=3), last_seen=NOW - timedelta(days=40))})
        doc, _ = run_build(FlakyList(self.COMBO, 99, listing=self.LISTING), prev=prev)
        self.assertIn("gone_old", doc["notices"])
        self.assertEqual(doc["stats"]["dropped"], 0)

    def test_ok_and_failed_combos_always_add_up_to_twelve(self):
        for times in (0, 1, 99):
            builder = build_cache.Builder(KEY, FlakyList(self.COMBO, times, listing=self.LISTING), NOW,
                                          build_cache.Config(sleep_seconds=0), lambda _s: None,
                                          lambda _m: None, lambda: 0.0)
            _rows, ok, failed = builder.list_notices()
            self.assertEqual((ok + failed, failed), (12, 1 if times == 99 else 0))

    def test_rows_of_a_failed_attempt_do_not_leak_into_the_result(self):
        class FailsOnPageTwo(FakeTransport):
            def __call__(self, url, params):
                if url == build_cache.LIST_URL and params["UPP_AIS_TP_CD"] == "39" and int(params["PAGE"]) == 2:
                    self.calls.append((url, dict(params)))
                    raise urllib.error.URLError("down")
                return super().__call__(url, params)

        page1 = list_body([row(f"half{i}", upp="39") for i in range(100)], all_cnt=150)
        t = FailsOnPageTwo(listing={("06", "공고중"): [list_body([row("p1")])], ("39", "공고중"): [page1]})
        doc, ok = run_build(t, max_calls=2000)
        self.assertTrue(ok)
        self.assertEqual(set(doc["notices"]), {"p1"})
        self.assertEqual(doc["stats"]["failures"]["list"], 1)

    def test_two_failed_combos_share_one_pause(self):
        class TwoFlaky(FlakyList):
            def __call__(self, url, params):
                combo = (params.get("UPP_AIS_TP_CD"), params.get("PAN_SS"))
                if url == build_cache.LIST_URL and combo in self.fail and combo not in self.seen:
                    self.seen.add(combo)
                    self.calls.append((url, dict(params)))
                    raise urllib.error.URLError("down")
                return FakeTransport.__call__(self, url, params)

        t = TwoFlaky({("39", "공고중"), ("13", "접수중")}, 0, listing=self.LISTING)
        t.seen = set()
        slept = []
        doc, _ = build_cache.build(None, KEY, t, NOW, build_cache.Config(sleep_seconds=0),
                                   sleep=slept.append, clock=lambda: 0.0)
        self.assertEqual(slept, [5.0])
        self.assertEqual(doc["stats"]["list_retried"], 2)

    def test_no_pause_when_it_is_zero_or_the_deadline_has_passed(self):
        def pauses(clock, **cfg):
            slept = []
            build_cache.build(None, KEY, FlakyList(self.COMBO, 1, listing=self.LISTING), NOW,
                              build_cache.Config(sleep_seconds=0, **cfg), sleep=slept.append, clock=clock)
            return slept

        self.assertEqual(pauses(lambda: 0.0, list_retry_pause_seconds=0), [])
        ticks = iter([0.0] * 13 + [100.0] * 100)  # start + 12 list calls in time, then past the deadline
        self.assertEqual(pauses(lambda: next(ticks), deadline_seconds=10), [])

    def test_the_pause_is_cut_to_the_time_left_before_the_deadline(self):
        slept = []
        build_cache.build(None, KEY, FlakyList(self.COMBO, 1, listing=self.LISTING), NOW,
                          build_cache.Config(sleep_seconds=0, list_retry_pause_seconds=float("inf"),
                                             deadline_seconds=30),
                          sleep=slept.append, clock=lambda: 0.0)
        self.assertEqual(slept, [30])

    def test_failure_reasons_are_logged_on_one_line_without_the_key(self):
        class NastyGateway(FakeTransport):
            def __call__(self, url, params):
                if url == build_cache.LIST_URL and params["UPP_AIS_TP_CD"] == "39":
                    self.calls.append((url, dict(params)))
                    return f"<x><returnAuthMsg>BAD\n::error::forged {KEY}</returnAuthMsg></x>"
                return super().__call__(url, params)

        logs = []
        build_cache.build(None, KEY, NastyGateway(listing=self.LISTING), NOW,
                          build_cache.Config(sleep_seconds=0, list_retry_pause_seconds=0),
                          sleep=lambda _s: None, log=logs.append, clock=lambda: 0.0)
        mine = [m for m in logs if m.startswith("list 39/")]
        self.assertEqual(len(mine), 6)  # 3 combos: one "will retry" and one final line each
        for message in mine:
            self.assertNotIn("\n", message)
            self.assertNotIn(KEY, message)
            self.assertIn("BAD ::error::forged", message)

    def test_every_combo_failing_twice_still_reports_not_ok(self):
        t = FakeTransport(list_error=urllib.error.URLError("down"))
        _doc, ok = run_build(t)
        self.assertFalse(ok)
        self.assertEqual(len(t.calls), 24)


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
        self.assertEqual(entry["fetched_status"], "공고중")
        self.assertEqual(entry["last_seen_at"], "2026-11-01")  # date granularity (M4)

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

    def test_malformed_dtl_url_does_not_crash(self):
        r = {"PAN_ID": "p8", "PAN_SS": "공고중", "UPP_AIS_TP_CD": "06", "DTL_URL": "http://[bad-ipv6/x?panId=p8"}
        t = FakeTransport(listing={("06", "공고중"): [list_body([r])]})
        doc, ok = run_build(t)
        self.assertTrue(ok)
        self.assertEqual(doc["notices"]["p8"]["codes"]["UPP_AIS_TP_CD"], "06")

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
        self.assertEqual(doc["notices"]["fresh"]["last_seen_at"], "2026-11-01")
        self.assertEqual(doc["notices"]["old"]["supply"]["dsList01"][0]["DDO_AR"], "16.98")
        self.assertEqual(doc["stats"]["refreshed"], 3)

    def test_correction_notice_is_refetched_again_after_24_hours(self):
        listing = list_body([row("corr", status="정정공고중")])
        prev = prev_doc({"corr": prev_entry(NOW - timedelta(hours=25), status="정정공고중")})
        t = FakeTransport(listing={("06", "공고중"): [listing]})
        run_build(t, prev=prev)
        self.assertEqual(t.detail_calls(build_cache.SUPPLY_URL), ["corr"])

    def test_failed_correction_refresh_is_retried_next_run(self):
        # last run saw 정정공고중 but the refresh failed -> fetched_status still 공고중 (M3)
        listing = list_body([row("corr", status="정정공고중")])
        prev = prev_doc({"corr": prev_entry(NOW - timedelta(hours=2), status="정정공고중",
                                            fetched_status="공고중")})
        t = FakeTransport(listing={("06", "공고중"): [listing]})
        doc, _ = run_build(t, prev=prev)
        self.assertEqual(t.detail_calls(build_cache.SUPPLY_URL), ["corr"])
        self.assertEqual(doc["notices"]["corr"]["fetched_status"], "정정공고중")

    def test_status_change_other_than_correction_triggers_refresh(self):
        listing = list_body([row("p1", status="접수중")])
        prev = prev_doc({"p1": prev_entry(NOW - timedelta(hours=2), status="공고중")})
        t = FakeTransport(listing={("06", "공고중"): [listing]})
        run_build(t, prev=prev)
        self.assertEqual(t.detail_calls(build_cache.DETAIL_URL), ["p1"])

    def test_empty_blocks_are_refetched_after_24_hours(self):
        listing = list_body([row("e_old"), row("e_new")])
        prev = prev_doc({
            "e_old": prev_entry(NOW - timedelta(hours=25), schedule={}),
            "e_new": prev_entry(NOW - timedelta(hours=23), schedule={}),
        })
        t = FakeTransport(listing={("06", "공고중"): [listing]})
        run_build(t, prev=prev)
        self.assertEqual(t.detail_calls(build_cache.DETAIL_URL), ["e_old"])

    def test_deadline_stops_refreshing_and_keeps_what_is_done(self):
        listing = list_body([row(f"n{i}") for i in range(10)])
        t = FakeTransport(listing={("06", "공고중"): [listing]})
        ticks = iter(range(0, 100000, 1))  # one second per clock read
        doc, ok = run_build(t, deadline_seconds=20, clock=lambda: next(ticks))
        self.assertTrue(ok)
        self.assertGreater(doc["stats"]["skipped_by_deadline"], 0)
        refreshed = [e for e in doc["notices"].values() if e["fetched_at"]]
        self.assertEqual(len(refreshed), doc["stats"]["refreshed"])
        self.assertLess(len(refreshed), 10)

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

    def test_incomplete_read_keeps_previous_value(self):
        doc, _ = self._run_with(supply_value=http.client.IncompleteRead(b"partial"))
        self.assert_supply_kept(doc)

    def test_bad_status_line_keeps_previous_value(self):
        doc, _ = self._run_with(supply_value=http.client.BadStatusLine("garbage"))
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
                     "schedule_calls", "list_retried", "failures", "dropped", "skipped_by_cap",
                     "skipped_by_deadline"):
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
                ["--output", str(self.out), "--sleep", "0", "--list-retry-pause", "0", *extra_args],
                transport=transport,
            )
        return code, stdout.getvalue() + stderr.getvalue()

    def test_list_retry_pause_flag_reaches_the_builder(self):
        seen = []
        real_build = build_cache.build

        def spy(previous, key, transport, now, config, **kwargs):
            seen.append(config.list_retry_pause_seconds)
            return real_build(previous, key, transport, now, config, **kwargs)

        with mock.patch.object(build_cache, "build", spy):
            self.run_main(FakeTransport(), extra_args=["--list-retry-pause", "2.5"])
        self.assertEqual(seen, [2.5])

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

    def test_mostly_failing_detail_calls_exit_nonzero_without_bumping_file(self):
        ids = [f"p{i}" for i in range(5)]
        t = FakeTransport(
            listing={("06", "공고중"): [list_body([row(i) for i in ids])]},
            supply={i: XML_ERROR for i in ids},
            detail={i: XML_ERROR for i in ids},
        )
        code, _ = self.run_main(t)
        self.assertNotEqual(code, 0)
        self.assertFalse(self.out.exists())

    def test_a_few_failures_below_the_minimum_count_still_write(self):
        t = FakeTransport(
            listing={("06", "공고중"): [list_body([row("p1")])]},
            supply={"p1": XML_ERROR},
            detail={"p1": XML_ERROR},
        )
        code, _ = self.run_main(t)
        self.assertEqual(code, 0)
        self.assertTrue(self.out.exists())

    def test_unchanged_notices_do_not_rewrite_the_file(self):
        listing = {("06", "공고중"): [list_body([row("p1")])]}
        self.assertEqual(self.run_main(FakeTransport(listing=listing))[0], 0)
        before = self.out.read_text(encoding="utf-8")
        code, logs = self.run_main(FakeTransport(listing=listing))
        self.assertEqual(code, 0)
        self.assertEqual(self.out.read_text(encoding="utf-8"), before)  # same bytes -> same ETag
        self.assertIn("unchanged", logs)

    def test_heartbeat_rewrites_unchanged_file_after_20_hours(self):
        listing = {("06", "공고중"): [list_body([row("p1")])]}
        self.run_main(FakeTransport(listing=listing))
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        old = (datetime.now(build_cache.KST) - timedelta(hours=21)).isoformat(timespec="seconds")
        doc["generated_at"] = old
        self.out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        self.run_main(FakeTransport(listing=listing))
        rewritten = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertNotEqual(rewritten["generated_at"], old)

    def test_changed_notices_rewrite_the_file(self):
        self.run_main(FakeTransport(listing={("06", "공고중"): [list_body([row("p1")])]}))
        self.run_main(FakeTransport(listing={("06", "공고중"): [list_body([row("p1"), row("p2")])]}))
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertIn("p2", doc["notices"])

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
