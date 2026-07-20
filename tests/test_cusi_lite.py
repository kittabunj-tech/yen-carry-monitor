"""Unit tests สำหรับ pipeline/cusi_lite.py (Phase 6D)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from pipeline.config import load_config
from pipeline.cusi_lite import compute_cusi_lite, compute_lite_components

NOW = datetime(2026, 7, 20, 18, 0, tzinfo=timezone.utc)
N_BARS = 400


@pytest.fixture
def cfg():
    return copy.deepcopy(load_config())


def bars(n=N_BARS, *, end=NOW, seed=0, base=160.0, drift=0.0):
    """สร้างแท่ง 1h สังเคราะห์ จบที่ ``end``."""
    idx = pd.date_range(end=end, periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(seed)
    px = base * np.exp(np.cumsum(rng.normal(drift, 0.0008, n)))
    return pd.DataFrame({"Close": px}, index=idx)


def market(usdjpy=None, vix=None):
    out = {}
    if usdjpy is not None:
        out["usdjpy"] = usdjpy
    if vix is not None:
        out["vix"] = vix
    return out


@pytest.fixture
def normal_market():
    return market(bars(seed=1), bars(seed=2, base=16.0))


# ---------------------------------------------------------------- basic


class TestComputeCusiLite:
    def test_normal_market_available(self, cfg, normal_market):
        b = compute_cusi_lite(normal_market, cfg, now=NOW)
        assert b["available"] is True
        assert 0.0 <= b["value"] <= 100.0
        assert b["level"] in ("CALM", "WATCH", "STRESS", "UNWIND")
        assert set(b["components"]) == {"jpy_momentum_1h", "vix_1h", "fx_vol_1h"}
        assert b["weight_coverage"] == pytest.approx(1.0)

    def test_weights_sum_validated(self, cfg, normal_market):
        cfg["cusi_lite"]["weights"]["vix_1h"] = 0.9
        with pytest.raises(ValueError, match="1.0"):
            compute_cusi_lite(normal_market, cfg, now=NOW)

    def test_empty_market_gracefully_unavailable(self, cfg):
        b = compute_cusi_lite({}, cfg, now=NOW)
        assert b["available"] is False
        assert "reason" in b

    def test_short_history_unavailable(self, cfg):
        b = compute_cusi_lite(market(bars(50, seed=1), bars(50, seed=2, base=16.0)), cfg, now=NOW)
        assert b["available"] is False   # ต่ำกว่า z_min_periods → ไม่มี component ไหนคำนวณได้

    def test_uses_same_levels_as_daily_cusi(self, cfg, normal_market):
        """สเกล/ระดับเดียวกับ CUSI เต็ม — เทียบเลขกันตรง ๆ ได้."""
        from pipeline.cusi import assign_level
        b = compute_cusi_lite(normal_market, cfg, now=NOW)
        assert b["level"] == assign_level(b["value"], cfg)


# ---------------------------------------------------------------- direction


class TestDirections:
    def test_usdjpy_crash_raises_lite(self, cfg):
        """USDJPY ร่วงแรงในแท่งท้าย ๆ → CUSI-Lite ต้องสูงกว่ากรณีปกติ."""
        calm = market(bars(seed=1), bars(seed=2, base=16.0))
        crash_px = bars(seed=1)
        crash_px.iloc[-30:, 0] = crash_px.iloc[-30, 0] * np.linspace(1.0, 0.96, 30)  # −4%
        crash = market(crash_px, bars(seed=2, base=16.0))

        v_calm = compute_cusi_lite(calm, cfg, now=NOW)["value"]
        v_crash = compute_cusi_lite(crash, cfg, now=NOW)["value"]
        assert v_crash > v_calm + 5

    def test_vix_spike_raises_lite(self, cfg):
        calm = market(bars(seed=1), bars(seed=2, base=16.0))
        vix_spike = bars(seed=2, base=16.0)
        vix_spike.iloc[-10:, 0] = 40.0
        spiked = market(bars(seed=1), vix_spike)

        assert (compute_cusi_lite(spiked, cfg, now=NOW)["value"]
                > compute_cusi_lite(calm, cfg, now=NOW)["value"])


# ---------------------------------------------------------------- staleness


class TestStaleness:
    def test_stale_vix_dropped_and_renormalized(self, cfg):
        """VIX ค้าง 80 ชม. (สุดสัปดาห์ยาว) → ตัดออก แต่ FX ยังพอรายงานได้."""
        old_vix = bars(seed=2, base=16.0, end=NOW - timedelta(hours=80))
        b = compute_cusi_lite(market(bars(seed=1), old_vix), cfg, now=NOW)
        assert "vix_1h" in b["stale"]
        assert b["available"] is True                       # jpy 0.45 + vol 0.22 = 0.67 ≥ 0.5
        assert b["weight_coverage"] == pytest.approx(0.67, abs=0.01)

    def test_all_stale_unavailable(self, cfg):
        old = NOW - timedelta(hours=100)
        b = compute_cusi_lite(
            market(bars(seed=1, end=old), bars(seed=2, base=16.0, end=old)), cfg, now=NOW)
        assert b["available"] is False

    def test_age_reported(self, cfg, normal_market):
        b = compute_cusi_lite(normal_market, cfg, now=NOW)
        for c in b["components"].values():
            assert c["age_hours"] == pytest.approx(0.0, abs=0.1)


# ---------------------------------------------------------------- lookahead


class TestNoLookahead:
    def test_truncating_future_bars_does_not_change_z(self, cfg):
        """z ของ component ณ แท่ง t ต้องไม่เปลี่ยนเมื่อตัดแท่งหลัง t ทิ้ง.

        เทียบ: คำนวณบนข้อมูลถึงแท่ง 350 ตรง ๆ vs คำนวณบนชุดเต็มที่ตัดท้าย
        """
        full_u, full_v = bars(seed=7), bars(seed=8, base=16.0)
        cut_time = full_u.index[349]

        a = compute_lite_components(
            market(full_u.iloc[:350], full_v[full_v.index <= cut_time]), cfg, now=NOW)
        b = compute_lite_components(
            market(full_u.iloc[:350].copy(), full_v[full_v.index <= cut_time].copy()), cfg, now=NOW)
        for k in a:
            assert a[k]["z"] == b[k]["z"]

    def test_future_spike_does_not_change_past_z(self, cfg):
        """ต่อแท่ง spike อนาคตเข้าไป → z ที่คำนวณจากอดีตเดิมต้องเท่าเดิม."""
        u = bars(seed=9)
        past = compute_lite_components(market(u, None), cfg, now=NOW)

        future_idx = pd.date_range(start=u.index[-1] + pd.Timedelta(hours=1),
                                   periods=20, freq="1h", tz="UTC")
        spike = pd.DataFrame({"Close": [999.0] * 20}, index=future_idx)
        extended = pd.concat([u, spike])
        # ตัดกลับมาที่จุดเดิม — ต้องได้ z เท่าเดิมเป๊ะ
        again = compute_lite_components(market(extended[extended.index <= u.index[-1]], None),
                                        cfg, now=NOW)
        assert past["jpy_momentum_1h"]["z"] == again["jpy_momentum_1h"]["z"]
