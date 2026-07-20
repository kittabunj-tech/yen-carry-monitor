"""Unit tests สำหรับ backtest/build_episodes.py (Phase 6E)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.build_episodes import extract_episode


@pytest.fixture
def frame():
    """indicator frame สังเคราะห์ 200 วันทำการ พร้อมคอลัมน์พิเศษ."""
    dates = pd.bdate_range("2024-01-01", periods=200)
    rng = np.random.default_rng(5)
    close = 150 * np.exp(np.cumsum(rng.normal(0, 0.004, 200)))
    df = pd.DataFrame({
        "usdjpy": close,
        "vix": rng.normal(16, 2, 200),
        "__cusi__": np.clip(rng.normal(50, 8, 200), 0, 100),
        "__cot__": rng.normal(-80, 20, 200),
    }, index=dates)
    return df


EP = {"id": "test", "anchor": "2024-06-14", "during_days": 5,
      "title": "ทดสอบ", "desc": "-"}


class TestExtractEpisode:
    def test_window_and_offsets(self, frame):
        out = extract_episode(frame, EP, pre_days=60, post_days=40)
        assert out is not None
        assert len(out["dates"]) == 101
        assert out["day_offset"][0] == -60 and out["day_offset"][-1] == 40
        assert 0 in out["day_offset"]

    def test_price_normalized_to_100_at_day0(self, frame):
        out = extract_episode(frame, EP, 60, 40)
        i0 = out["day_offset"].index(0)
        assert out["series"]["usdjpy"][i0] == pytest.approx(100.0, abs=0.01)

    def test_raw_series_not_normalized(self, frame):
        out = extract_episode(frame, EP, 60, 40)
        i0 = out["day_offset"].index(0)
        # vix เป็นค่าดิบ — ควรอยู่แถว 16 ไม่ใช่ 100
        assert out["series"]["vix"][i0] < 50

    def test_anchor_on_weekend_snaps_backward(self, frame):
        """anchor เสาร์ → ใช้วันทำการก่อนหน้า (ศุกร์)."""
        ep = dict(EP, anchor="2024-06-15")           # เสาร์
        out = extract_episode(frame, ep, 60, 40)
        i0 = out["day_offset"].index(0)
        assert out["dates"][i0] == "2024-06-14"      # ศุกร์

    def test_anchor_before_data_returns_none(self, frame):
        assert extract_episode(frame, dict(EP, anchor="2020-01-01"), 60, 40) is None

    def test_window_clipped_at_data_edges(self, frame):
        """anchor ใกล้ต้นชุดข้อมูล → หน้าต่างสั้นลงแต่ไม่พัง."""
        ep = dict(EP, anchor="2024-01-15")
        out = extract_episode(frame, ep, 60, 40)
        assert out["day_offset"][0] > -60            # โดน clip ฝั่งซ้าย
        assert out["day_offset"][-1] == 40

    def test_peak_offset_ignores_nan(self, frame):
        """NaN ช่วงต้น (warm-up) ต้องไม่ทำให้ peak offset เพี้ยน — บั๊กที่เคยเจอจริง."""
        f = frame.copy()
        f.loc[f.index[:70], "__cusi__"] = np.nan     # จำลอง warm-up
        peak_pos = 130                                # ใส่ peak รู้ตำแหน่ง
        f.iloc[peak_pos, f.columns.get_loc("__cusi__")] = 99.0
        out = extract_episode(f, EP, 60, 40)
        anchor_pos = f.index.get_loc(pd.Timestamp("2024-06-14"))
        assert out["stats"]["cusi_peak"] == pytest.approx(99.0)
        assert out["stats"]["cusi_peak_offset"] == peak_pos - anchor_pos

    def test_phases_consistent(self, frame):
        out = extract_episode(frame, EP, 60, 40)
        p = out["phases"]
        assert p["pre"] == [-60, -1]
        assert p["during"] == [0, 5]
        assert p["post"] == [6, 40]
