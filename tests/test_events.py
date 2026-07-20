"""Unit tests สำหรับ pipeline/fetch_events.py (Phase 6C).

Fixture HTML จำลองโครงสร้างจริงของหน้า BOJ/Fed (ตรวจสอบกับของจริง 2026-07)
"""

from __future__ import annotations

import copy
from datetime import date

import pytest

from pipeline.config import load_config
from pipeline.fetch_events import (
    build_events_block,
    japan_cpi_events,
    parse_boj,
    parse_fomc,
    third_friday,
)

# ---------------------------------------------------------------- fixtures

#: จำลองหน้า BOJ: การประชุมเป็นคู่สองวัน / แถววันเดียวคือ minutes (ต้องไม่ถูกนับ)
BOJ_HTML = """
<h3 id="p2026">2026</h3>
<table><tr><td>Jan. 22 (Thurs.), 23 (Fri.)</td><td>Feb. 2 (Mon.) [PDF]</td></tr>
<tr><td>Apr. 30 (Thurs.), May 1 (Fri.)</td><td>May 12 (Tues.) [PDF]</td></tr>
<tr><td>July 30 (Thurs.), 31 (Fri.)</td><td></td></tr></table>
<h3 id="p2025">2025</h3>
<table><tr><td>Dec. 18 (Thurs.), 19 (Fri.)</td><td>Dec. 24 (Wed.) [PDF]</td></tr></table>
"""

#: จำลองหน้า Fed: section ต่อปี + month/date divs (มี * และ unscheduled)
FOMC_HTML = """
<h4>2026 FOMC Meetings</h4>
<div class="fomc-meeting__month col"><strong>January</strong></div>
<div class="fomc-meeting__date col">27-28</div>
<div class="fomc-meeting__month col"><strong>April/May</strong></div>
<div class="fomc-meeting__date col">30-1</div>
<div class="fomc-meeting__month col"><strong>March</strong></div>
<div class="fomc-meeting__date col">17-18*</div>
<h4>2025 FOMC Meetings</h4>
<div class="fomc-meeting__month col"><strong>December</strong></div>
<div class="fomc-meeting__date col">9-10 (unscheduled)</div>
"""


@pytest.fixture
def cfg():
    return copy.deepcopy(load_config())


def make_payload(events):
    return {"generated_at": "2026-07-20T00:00:00+00:00", "sources": {}, "events": events}


def ev(type_, start, end, approx=False):
    return {"type": type_, "label": type_, "start_date": start, "date": end,
            "approx": approx, "source": "test"}


# ---------------------------------------------------------------- parsers


class TestParseBoj:
    def test_finds_two_day_meetings_only(self):
        out = parse_boj(BOJ_HTML)
        dates = {e["date"] for e in out}
        # การประชุมจริง 4 ครั้ง — วันที่ = วันที่สอง
        assert dates == {"2026-01-23", "2026-05-01", "2026-07-31", "2025-12-19"}

    def test_single_day_minutes_rows_excluded(self):
        out = parse_boj(BOJ_HTML)
        assert not any(e["date"] in ("2026-02-02", "2026-05-12", "2025-12-24") for e in out)

    def test_cross_month_meeting(self):
        out = parse_boj(BOJ_HTML)
        apr = [e for e in out if e["date"] == "2026-05-01"][0]
        assert apr["start_date"] == "2026-04-30"

    def test_year_from_section(self):
        out = parse_boj(BOJ_HTML)
        assert any(e["date"].startswith("2025") for e in out)

    def test_empty_html(self):
        assert parse_boj("<html></html>") == []


class TestParseFomc:
    def test_basic_and_starred(self):
        out = parse_fomc(FOMC_HTML)
        dates = {e["date"] for e in out}
        assert "2026-01-28" in dates
        assert "2026-03-18" in dates          # เครื่องหมาย * ต้องถูกตัดทิ้ง

    def test_cross_month_april_may(self):
        out = parse_fomc(FOMC_HTML)
        m = [e for e in out if e["date"] == "2026-05-01"][0]
        assert m["start_date"] == "2026-04-30"

    def test_unscheduled_excluded(self):
        out = parse_fomc(FOMC_HTML)
        assert not any(e["date"].startswith("2025-12") for e in out)

    def test_empty_html(self):
        assert parse_fomc("") == []


