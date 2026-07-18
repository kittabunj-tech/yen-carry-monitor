"""Unit tests สำหรับ pipeline/cusi.py (spec §3).

ครอบ:
  * weights รวมกัน = 1.0 และ validate ค่าผิดรูป
  * CUSI อยู่ใน [0, 100] เสมอ
  * synthetic "unwind" → CUSI > 75 / synthetic "calm" → CUSI < 40
  * COT forward-fill ไม่ leak อนาคต (จุดอันตรายที่สุดของโปรเจกต์ตาม spec §6)
  * ทิศทางเครื่องหมายของทุก component ("ยิ่งสูง = ยิ่งเสี่ยง")
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import numpy as np
import pandas as pd
import pytest

from pipeline.config import load_config
from pipeline.cusi import (
    COMPONENT_ORDER,
    align_cot_daily,
    assign_level,
    assign_levels,
    build_cusi_frame,
    build_cusi_history,
    compute_components,
    compute_cusi,
    contribution_breakdown,
    load_weights,
)

N_DAYS = 500  # ยาวพอให้ z-score window 252 วันอุ่นเครื่องเสร็จ


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def cfg() -> Dict[str, Any]:
    """config จริงจาก config.yaml (deep copy — เทสต์แก้ได้ไม่กระทบตัวอื่น)."""
    return copy.deepcopy(load_config())


@pytest.fixture
def weights(cfg: Dict[str, Any]) -> Dict[str, float]:
    """น้ำหนัก 9 components จาก config."""
    return load_weights(cfg)


@pytest.fixture
def dates() -> pd.DatetimeIndex:
    """ปฏิทินวันทำการ."""
    return pd.bdate_range("2023-01-02", periods=N_DAYS)


def make_indicator_frame(dates: pd.DatetimeIndex, *, seed: int = 0) -> pd.DataFrame:
    """สร้าง indicator frame สังเคราะห์ในสภาวะ "ปกติ" (noise รอบค่ากลางที่สมจริง).

    Args:
        dates: ปฏิทินเป้าหมาย
        seed: seed ของ RNG

    Returns:
        DataFrame ที่มีคอลัมน์ต้นทางครบทุก component
    """
    rng = np.random.default_rng(seed)
    n = len(dates)
    return pd.DataFrame(
        {
            "usdjpy_chg_5d_pct": rng.normal(0.0, 0.8, n),
            "audjpy_dd_pct": -np.abs(rng.normal(1.0, 0.6, n)),
            "spread_chg_20d_bps": rng.normal(0.0, 8.0, n),
            "vix": rng.normal(15.0, 2.0, n),
            "vix_chg_5d": rng.normal(0.0, 1.5, n),
            "fx_realized_vol_10d": rng.normal(8.0, 1.5, n),
            "move": rng.normal(95.0, 8.0, n),
            "nikkei_chg_5d_pct": rng.normal(0.0, 1.5, n),
            "mxnjpy_dd_pct": -np.abs(rng.normal(1.0, 0.6, n)),
        },
        index=dates,
    )


def make_cot_frame(dates: pd.DatetimeIndex, *, seed: int = 0, tail_net: float | None = None) -> pd.DataFrame:
    """สร้าง COT weekly สังเคราะห์ (รายงานวันอังคาร, ประกาศวันศุกร์).

    Args:
        dates: ปฏิทินรายวันที่ต้องครอบคลุม
        seed: seed ของ RNG
        tail_net: ถ้าระบุ จะบังคับค่า net ของ 8 สัปดาห์สุดท้ายให้เป็นค่านี้
            (ใช้จำลอง short crowding สุดขั้ว)

    Returns:
        DataFrame ที่มี report_date / available_from / net_speculative
    """
    rng = np.random.default_rng(seed)
    tuesdays = pd.date_range(dates[0], dates[-1], freq="W-TUE")
    net = rng.normal(-40_000, 15_000, len(tuesdays))
    if tail_net is not None:
        net[-8:] = tail_net
    return pd.DataFrame(
        {
            "report_date": tuesdays,
            "available_from": tuesdays + pd.Timedelta(days=3),  # ศุกร์
            "net_speculative": net,
            "noncomm_long": 100_000.0,
            "noncomm_short": 100_000.0 - net,
        }
    )


@pytest.fixture
def calm_frame(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """indicator frame สภาวะปกติ."""
    return make_indicator_frame(dates)


@pytest.fixture
def cot_frame(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """COT สภาวะปกติ."""
    return make_cot_frame(dates)


def apply_unwind_shock(df: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """ยัดสถานการณ์ unwind ตาม spec ลงใน ``n`` วันสุดท้าย.

    สถานการณ์: USDJPY −5% ใน 5 วัน, VIX 40, spread −30bps
    บวกกับ component อื่นที่ขยับไปทางเสี่ยงพร้อมกัน (ตามที่เกิดจริง ส.ค. 2024)

    Args:
        df: indicator frame ตั้งต้น
        n: จำนวนวันสุดท้ายที่ยัด shock

    Returns:
        DataFrame ใหม่ (ไม่แก้ของเดิม)
    """
    out = df.copy()
    tail = out.index[-n:]
    out.loc[tail, "usdjpy_chg_5d_pct"] = -5.0
    out.loc[tail, "vix"] = 40.0
    out.loc[tail, "vix_chg_5d"] = 22.0
    out.loc[tail, "spread_chg_20d_bps"] = -30.0
    out.loc[tail, "audjpy_dd_pct"] = -8.0
    out.loc[tail, "mxnjpy_dd_pct"] = -10.0
    out.loc[tail, "nikkei_chg_5d_pct"] = -12.0
    out.loc[tail, "fx_realized_vol_10d"] = 22.0
    out.loc[tail, "move"] = 160.0
    return out


# ================================================================ weights


class TestWeights:
    def test_sum_to_one(self, weights: Dict[str, float]) -> None:
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_matches_spec_table(self, weights: Dict[str, float]) -> None:
        """น้ำหนักต้องตรงตามตาราง §3.2 เป๊ะ ๆ."""
        expected = {
            "jpy_momentum": 0.20,
            "audjpy_drawdown": 0.15,
            "spread_compression": 0.15,
            "vix": 0.15,
            "fx_realized_vol": 0.10,
            "cot_positioning": 0.10,
            "move": 0.05,
            "nikkei_momentum": 0.05,
            "mxnjpy_drawdown": 0.05,
        }
        assert weights == pytest.approx(expected)

    def test_all_nine_components_present(self, weights: Dict[str, float]) -> None:
        assert set(weights) == set(COMPONENT_ORDER)
        assert len(weights) == 9

    def test_rejects_sum_not_one(self, cfg: Dict[str, Any]) -> None:
        cfg["cusi"]["weights"]["vix"] = 0.99
        with pytest.raises(ValueError, match="sum to 1.0"):
            load_weights(cfg)

    def test_rejects_negative_weight(self, cfg: Dict[str, Any]) -> None:
        cfg["cusi"]["weights"]["vix"] = -0.15
        cfg["cusi"]["weights"]["move"] = 0.35
        with pytest.raises(ValueError, match="non-negative"):
            load_weights(cfg)

    def test_rejects_unknown_component(self, cfg: Dict[str, Any]) -> None:
        cfg["cusi"]["weights"]["bitcoin_vibes"] = 0.0
        with pytest.raises(ValueError, match="unknown"):
            load_weights(cfg)

    def test_rejects_missing_component(self, cfg: Dict[str, Any]) -> None:
        del cfg["cusi"]["weights"]["move"]
        cfg["cusi"]["weights"]["vix"] = 0.20
        with pytest.raises(ValueError, match="missing"):
            load_weights(cfg)


# ================================================================ components


class TestComputeComponents:
    def test_returns_all_nine_columns(self, calm_frame, cot_frame, cfg) -> None:
        comp = compute_components(calm_frame, cot_frame, cfg)
        assert list(comp.columns) == COMPONENT_ORDER

    def test_index_preserved(self, calm_frame, cot_frame, cfg) -> None:
        comp = compute_components(calm_frame, cot_frame, cfg)
        assert comp.index.equals(calm_frame.index)

    def test_z_scores_are_clipped(self, calm_frame, cot_frame, cfg) -> None:
        comp = compute_components(calm_frame, cot_frame, cfg)
        clip = float(cfg["parameters"]["z_clip"])
        assert comp.abs().max().max() <= clip + 1e-9

    def test_missing_column_gives_nan_not_crash(self, calm_frame, cot_frame, cfg) -> None:
        comp = compute_components(calm_frame.drop(columns="move"), cot_frame, cfg)
        assert comp["move"].isna().all()
        assert comp["vix"].notna().sum() > 0  # ตัวอื่นยังทำงาน

    def test_no_cot_gives_nan_component(self, calm_frame, cfg) -> None:
        comp = compute_components(calm_frame, None, cfg)
        assert comp["cot_positioning"].isna().all()

    def test_empty_input_returns_empty(self, cfg) -> None:
        assert compute_components(pd.DataFrame(), None, cfg).empty

    def test_vix_is_average_of_level_and_change(self, calm_frame, cot_frame, cfg) -> None:
        """§3.2: VIX component = 0.5×z(level) + 0.5×z(5d change)."""
        from pipeline.indicators import rolling_z

        comp = compute_components(calm_frame, cot_frame, cfg)
        z_win = int(cfg["parameters"]["z_window"])
        z_clip = float(cfg["parameters"]["z_clip"])
        expected = 0.5 * rolling_z(calm_frame["vix"], z_win, z_clip) + 0.5 * rolling_z(
            calm_frame["vix_chg_5d"], z_win, z_clip
        )
        pd.testing.assert_series_equal(
            comp["vix"].dropna(), expected.dropna(), check_names=False, rtol=1e-9
        )


class TestComponentDirections:
    """§3.2: ทุก component ต้อง "ยิ่งสูง = ยิ่งเสี่ยง" หลังปรับเครื่องหมายแล้ว."""

    @pytest.mark.parametrize(
        "column,shock,component",
        [
            ("usdjpy_chg_5d_pct", -5.0, "jpy_momentum"),      # USDJPY ร่วง
            ("audjpy_dd_pct", -8.0, "audjpy_drawdown"),        # drawdown ลึก
            ("spread_chg_20d_bps", -30.0, "spread_compression"),  # spread แคบลง
            ("nikkei_chg_5d_pct", -12.0, "nikkei_momentum"),   # Nikkei ร่วง
            ("mxnjpy_dd_pct", -10.0, "mxnjpy_drawdown"),       # canary ร่วง
        ],
    )
    def test_falling_inputs_raise_risk(self, calm_frame, cot_frame, cfg, column, shock, component) -> None:
        shocked = calm_frame.copy()
        shocked.loc[shocked.index[-3:], column] = shock
        comp = compute_components(shocked, cot_frame, cfg)
        assert comp[component].iloc[-1] > 1.0, f"{component} ควรเป็นบวกมากเมื่อ {column} ร่วง"

    @pytest.mark.parametrize(
        "column,shock,component",
        [
            ("vix", 40.0, "vix"),
            ("fx_realized_vol_10d", 22.0, "fx_realized_vol"),
            ("move", 160.0, "move"),
        ],
    )
    def test_rising_inputs_raise_risk(self, calm_frame, cot_frame, cfg, column, shock, component) -> None:
        shocked = calm_frame.copy()
        shocked.loc[shocked.index[-3:], column] = shock
        comp = compute_components(shocked, cot_frame, cfg)
        assert comp[component].iloc[-1] > 1.0, f"{component} ควรเป็นบวกมากเมื่อ {column} พุ่ง"

    def test_deeper_short_raises_cot_risk(self, calm_frame, dates, cfg) -> None:
        """COT net ติดลบลึก (short หนาแน่น) = เชื้อเพลิงเยอะ = z สูง."""
        crowded = make_cot_frame(dates, tail_net=-150_000.0)
        comp = compute_components(calm_frame, crowded, cfg)
        assert comp["cot_positioning"].iloc[-1] > 1.0

    def test_sign_flip_equals_negated_z(self, calm_frame, cot_frame, cfg) -> None:
        """z(-x) ≡ -z(x): กลับเครื่องหมายก่อนหรือหลังคำนวณ z ต้องได้ผลเท่ากัน."""
        from pipeline.indicators import rolling_z

        comp = compute_components(calm_frame, cot_frame, cfg)
        z_win = int(cfg["parameters"]["z_window"])
        z_clip = float(cfg["parameters"]["z_clip"])
        flipped_first = rolling_z(-calm_frame["usdjpy_chg_5d_pct"], z_win, z_clip)
        pd.testing.assert_series_equal(
            comp["jpy_momentum"].dropna(), flipped_first.dropna(), check_names=False, rtol=1e-9
        )


# ================================================================ CUSI range


class TestComputeCusi:
    def test_always_within_0_100(self, calm_frame, cot_frame, cfg, weights) -> None:
        comp = compute_components(calm_frame, cot_frame, cfg)
        cusi = compute_cusi(comp, weights, cfg).dropna()
        assert len(cusi) > 0
        assert cusi.min() >= 0.0
        assert cusi.max() <= 100.0

    def test_extreme_inputs_still_bounded(self, calm_frame, cot_frame, cfg, weights) -> None:
        """แม้ยัดค่าสุดขั้วทุก component ก็ต้องไม่หลุด [0,100]."""
        for direction in (+1, -1):
            wild = calm_frame.copy()
            wild.iloc[-10:] = wild.iloc[-10:] * 0 + direction * 1e6
            comp = compute_components(wild, cot_frame, cfg)
            cusi = compute_cusi(comp, weights, cfg).dropna()
            assert cusi.min() >= 0.0 and cusi.max() <= 100.0

    def test_neutral_components_give_50(self, dates, cfg, weights) -> None:
        """z = 0 ทุกตัว → CUSI = center = 50 พอดี (สูตร §3.2)."""
        comp = pd.DataFrame(0.0, index=dates, columns=COMPONENT_ORDER)
        assert compute_cusi(comp, weights, cfg).iloc[-1] == pytest.approx(50.0)

    def test_formula_matches_spec(self, dates, cfg, weights) -> None:
        """CUSI = 50 + (weighted_z / 3) × 50."""
        comp = pd.DataFrame(1.5, index=dates, columns=COMPONENT_ORDER)
        assert compute_cusi(comp, weights, cfg).iloc[-1] == pytest.approx(50 + (1.5 / 3) * 50)

    def test_max_z_gives_100(self, dates, cfg, weights) -> None:
        comp = pd.DataFrame(3.0, index=dates, columns=COMPONENT_ORDER)
        assert compute_cusi(comp, weights, cfg).iloc[-1] == pytest.approx(100.0)

    def test_min_z_gives_0(self, dates, cfg, weights) -> None:
        comp = pd.DataFrame(-3.0, index=dates, columns=COMPONENT_ORDER)
        assert compute_cusi(comp, weights, cfg).iloc[-1] == pytest.approx(0.0)

    def test_empty_components(self, cfg, weights) -> None:
        assert compute_cusi(pd.DataFrame(), weights, cfg).empty

    def test_all_nan_row_gives_nan(self, dates, cfg, weights) -> None:
        comp = pd.DataFrame(np.nan, index=dates, columns=COMPONENT_ORDER)
        assert compute_cusi(comp, weights, cfg).isna().all()


class TestWeightRenormalisation:
    """component ที่ขาดต้อง re-normalize ไม่ใช่ถือว่า z = 0."""

    def test_missing_component_renormalises(self, dates, cfg, weights) -> None:
        comp = pd.DataFrame(2.0, index=dates, columns=COMPONENT_ORDER)
        comp["cot_positioning"] = np.nan  # น้ำหนัก 10% หายไป
        # ตัวที่เหลือเป็น 2.0 ทั้งหมด → weighted_z ต้องยังเป็น 2.0 ไม่ใช่ 1.8
        assert compute_cusi(comp, weights, cfg).iloc[-1] == pytest.approx(50 + (2.0 / 3) * 50)

    def test_below_coverage_threshold_gives_nan(self, dates, cfg, weights) -> None:
        comp = pd.DataFrame(np.nan, index=dates, columns=COMPONENT_ORDER)
        comp["move"] = 2.0  # เหลือน้ำหนักแค่ 5% < min_weight_coverage 0.5
        assert compute_cusi(comp, weights, cfg).isna().all()

    def test_above_coverage_threshold_computes(self, dates, cfg, weights) -> None:
        comp = pd.DataFrame(np.nan, index=dates, columns=COMPONENT_ORDER)
        # jpy 0.20 + audjpy 0.15 + spread 0.15 + vix 0.15 = 0.65 >= 0.5
        for c in ("jpy_momentum", "audjpy_drawdown", "spread_compression", "vix"):
            comp[c] = 1.0
        assert compute_cusi(comp, weights, cfg).notna().all()


# ================================================================ synthetic scenarios


class TestSyntheticScenarios:
    """เทสต์ตามที่ spec Phase 2 กำหนด: unwind > 75, calm < 40."""

    def test_unwind_scenario_exceeds_75(self, calm_frame, dates, cfg, weights) -> None:
        """USDJPY −5% ใน 5 วัน + VIX 40 + spread −30bps + short crowded → CUSI > 75."""
        shocked = apply_unwind_shock(calm_frame)
        crowded = make_cot_frame(dates, tail_net=-150_000.0)
        comp = compute_components(shocked, crowded, cfg)
        cusi = compute_cusi(comp, weights, cfg)
        assert cusi.iloc[-1] > 75.0, f"unwind scenario ได้ CUSI = {cusi.iloc[-1]:.1f} (ต้อง > 75)"

    def test_unwind_scenario_reaches_stress_or_unwind_level(self, calm_frame, dates, cfg, weights) -> None:
        shocked = apply_unwind_shock(calm_frame)
        crowded = make_cot_frame(dates, tail_net=-150_000.0)
        comp = compute_components(shocked, crowded, cfg)
        level = assign_level(compute_cusi(comp, weights, cfg).iloc[-1], cfg)
        assert level in ("STRESS", "UNWIND")

    def test_calm_scenario_below_40(self, calm_frame, dates, cfg, weights) -> None:
        """ตลาดสงบผิดปกติ: JPY อ่อนค่านิ่ง ๆ, VIX ต่ำ, spread กว้างขึ้น → CUSI < 40."""
        calm = calm_frame.copy()
        tail = calm.index[-5:]
        calm.loc[tail, "usdjpy_chg_5d_pct"] = 2.0      # USDJPY ขึ้น = JPY อ่อน = carry สบาย
        calm.loc[tail, "vix"] = 10.0
        calm.loc[tail, "vix_chg_5d"] = -4.0
        calm.loc[tail, "spread_chg_20d_bps"] = 25.0     # spread กว้างขึ้น
        calm.loc[tail, "audjpy_dd_pct"] = 0.0           # ทำ new high
        calm.loc[tail, "mxnjpy_dd_pct"] = 0.0
        calm.loc[tail, "nikkei_chg_5d_pct"] = 3.0
        calm.loc[tail, "fx_realized_vol_10d"] = 4.0
        calm.loc[tail, "move"] = 70.0

        quiet_cot = make_cot_frame(dates, tail_net=10_000.0)  # net long = ไม่มีเชื้อเพลิง
        comp = compute_components(calm, quiet_cot, cfg)
        cusi = compute_cusi(comp, weights, cfg)
        assert cusi.iloc[-1] < 40.0, f"calm scenario ได้ CUSI = {cusi.iloc[-1]:.1f} (ต้อง < 40)"

    def test_unwind_is_higher_than_calm(self, calm_frame, dates, cfg, weights) -> None:
        crowded = make_cot_frame(dates, tail_net=-150_000.0)
        unwind = compute_cusi(compute_components(apply_unwind_shock(calm_frame), crowded, cfg), weights, cfg)
        base = compute_cusi(compute_components(calm_frame, crowded, cfg), weights, cfg)
        assert unwind.iloc[-1] > base.iloc[-1] + 20


# ================================================================ levels


class TestAssignLevel:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (0.0, "CALM"),
            (20.0, "CALM"),
            (39.9, "CALM"),
            (40.0, "WATCH"),      # ขอบล่างรวม
            (50.0, "WATCH"),
            (59.9, "WATCH"),
            (60.0, "STRESS"),
            (79.9, "STRESS"),
            (80.0, "UNWIND"),
            (100.0, "UNWIND"),
        ],
    )
    def test_thresholds_match_spec(self, value: float, expected: str, cfg) -> None:
        assert assign_level(value, cfg) == expected

    @pytest.mark.parametrize("bad", [None, np.nan, float("inf"), "abc"])
    def test_invalid_values_give_none(self, bad, cfg) -> None:
        assert assign_level(bad, cfg) is None

    def test_vectorised_matches_scalar(self, dates, cfg) -> None:
        values = pd.Series(np.linspace(0, 100, len(dates)), index=dates)
        levels = assign_levels(values, cfg)
        assert all(levels.iloc[i] == assign_level(values.iloc[i], cfg) for i in range(0, len(values), 25))

    def test_all_four_levels_reachable(self, cfg) -> None:
        got = {assign_level(v, cfg) for v in (10, 50, 70, 90)}
        assert got == {"CALM", "WATCH", "STRESS", "UNWIND"}


# ================================================================ contributions


class TestContributionBreakdown:
    def test_contribution_is_weight_times_z(self, dates, weights) -> None:
        """ตาม §4.4: z 0.8 × weight 0.20 = contribution 0.16."""
        comp = pd.DataFrame(0.0, index=dates, columns=COMPONENT_ORDER)
        comp["jpy_momentum"] = 0.8
        out = contribution_breakdown(comp, weights)
        assert out["jpy_momentum"]["contribution"] == pytest.approx(0.16)
        assert out["jpy_momentum"]["weight"] == pytest.approx(0.20)
        assert out["jpy_momentum"]["z"] == pytest.approx(0.8)

    def test_sorted_descending(self, calm_frame, cot_frame, cfg, weights) -> None:
        comp = compute_components(calm_frame, cot_frame, cfg)
        contribs = [v["contribution"] for v in contribution_breakdown(comp, weights).values()]
        assert contribs == sorted(contribs, reverse=True)

    def test_shares_sum_to_100(self, calm_frame, cot_frame, cfg, weights) -> None:
        comp = compute_components(calm_frame, cot_frame, cfg)
        shares = [v["share_pct"] for v in contribution_breakdown(comp, weights).values()]
        assert sum(shares) == pytest.approx(100.0, abs=0.5)

    def test_nan_components_excluded(self, dates, weights) -> None:
        comp = pd.DataFrame(1.0, index=dates, columns=COMPONENT_ORDER)
        comp["move"] = np.nan
        assert "move" not in contribution_breakdown(comp, weights)

    def test_empty_frame(self, weights) -> None:
        assert contribution_breakdown(pd.DataFrame(), weights) == {}


# ================================================================ COT lookahead


class TestCotNoLookahead:
    """spec §6: จุดที่ต้องระวังที่สุดของทั้งโปรเจกต์.

    ข้อมูล COT วัดวันอังคาร แต่ประกาศบ่ายวันศุกร์ → ห้ามใช้ก่อนวันศุกร์
    """

    def test_value_not_available_before_release(self, dates, cfg) -> None:
        """ช่วงอังคาร→พฤหัส ต้องยังเห็นค่าของรายงาน *ก่อนหน้า* เท่านั้น."""
        cot = make_cot_frame(dates)
        aligned = align_cot_daily(cot, dates, cfg)

        last_report = pd.Timestamp(cot["report_date"].iloc[-1])
        last_available = pd.Timestamp(cot["available_from"].iloc[-1])

        prior = aligned.loc[aligned.index < last_report].dropna()
        gap = aligned.loc[(aligned.index >= last_report) & (aligned.index < last_available)].dropna()
        after = aligned.loc[aligned.index >= last_available].dropna()

        assert len(prior) and len(gap) and len(after), "fixture ต้องครอบคลุมทั้ง 3 ช่วง"
        # ระหว่างวัดผลกับวันประกาศ: ค่าต้องนิ่งอยู่ที่ค่าเดิม
        assert gap.eq(prior.iloc[-1]).all(), "ค่า COT ใหม่รั่วออกมาก่อนวันประกาศ"
        # พอถึงวันประกาศจริงค่าถึงเปลี่ยน
        assert after.iloc[0] != prior.iloc[-1]

    def test_uses_available_from_not_report_date(self, dates, cfg) -> None:
        """เลื่อนวันประกาศออกไป → ค่าต้องเลื่อนตาม (พิสูจน์ว่าไม่ได้อิง report_date)."""
        cot = make_cot_frame(dates)
        delayed = cot.copy()
        delayed["available_from"] = delayed["report_date"] + pd.Timedelta(days=10)

        base = align_cot_daily(cot, dates, cfg)
        late = align_cot_daily(delayed, dates, cfg)

        # หาวันที่ base มีค่าแล้วแต่ late ยังไม่มี/ยังเป็นค่าเก่า
        diff = (base != late) & base.notna()
        assert diff.any(), "การเลื่อนวันประกาศไม่มีผล → แปลว่ากำลังใช้ report_date"

    def test_no_value_before_first_release(self, dates, cfg) -> None:
        cot = make_cot_frame(dates)
        aligned = align_cot_daily(cot, dates, cfg)
        first_available = pd.Timestamp(cot["available_from"].iloc[0])
        assert aligned.loc[aligned.index < first_available].isna().all()

    def test_future_data_does_not_change_past(self, dates, cfg) -> None:
        """ตัดรายงานท้าย ๆ ทิ้ง → ค่าช่วงต้นต้องไม่เปลี่ยน."""
        cot = make_cot_frame(dates)
        full = align_cot_daily(cot, dates, cfg)
        truncated = align_cot_daily(cot.iloc[:-10], dates, cfg)

        cutoff = pd.Timestamp(cot["available_from"].iloc[-11])
        mask = dates < cutoff
        pd.testing.assert_series_equal(full[mask], truncated[mask], check_names=False)

    def test_ffill_is_limited(self, dates, cfg) -> None:
        """ห้าม ffill ค้างยาวเกิน cot_max_ffill_days (ข้อมูลค้าง = ข้อมูลผิด)."""
        cot = make_cot_frame(dates)
        stopped = cot.iloc[:-20]  # หยุดส่งรายงาน 20 สัปดาห์สุดท้าย
        aligned = align_cot_daily(stopped, dates, cfg)

        last_available = pd.Timestamp(stopped["available_from"].iloc[-1])
        limit = int(cfg["cusi"]["cot_max_ffill_days"])

        # ภายใน limit ยังมีค่า, พ้น limit ต้องเป็น NaN
        assert aligned.loc[aligned.index <= last_available].dropna().size > 0
        assert pd.isna(aligned.iloc[-1]), "ffill ค้างยาวเกินกำหนด"
        beyond = aligned.loc[aligned.index > last_available + pd.Timedelta(days=limit)]
        assert beyond.isna().all()

    def test_shuffled_input_gives_same_result(self, dates, cfg) -> None:
        """ลำดับแถวใน input ต้องไม่มีผล (กันบั๊กจากการไม่ sort)."""
        cot = make_cot_frame(dates)
        shuffled = cot.sample(frac=1.0, random_state=3)
        pd.testing.assert_series_equal(
            align_cot_daily(cot, dates, cfg), align_cot_daily(shuffled, dates, cfg)
        )

    def test_empty_cot_returns_all_nan(self, dates, cfg) -> None:
        assert align_cot_daily(pd.DataFrame(), dates, cfg).isna().all()


class TestCusiNoLookahead:
    """CUSI ทั้งชุดต้องไม่มี lookahead — ตัดท้ายทิ้งแล้วค่าช่วงต้นต้องเท่าเดิม."""

    def test_truncating_future_does_not_change_past(self, calm_frame, cot_frame, cfg, weights) -> None:
        cut = 400
        full = compute_cusi(compute_components(calm_frame, cot_frame, cfg), weights, cfg)
        truncated = compute_cusi(
            compute_components(calm_frame.iloc[:cut], cot_frame, cfg), weights, cfg
        )
        pd.testing.assert_series_equal(
            full.iloc[:cut], truncated.iloc[:cut], check_names=False, rtol=1e-12
        )

    def test_future_shock_does_not_change_past(self, calm_frame, cot_frame, cfg, weights) -> None:
        shocked = apply_unwind_shock(calm_frame, n=3)
        base = compute_cusi(compute_components(calm_frame, cot_frame, cfg), weights, cfg)
        after = compute_cusi(compute_components(shocked, cot_frame, cfg), weights, cfg)
        pd.testing.assert_series_equal(
            base.iloc[:-3], after.iloc[:-3], check_names=False, rtol=1e-12
        )


# ================================================================ orchestration


class TestBuildCusiFrame:
    def test_returns_expected_columns(self, calm_frame, cot_frame, cfg) -> None:
        cusi_df, comp = build_cusi_frame(calm_frame, cot_frame, cfg)
        assert list(cusi_df.columns) == ["value", "level", "change_1d"]
        assert list(comp.columns) == COMPONENT_ORDER

    def test_levels_consistent_with_values(self, calm_frame, cot_frame, cfg) -> None:
        cusi_df, _ = build_cusi_frame(calm_frame, cot_frame, cfg)
        valid = cusi_df.dropna(subset=["value"])
        assert all(assign_level(r["value"], cfg) == r["level"] for _, r in valid.iterrows())

    def test_change_1d_is_diff(self, calm_frame, cot_frame, cfg) -> None:
        cusi_df, _ = build_cusi_frame(calm_frame, cot_frame, cfg)
        expected = cusi_df["value"].diff()
        pd.testing.assert_series_equal(cusi_df["change_1d"], expected, check_names=False)


class TestBuildCusiHistory:
    def test_structure(self, calm_frame, cot_frame, cfg) -> None:
        cusi_df, _ = build_cusi_frame(calm_frame, cot_frame, cfg)
        hist = build_cusi_history(cusi_df, cfg)
        assert hist["count"] == len(hist["history"])
        assert len(hist["levels"]) == 4
        first = hist["history"][0]
        assert set(first) == {"date", "value", "level"}

    def test_no_nan_values_in_history(self, calm_frame, cot_frame, cfg) -> None:
        cusi_df, _ = build_cusi_frame(calm_frame, cot_frame, cfg)
        hist = build_cusi_history(cusi_df, cfg)
        assert all(isinstance(h["value"], float) and np.isfinite(h["value"]) for h in hist["history"])

    def test_all_values_in_range(self, calm_frame, cot_frame, cfg) -> None:
        cusi_df, _ = build_cusi_frame(calm_frame, cot_frame, cfg)
        hist = build_cusi_history(cusi_df, cfg)
        assert all(0.0 <= h["value"] <= 100.0 for h in hist["history"])

    def test_empty_frame(self, cfg) -> None:
        assert build_cusi_history(pd.DataFrame(), cfg) == {"count": 0, "history": []}
