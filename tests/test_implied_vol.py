"""Unit tests สำหรับ pipeline/fetch_implied_vol.py (Phase 6B).

จุดที่ต้องเข้มที่สุด: BS inversion ถูกต้อง (roundtrip) และ **เครื่องหมาย
ของ risk reversal** — สลับแล้วความหมายกลับด้านทั้งระบบ
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline.fetch_implied_vol import (
    atm_iv_from_smile,
    bs_price,
    call_delta,
    compute_metrics,
    implied_vol_from_price,
    iv_at_delta,
)

NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)
CFG = {"max_quote_age_days": 5, "min_option_price": 0.05, "iv_bounds": [0.005, 0.60]}


def contract(strike, price, age_days=1):
    ts = (NOW - timedelta(days=age_days)).isoformat()
    return {"strike": strike, "last_price": price, "last_trade": ts}


def synth_chain(spot=56.5, t_years=30 / 365, sigma=0.08, strikes=(54, 55, 56, 57, 58, 59)):
    """สร้าง chain สังเคราะห์จากราคาทฤษฎีที่ σ คงที่ — inversion ต้องได้ σ คืน."""
    calls = [contract(k, bs_price(spot, k, t_years, sigma, True)) for k in strikes]
    puts = [contract(k, bs_price(spot, k, t_years, sigma, False)) for k in strikes]
    return calls, puts


# ---------------------------------------------------------------- Black-Scholes


class TestBlackScholes:
    def test_put_call_parity(self):
        s, k, t, sig = 56.5, 57.0, 30 / 365, 0.08
        c = bs_price(s, k, t, sig, True)
        p = bs_price(s, k, t, sig, False)
        assert c - p == pytest.approx(s - k, abs=1e-9)   # r = 0

    @pytest.mark.parametrize("sigma", [0.03, 0.08, 0.25])
    @pytest.mark.parametrize("strike", [54.0, 56.5, 59.0])
    def test_inversion_roundtrip(self, sigma, strike):
        s, t = 56.5, 30 / 365
        px = bs_price(s, strike, t, sigma, True)
        if px < 0.01:
            pytest.skip("ราคาต่ำเกิน inversion ไม่มีความหมาย")
        iv = implied_vol_from_price(px, s, strike, t, True)
        assert iv == pytest.approx(sigma, abs=1e-4)

    def test_price_below_intrinsic_returns_none(self):
        # call ITM: intrinsic = 2.5 แต่ราคา 2.0 → เป็นไปไม่ได้
        assert implied_vol_from_price(2.0, 56.5, 54.0, 30 / 365, True) is None

    def test_zero_price_returns_none(self):
        assert implied_vol_from_price(0.0, 56.5, 57.0, 30 / 365, True) is None

    def test_delta_monotonic_in_strike(self):
        s, t, sig = 56.5, 30 / 365, 0.08
        deltas = [call_delta(s, k, t, sig) for k in (54, 56, 58, 60)]
        assert deltas == sorted(deltas, reverse=True)     # strike สูงขึ้น delta ลดลง
        assert call_delta(s, s, t, sig) == pytest.approx(0.5, abs=0.05)


# ---------------------------------------------------------------- interpolation


class TestSmileInterp:
    def test_atm_flat_smile(self):
        smile = [(54.0, 0.08), (56.0, 0.08), (58.0, 0.08)]
        assert atm_iv_from_smile(smile, 56.5) == pytest.approx(0.08)

    def test_atm_outside_range_none(self):
        assert atm_iv_from_smile([(54.0, 0.08), (55.0, 0.08)], 60.0) is None

    def test_iv_at_25_delta_flat_smile(self):
        s, t = 56.5, 30 / 365
        smile = [(k, 0.08) for k in (55, 56, 57, 58, 59)]
        iv = iv_at_delta(smile, s, t, 0.25, is_call=True)
        assert iv == pytest.approx(0.08, abs=1e-6)        # smile แบน → IV เท่ากันทุก delta


# ---------------------------------------------------------------- metrics + sign


class TestComputeMetrics:
    def test_flat_smile_recovers_sigma_and_zero_rr(self):
        calls, puts = synth_chain(sigma=0.08)
        out = compute_metrics(calls, puts, 56.5, 30, CFG, now=NOW)
        assert out["quality"] == "ok"
        assert out["iv_atm_1m_pct"] == pytest.approx(8.0, abs=0.15)
        assert out["rr_25d_pct"] == pytest.approx(0.0, abs=0.15)

    def test_rr_sign_convention_fxy_calls_rich_gives_negative(self):
        """หัวใจของ Phase นี้: FXY call แพง (hedge เยนแข็ง) → rr_25d ต้องติดลบ.

        สร้าง smile เอียง: IV ฝั่ง call สูงกว่าฝั่ง put ชัด ๆ
        """
        s, t = 56.5, 30 / 365
        calls = [contract(k, bs_price(s, k, t, 0.10, True)) for k in (56, 57, 58, 59)]
        # หมายเหตุ: put σ=6% ที่ strike ห่าง ๆ ราคาต่ำกว่า min_option_price
        # และถูก gate กรองทิ้ง (ถูกต้อง!) — fixture จึงใช้ strike ถี่ใกล้ ATM
        puts = [contract(k, bs_price(s, k, t, 0.06, False)) for k in (55.0, 55.5, 56.0, 56.5)]
        out = compute_metrics(calls, puts, s, 30, CFG, now=NOW)
        assert out["rr_25d_pct"] is not None
        assert out["rr_25d_pct"] < -2.0                   # −(10% − 6%) ≈ −4%

    def test_stale_trades_flagged(self):
        calls, puts = synth_chain()
        old = [dict(c, last_trade=(NOW - timedelta(days=10)).isoformat()) for c in calls]
        oldp = [dict(p, last_trade=(NOW - timedelta(days=10)).isoformat()) for p in puts]
        out = compute_metrics(old, oldp, 56.5, 30, CFG, now=NOW)
        assert out["quality"] == "stale_quotes"           # invert ได้ แต่ห้ามเข้า history

    def test_junk_prices_filtered(self):
        calls = [contract(k, 0.01) for k in (54, 55, 56, 57)]   # ต่ำกว่า min_option_price
        out = compute_metrics(calls, [], 56.5, 30, CFG, now=NOW)
        assert out["quality"] == "insufficient"
        assert out["iv_atm_1m_pct"] is None

    def test_empty_chain(self):
        out = compute_metrics([], [], 56.5, 30, CFG, now=NOW)
        assert out["quality"] == "insufficient"

    def test_iv_out_of_bounds_filtered(self):
        s, t = 56.5, 30 / 365
        crazy = [contract(k, bs_price(s, k, t, 1.50, True)) for k in (55, 56, 57)]
        out = compute_metrics(crazy, [], s, 30, CFG, now=NOW)
        assert out["quality"] == "insufficient"           # 150% หลุด iv_bounds


# ---------------------------------------------------------------- CUSI fallback


class TestCusiSourceFallback:
    def test_insufficient_history_falls_back_to_realized(self):
        """สลับ config เป็น implied แต่ history สั้น → ต้องใช้ realized เหมือนเดิม."""
        import copy

        import numpy as np
        import pandas as pd

        from pipeline.config import load_config
        from pipeline.cusi import compute_components

        cfg = copy.deepcopy(load_config())
        cfg["cusi"]["fx_vol_source"] = "implied"

        dates = pd.bdate_range("2023-01-02", periods=400)
        rng = np.random.default_rng(1)
        df = pd.DataFrame({
            "usdjpy_chg_5d_pct": rng.normal(0, 0.8, 400),
            "fx_realized_vol_10d": rng.normal(8, 1.5, 400),
            "fx_implied_vol_1m": [np.nan] * 395 + [7.4] * 5,   # แค่ 5 วัน — ไม่พอ
            "vix": rng.normal(15, 2, 400),
            "vix_chg_5d": rng.normal(0, 1.5, 400),
        }, index=dates)

        comp = compute_components(df, None, cfg)
        # ถ้า (ผิด) ใช้ implied: มีข้อมูล 5 จุด z จะ NaN หมด — realized ต้องยังทำงาน
        assert comp["fx_realized_vol"].notna().sum() > 100