# ---------------------------------------------------------------- CPI rule


class TestJapanCpi:
    def test_third_friday_known_values(self):
        assert third_friday(2026, 7) == date(2026, 7, 17)
        assert third_friday(2026, 8) == date(2026, 8, 21)

    def test_rule_events_flagged_approx(self, cfg):
        out = japan_cpi_events(cfg["events"], date(2026, 7, 20), months_ahead=2)
        assert all(e["approx"] for e in out if e["source"].startswith("rule"))

    def test_override_replaces_rule_month(self, cfg):
        ev_cfg = dict(cfg["events"], jp_cpi_overrides=["2026-08-19"])
        out = japan_cpi_events(ev_cfg, date(2026, 7, 20), months_ahead=2)
        aug = [e for e in out if e["date"].startswith("2026-08")]
        assert len(aug) == 1
        assert aug[0]["date"] == "2026-08-19"
        assert aug[0]["approx"] is False       # วัน pin = วันจริง


# ---------------------------------------------------------------- risk window


class TestEventsBlock:
    def test_risk_inactive_before_window(self, cfg):
        p = make_payload([ev("fomc", "2026-07-28", "2026-07-29")])
        b = build_events_block(p, cfg, today=date(2026, 7, 20))
        assert b["risk_active"] is False

    @pytest.mark.parametrize("today", [
        date(2026, 7, 25),   # 3 วันก่อนเริ่ม (ขอบหน้าต่าง)
        date(2026, 7, 28),   # วันเริ่มประชุม
        date(2026, 7, 29),   # วันแถลงผล
    ])
    def test_risk_active_in_window(self, cfg, today):
        p = make_payload([ev("fomc", "2026-07-28", "2026-07-29")])
        assert build_events_block(p, cfg, today=today)["risk_active"] is True

    def test_risk_clears_after_event(self, cfg):
        p = make_payload([ev("fomc", "2026-07-28", "2026-07-29")])
        assert build_events_block(p, cfg, today=date(2026, 7, 30))["risk_active"] is False

    def test_cpi_never_raises_flag(self, cfg):
        p = make_payload([ev("jp_cpi", "2026-07-21", "2026-07-21", approx=True)])
        b = build_events_block(p, cfg, today=date(2026, 7, 20))
        assert b["risk_active"] is False       # risk_types มีแค่ boj_mpm/fomc

    def test_past_events_excluded_from_upcoming(self, cfg):
        p = make_payload([ev("fomc", "2026-06-16", "2026-06-17"),
                          ev("fomc", "2026-07-28", "2026-07-29")])
        b = build_events_block(p, cfg, today=date(2026, 7, 20))
        assert [e["date"] for e in b["upcoming"]] == ["2026-07-29"]

    def test_days_until_counts_to_start(self, cfg):
        p = make_payload([ev("boj_mpm", "2026-07-30", "2026-07-31")])
        b = build_events_block(p, cfg, today=date(2026, 7, 20))
        assert b["upcoming"][0]["days_until"] == 10

    def test_upcoming_capped(self, cfg):
        events = [ev("fomc", f"2026-{m:02d}-10", f"2026-{m:02d}-11") for m in range(8, 13)]
        events += [ev("boj_mpm", "2027-01-10", "2027-01-11")]
        b = build_events_block(p := make_payload(events), cfg, today=date(2026, 7, 20))
        assert len(b["upcoming"]) <= int(cfg["events"]["upcoming_count"])

    def test_empty_payload(self, cfg):
        b = build_events_block(make_payload([]), cfg, today=date(2026, 7, 20))
        assert b["risk_active"] is False and b["upcoming"] == []
