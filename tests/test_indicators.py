"""Unit tests สำหรับ pipeline/indicators.py.

ครอบทุกฟังก์ชัน + edge cases ที่ระบุใน spec:
  * NaN ปนกลาง series
  * series สั้นกว่า window
  * ค่าคงที่ (std = 0)
  * และที่สำคัญที่สุด: **lookahead** — ค่าที่ t ต้องไม่เปลี่ยนเมื่อข้อมูลอนาคตเปลี่ยน
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pipeline.indicators import (
    atr_pct,
    delta_bps,
    drawdown_from_high,
    log_returns,
    pct_change_n,
    realized_vol,
    rolling_percentile,
    rolling_z,
    spread,
    true_range,
    vol_of_vol,
)

# ---------------------------------------------------------------- fixtures


@pytest.fixture
def dates() -> pd.DatetimeIndex:
    """ปฏิทินวันทำการ 300 วัน."""
    return pd.bdate_range("2023-01-02", periods=300)


@pytest.fixture
def prices(dates: pd.DatetimeIndex) -> pd.Series:
    """ราคาสังเคราะห์แบบ random walk (seed คงที่ → ทดสอบซ้ำได้)."""
    rng = np.random.default_rng(42)
    steps = rng.normal(0.0, 0.005, len(dates))
    return pd.Series(150.0 * np.exp(np.cumsum(steps)), index=dates, name="usdjpy")


@pytest.fixture
def ohlc(prices: pd.Series) -> pd.DataFrame:
    """OHLC สังเคราะห์ที่สอดคล้องกัน (High >= Close >= Low)."""
    rng = np.random.default_rng(7)
    close = prices
    spread_ = np.abs(rng.normal(0.4, 0.15, len(close)))
    return pd.DataFrame(
        {
            "Open": close.shift(1).fillna(close.iloc[0]),
            "High": close + spread_,
            "Low": close - spread_,
            "Close": close,
            "Volume": rng.integers(1000, 5000, len(close)),
        },
        index=close.index,
    )


@pytest.fixture
def constant(dates: pd.DatetimeIndex) -> pd.Series:
    """Series ค่าคงที่ — ทำให้ std = 0."""
    return pd.Series(100.0, index=dates, name="flat")


# ================================================================ log_returns


class TestLogReturns:
    def test_first_value_is_nan(self, prices: pd.Series) -> None:
        assert np.isnan(log_returns(prices).iloc[0])

    def test_matches_manual_calculation(self, prices: pd.Series) -> None:
        got = log_returns(prices)
        expected = np.log(prices.iloc[5] / prices.iloc[4])
        assert got.iloc[5] == pytest.approx(expected)

    def test_constant_series_gives_zero(self, constant: pd.Series) -> None:
        assert log_returns(constant).iloc[1:].abs().max() == pytest.approx(0.0)

    def test_non_positive_prices_become_nan(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series([100.0, 0.0, -5.0, 110.0], index=dates[:4])
        out = log_returns(px)
        assert out.iloc[1:3].isna().all()

    def test_rejects_non_series(self) -> None:
        with pytest.raises(TypeError):
            log_returns([1, 2, 3])  # type: ignore[arg-type]


# ================================================================ realized_vol


class TestRealizedVol:
    def test_warmup_is_nan_then_values(self, prices: pd.Series) -> None:
        out = realized_vol(prices, 10)
        # ต้องมี return 10 ตัว → ต้องมีราคา 11 แถว → index 0..9 เป็น NaN
        assert out.iloc[:10].isna().all()
        assert out.iloc[10:].notna().all()

    def test_annualisation_scales_correctly(self, prices: pd.Series) -> None:
        raw = realized_vol(prices, 10, as_pct=False)
        pct = realized_vol(prices, 10, as_pct=True)
        assert (pct.dropna() / raw.dropna()).round(6).eq(100.0).all()

    def test_known_value(self, dates: pd.DatetimeIndex) -> None:
        # ราคาสลับขึ้นลงคงที่ → std ของ log return คำนวณมือได้
        px = pd.Series([100.0, 101.0, 100.0, 101.0, 100.0, 101.0], index=dates[:6])
        out = realized_vol(px, 5, trading_days=252)
        rets = np.log(px / px.shift(1)).dropna()
        expected = rets.std(ddof=1) * np.sqrt(252) * 100
        assert out.iloc[-1] == pytest.approx(expected)

    def test_constant_series_gives_zero_vol(self, constant: pd.Series) -> None:
        out = realized_vol(constant, 10)
        assert out.dropna().abs().max() == pytest.approx(0.0)

    def test_series_shorter_than_window_is_all_nan(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series([100.0, 101.0, 102.0], index=dates[:3])
        assert realized_vol(px, 10).isna().all()

    def test_empty_series(self) -> None:
        assert len(realized_vol(pd.Series(dtype="float64"), 10)) == 0

    def test_nan_in_middle_propagates_locally_only(self, prices: pd.Series) -> None:
        dirty = prices.copy()
        dirty.iloc[50] = np.nan
        out = realized_vol(dirty, 10)
        # NaN ทำให้ window ที่คร่อมมันเสีย แต่ต้องฟื้นหลังผ่านไป
        assert out.iloc[70:].notna().all()

    @pytest.mark.parametrize("bad", [0, -1])
    def test_invalid_window_raises(self, prices: pd.Series, bad: int) -> None:
        with pytest.raises(ValueError):
            realized_vol(prices, bad)


class TestVolOfVol:
    def test_shape_and_warmup(self, prices: pd.Series) -> None:
        rv = realized_vol(prices, 10)
        vov = vol_of_vol(rv, 20)
        assert len(vov) == len(prices)
        assert vov.notna().sum() > 0

    def test_constant_vol_gives_zero(self, dates: pd.DatetimeIndex) -> None:
        rv = pd.Series(8.0, index=dates)
        assert vol_of_vol(rv, 20).dropna().abs().max() == pytest.approx(0.0)


# ================================================================ ATR


class TestTrueRange:
    def test_first_row_uses_high_minus_low(self, ohlc: pd.DataFrame) -> None:
        tr = true_range(ohlc)
        expected = ohlc["High"].iloc[0] - ohlc["Low"].iloc[0]
        assert tr.iloc[0] == pytest.approx(expected)

    def test_takes_max_of_three_ranges(self) -> None:
        df = pd.DataFrame(
            {"High": [10.0, 12.0], "Low": [9.0, 11.5], "Close": [9.5, 12.0]},
            index=pd.bdate_range("2024-01-01", periods=2),
        )
        # แถว 2: H-L=0.5, |H-Cprev|=2.5, |L-Cprev|=2.0 → 2.5
        assert true_range(df).iloc[1] == pytest.approx(2.5)

    def test_case_insensitive_columns(self, ohlc: pd.DataFrame) -> None:
        lower = ohlc.rename(columns=str.lower)
        assert true_range(lower).equals(true_range(ohlc))

    def test_missing_column_raises(self, ohlc: pd.DataFrame) -> None:
        with pytest.raises(KeyError):
            true_range(ohlc.drop(columns="Low"))

    def test_rejects_series(self, prices: pd.Series) -> None:
        with pytest.raises(TypeError):
            true_range(prices)  # type: ignore[arg-type]


class TestAtrPct:
    def test_warmup_masked(self, ohlc: pd.DataFrame) -> None:
        out = atr_pct(ohlc, 14)
        assert out.iloc[:14].isna().all()
        assert out.iloc[14:].notna().all()

    def test_is_positive_percentage(self, ohlc: pd.DataFrame) -> None:
        out = atr_pct(ohlc, 14).dropna()
        assert (out > 0).all()
        assert (out < 100).all()

    @pytest.mark.parametrize("method", ["wilder", "sma"])
    def test_both_methods_are_close(self, ohlc: pd.DataFrame, method: str) -> None:
        out = atr_pct(ohlc, 14, method=method).dropna()
        assert len(out) > 0
        assert out.mean() == pytest.approx(atr_pct(ohlc, 14).dropna().mean(), rel=0.5)

    def test_sma_method_matches_manual(self, ohlc: pd.DataFrame) -> None:
        out = atr_pct(ohlc, 14, method="sma")
        tr = true_range(ohlc)
        expected = tr.rolling(14).mean().iloc[20] / ohlc["Close"].iloc[20] * 100
        assert out.iloc[20] == pytest.approx(expected)

    def test_short_series_all_nan(self, ohlc: pd.DataFrame) -> None:
        assert atr_pct(ohlc.head(5), 14).isna().all()

    def test_zero_close_gives_nan_not_inf(self, ohlc: pd.DataFrame) -> None:
        df = ohlc.copy()
        df.iloc[20, df.columns.get_loc("Close")] = 0.0
        out = atr_pct(df, 14)
        assert np.isnan(out.iloc[20])
        assert not np.isinf(out.dropna()).any()

    def test_invalid_method_raises(self, ohlc: pd.DataFrame) -> None:
        with pytest.raises(ValueError):
            atr_pct(ohlc, 14, method="ema")


# ================================================================ rolling_z


class TestRollingZ:
    def test_warmup_is_nan(self, prices: pd.Series) -> None:
        out = rolling_z(prices, 252)
        assert out.iloc[:251].isna().all()
        assert out.iloc[251:].notna().all()

    def test_matches_manual_calculation(self, prices: pd.Series) -> None:
        out = rolling_z(prices, 20, clip=None)
        window = prices.iloc[11:31]
        expected = (prices.iloc[30] - window.mean()) / window.std(ddof=1)
        assert out.iloc[30] == pytest.approx(expected)

    def test_clip_bounds_respected(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series(np.r_[np.zeros(30), 1000.0], index=dates[:31])
        out = rolling_z(px, 20, clip=3.0)
        assert out.max() == pytest.approx(3.0)

    def test_clip_none_allows_large_values(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series(np.r_[np.random.default_rng(1).normal(0, 1, 30), 1000.0], index=dates[:31])
        assert rolling_z(px, 20, clip=None).iloc[-1] > 3.0

    def test_constant_series_gives_zero_not_inf(self, constant: pd.Series) -> None:
        """std = 0 → z ต้องเป็น 0 ไม่ใช่ inf/NaN (สำคัญมาก — กัน inf รั่วเข้า CUSI)."""
        out = rolling_z(constant, 20)
        valid = out.iloc[19:]
        assert valid.notna().all()
        assert valid.abs().max() == pytest.approx(0.0)
        assert not np.isinf(out.dropna()).any()

    def test_series_shorter_than_window_all_nan(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series([1.0, 2.0, 3.0], index=dates[:3])
        assert rolling_z(px, 252).isna().all()

    def test_min_periods_override(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series(np.arange(10, dtype=float), index=dates[:10])
        assert rolling_z(px, 252, min_periods=5).iloc[4:].notna().all()

    def test_nan_input_stays_nan(self, prices: pd.Series) -> None:
        dirty = prices.copy()
        dirty.iloc[100] = np.nan
        assert np.isnan(rolling_z(dirty, 20).iloc[100])

    def test_empty_series(self) -> None:
        assert len(rolling_z(pd.Series(dtype="float64"), 20)) == 0

    def test_invalid_clip_raises(self, prices: pd.Series) -> None:
        with pytest.raises(ValueError):
            rolling_z(prices, 20, clip=0)

    def test_invalid_window_raises(self, prices: pd.Series) -> None:
        with pytest.raises(ValueError):
            rolling_z(prices, 0)


class TestRollingPercentile:
    def test_range_is_0_to_100(self, prices: pd.Series) -> None:
        out = rolling_percentile(prices, 100).dropna()
        assert out.min() >= 0.0
        assert out.max() <= 100.0

    def test_rising_series_is_top_percentile(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series(np.arange(100, dtype=float), index=dates[:100])
        assert rolling_percentile(px, 50).iloc[-1] == pytest.approx(100.0)

    def test_constant_series(self, constant: pd.Series) -> None:
        # ทุกค่าเท่ากัน → ค่าปัจจุบัน >= ทุกค่า → 100
        assert rolling_percentile(constant, 50).dropna().eq(100.0).all()

    def test_short_series_all_nan(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series([1.0], index=dates[:1])
        assert rolling_percentile(px, 50).isna().all()


# ================================================================ drawdown


class TestDrawdownFromHigh:
    def test_always_non_positive(self, prices: pd.Series) -> None:
        assert (drawdown_from_high(prices, 20).dropna() <= 1e-9).all()

    def test_at_new_high_is_zero(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series(np.arange(1, 51, dtype=float), index=dates[:50])
        assert drawdown_from_high(px, 20).iloc[-1] == pytest.approx(0.0)

    def test_known_drawdown(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series([100.0] * 19 + [90.0], index=dates[:20])
        assert drawdown_from_high(px, 20).iloc[-1] == pytest.approx(-10.0)

    def test_uses_rolling_not_global_high(self, dates: pd.DatetimeIndex) -> None:
        """high เก่าที่หลุด window แล้วต้องไม่ถูกนับ."""
        px = pd.Series([200.0] + [100.0] * 29, index=dates[:30])
        # ที่ index 25 ค่า 200 หลุด window 20 ไปแล้ว → dd = 0
        assert drawdown_from_high(px, 20).iloc[25] == pytest.approx(0.0)

    def test_warmup_is_nan(self, prices: pd.Series) -> None:
        assert drawdown_from_high(prices, 20).iloc[:19].isna().all()

    def test_short_series_all_nan(self, dates: pd.DatetimeIndex) -> None:
        assert drawdown_from_high(pd.Series([1.0, 2.0], index=dates[:2]), 20).isna().all()

    def test_constant_series_is_zero(self, constant: pd.Series) -> None:
        assert drawdown_from_high(constant, 20).dropna().abs().max() == pytest.approx(0.0)


# ================================================================ changes


class TestPctChangeN:
    def test_known_value(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series([100.0, 105.0], index=dates[:2])
        assert pct_change_n(px, 1).iloc[1] == pytest.approx(5.0)

    def test_5d_return(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0, 95.0], index=dates[:6])
        assert pct_change_n(px, 5).iloc[5] == pytest.approx(-5.0)

    def test_warmup_is_nan(self, prices: pd.Series) -> None:
        assert pct_change_n(prices, 5).iloc[:5].isna().all()

    def test_constant_series_is_zero(self, constant: pd.Series) -> None:
        assert pct_change_n(constant, 5).dropna().abs().max() == pytest.approx(0.0)

    def test_zero_denominator_gives_nan(self, dates: pd.DatetimeIndex) -> None:
        px = pd.Series([0.0, 100.0], index=dates[:2])
        assert np.isnan(pct_change_n(px, 1).iloc[1])

    def test_invalid_periods_raises(self, prices: pd.Series) -> None:
        with pytest.raises(ValueError):
            pct_change_n(prices, 0)


class TestSpread:
    def test_simple_difference(self, dates: pd.DatetimeIndex) -> None:
        a = pd.Series([4.2, 4.3], index=dates[:2])
        b = pd.Series([1.1, 1.2], index=dates[:2])
        assert spread(a, b).tolist() == pytest.approx([3.1, 3.1])

    def test_misaligned_index_gives_nan_not_fill(self, dates: pd.DatetimeIndex) -> None:
        """ห้าม forward-fill ใน spread — วันที่ขาดต้องเป็น NaN."""
        a = pd.Series([4.2, 4.3, 4.4], index=dates[:3])
        b = pd.Series([1.1, 1.2], index=dates[[0, 2]])
        out = spread(a, b)
        assert len(out) == 3
        assert np.isnan(out.iloc[1])

    def test_union_of_indexes(self, dates: pd.DatetimeIndex) -> None:
        a = pd.Series([4.2], index=dates[:1])
        b = pd.Series([1.1], index=dates[1:2])
        assert len(spread(a, b)) == 2

    def test_empty_inputs(self) -> None:
        assert len(spread(pd.Series(dtype="float64"), pd.Series(dtype="float64"))) == 0


class TestDeltaBps:
    def test_converts_percent_to_bps(self, dates: pd.DatetimeIndex) -> None:
        # 4.21% → 4.15% = -6 bps
        y = pd.Series([4.21, 4.15], index=dates[:2])
        assert delta_bps(y, 1).iloc[1] == pytest.approx(-6.0)

    def test_20d_change(self, dates: pd.DatetimeIndex) -> None:
        y = pd.Series(np.linspace(3.0, 3.20, 21), index=dates[:21])
        assert delta_bps(y, 20).iloc[-1] == pytest.approx(20.0)

    def test_warmup_is_nan(self, dates: pd.DatetimeIndex) -> None:
        y = pd.Series(np.linspace(3.0, 3.2, 25), index=dates[:25])
        assert delta_bps(y, 20).iloc[:20].isna().all()

    def test_constant_series_is_zero(self, constant: pd.Series) -> None:
        assert delta_bps(constant, 5).dropna().abs().max() == pytest.approx(0.0)

    def test_nan_propagates(self, dates: pd.DatetimeIndex) -> None:
        y = pd.Series([3.0, np.nan, 3.2], index=dates[:3])
        assert np.isnan(delta_bps(y, 1).iloc[1])


# ================================================================ LOOKAHEAD


class TestNoLookahead:
    """บทเรียนจาก Engine v2 (spec §4.7): ค่าที่ t ต้องไม่ขึ้นกับข้อมูลหลัง t.

    วิธีทดสอบ: คำนวณบน series เต็ม แล้วคำนวณซ้ำบน series ที่ตัดท้ายทิ้ง
    ค่าที่ index เดียวกันต้องเท่ากันเป๊ะ
    """

    @staticmethod
    def _assert_prefix_stable(full: pd.Series, truncated: pd.Series, cut: int) -> None:
        """เทียบค่าช่วงต้นของสองผลลัพธ์ว่าตรงกันทุกจุด."""
        a = full.iloc[:cut]
        b = truncated.iloc[:cut]
        pd.testing.assert_series_equal(a, b, check_names=False, rtol=1e-12, atol=1e-12)

    def test_realized_vol(self, prices: pd.Series) -> None:
        cut = 200
        self._assert_prefix_stable(realized_vol(prices, 10), realized_vol(prices.iloc[:cut], 10), cut)

    def test_rolling_z(self, prices: pd.Series) -> None:
        cut = 200
        self._assert_prefix_stable(rolling_z(prices, 60), rolling_z(prices.iloc[:cut], 60), cut)

    def test_drawdown(self, prices: pd.Series) -> None:
        cut = 200
        self._assert_prefix_stable(drawdown_from_high(prices, 20), drawdown_from_high(prices.iloc[:cut], 20), cut)

    def test_atr_pct(self, ohlc: pd.DataFrame) -> None:
        cut = 200
        self._assert_prefix_stable(atr_pct(ohlc, 14), atr_pct(ohlc.iloc[:cut], 14), cut)

    def test_percentile(self, prices: pd.Series) -> None:
        cut = 200
        self._assert_prefix_stable(rolling_percentile(prices, 100), rolling_percentile(prices.iloc[:cut], 100), cut)

    def test_delta_bps(self, prices: pd.Series) -> None:
        cut = 200
        self._assert_prefix_stable(delta_bps(prices, 20), delta_bps(prices.iloc[:cut], 20), cut)

    def test_pct_change(self, prices: pd.Series) -> None:
        cut = 200
        self._assert_prefix_stable(pct_change_n(prices, 5), pct_change_n(prices.iloc[:cut], 5), cut)

    def test_future_spike_does_not_change_past(self, prices: pd.Series) -> None:
        """ยัด spike มหาศาลที่วันสุดท้าย — ค่าก่อนหน้าต้องไม่ขยับแม้แต่นิดเดียว."""
        spiked = prices.copy()
        spiked.iloc[-1] = 10_000.0

        for fn in (
            lambda s: realized_vol(s, 10),
            lambda s: rolling_z(s, 60),
            lambda s: drawdown_from_high(s, 20),
            lambda s: rolling_percentile(s, 100),
        ):
            base, mod = fn(prices), fn(spiked)
            pd.testing.assert_series_equal(base.iloc[:-1], mod.iloc[:-1], check_names=False)


# ================================================================ integration


class TestOutputContracts:
    """สัญญาที่ทุกฟังก์ชันต้องรักษา — index เดิม, ไม่แก้ input, ไม่มี inf."""

    @pytest.fixture
    def all_outputs(self, prices: pd.Series, ohlc: pd.DataFrame) -> dict:
        rv = realized_vol(prices, 10)
        return {
            "log_returns": log_returns(prices),
            "realized_vol": rv,
            "vol_of_vol": vol_of_vol(rv, 20),
            "atr_pct": atr_pct(ohlc, 14),
            "rolling_z": rolling_z(prices, 60),
            "rolling_percentile": rolling_percentile(prices, 100),
            "drawdown": drawdown_from_high(prices, 20),
            "pct_change": pct_change_n(prices, 5),
            "delta_bps": delta_bps(prices, 20),
        }

    def test_index_preserved(self, all_outputs: dict, prices: pd.Series) -> None:
        for name, out in all_outputs.items():
            assert out.index.equals(prices.index), f"{name} changed the index"

    def test_no_infinities(self, all_outputs: dict) -> None:
        for name, out in all_outputs.items():
            assert not np.isinf(out.dropna()).any(), f"{name} produced inf"

    def test_dtype_is_float(self, all_outputs: dict) -> None:
        for name, out in all_outputs.items():
            assert out.dtype == np.float64, f"{name} is not float64"

    def test_input_not_mutated(self, prices: pd.Series) -> None:
        before = prices.copy()
        realized_vol(prices, 10)
        rolling_z(prices, 60)
        drawdown_from_high(prices, 20)
        pd.testing.assert_series_equal(prices, before)
