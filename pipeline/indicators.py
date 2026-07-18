"""Pure indicator functions — ไม่มี I/O, ไม่มี state, ไม่มี lookahead.

LOOKAHEAD CONTRACT (สำคัญที่สุดของโปรเจกต์ — spec §4.7)
--------------------------------------------------------
ทุกฟังก์ชันในโมดูลนี้รับประกันว่า **ค่าที่ index t คำนวณจากข้อมูลถึง index t
เท่านั้น** ไม่มีการใช้ข้อมูลของ t+1 เป็นต้นไป

pandas ``.rolling(w)`` ให้ window ที่ *จบ* ที่ t (inclusive) อยู่แล้ว —
ค่าที่ t จึงใช้ข้อมูล [t-w+1, t] ซึ่งถูกต้องตาม contract และ **ไม่ต้อง shift**
(การ ``.shift(-k)`` คือสิ่งที่ทำให้เกิด lookahead — ห้ามใช้เด็ดขาด
 ส่วน ``.shift(+k)`` ปลอดภัยเพราะดึงอดีตมาเทียบ)

ทุกฟังก์ชันคืน Series ที่มี index เดียวกับ input และเป็น NaN ในช่วง warm-up
ที่ข้อมูลยังไม่ครบ window (``min_periods=window``) — ไม่ backfill
เพราะ backfill = lookahead
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

__all__ = [
    "log_returns",
    "realized_vol",
    "vol_of_vol",
    "true_range",
    "atr_pct",
    "rolling_z",
    "drawdown_from_high",
    "spread",
    "delta_bps",
    "pct_change_n",
    "rolling_percentile",
]

# ---------------------------------------------------------------- helpers


def _as_series(data: pd.Series, name: str = "series") -> pd.Series:
    """Validate + coerce input เป็น float Series (ไม่แก้ของเดิม).

    Args:
        data: input series
        name: ชื่อสำหรับข้อความ error

    Returns:
        copy ของ series ในรูป float64

    Raises:
        TypeError: ถ้า input ไม่ใช่ :class:`pandas.Series`
    """
    if not isinstance(data, pd.Series):
        raise TypeError(f"{name} must be a pandas Series, got {type(data).__name__}")
    return data.astype("float64")


def _validate_window(window: int, name: str = "window") -> int:
    """ตรวจว่า window เป็นจำนวนเต็มบวก.

    Args:
        window: ขนาด window
        name: ชื่อสำหรับข้อความ error

    Returns:
        window ที่ผ่านการตรวจแล้ว

    Raises:
        ValueError: ถ้า window < 1
    """
    window = int(window)
    if window < 1:
        raise ValueError(f"{name} must be >= 1, got {window}")
    return window


# ---------------------------------------------------------------- returns / vol


def log_returns(prices: pd.Series) -> pd.Series:
    """Log return ต่อคาบ: ``ln(P_t / P_{t-1})``.

    ค่าที่ t ใช้เฉพาะ P_t และ P_{t-1} → ปลอดภัยจาก lookahead
    ราคาที่ <= 0 ถือเป็นข้อมูลเสียและถูกแปลงเป็น NaN

    Args:
        prices: series ของราคา (close)

    Returns:
        Series ของ log returns; ตัวแรกเป็น NaN เสมอ
    """
    px = _as_series(prices, "prices")
    px = px.where(px > 0)  # กัน log(0) / log(ติดลบ) → NaN
    return np.log(px / px.shift(1))


def realized_vol(
    prices: pd.Series,
    window: int,
    *,
    trading_days: int = 252,
    as_pct: bool = True,
) -> pd.Series:
    """Annualised realized volatility (spec §2.4).

    ``std(log returns, window) × sqrt(trading_days) × 100``

    Args:
        prices: series ของราคา close
        window: จำนวนคาบสำหรับ rolling std (เช่น 10)
        trading_days: จำนวนวันทำการต่อปีสำหรับ annualise (default 252)
        as_pct: True = คืนเป็นเปอร์เซ็นต์ (×100)

    Returns:
        Series ของ realized vol; NaN ในช่วง warm-up

    Raises:
        ValueError: ถ้า window < 1
    """
    window = _validate_window(window)
    rets = log_returns(prices)
    # ddof=1 (sample std) ตามธรรมเนียมของ realized vol
    vol = rets.rolling(window=window, min_periods=window).std(ddof=1)
    vol = vol * np.sqrt(float(trading_days))
    return vol * 100.0 if as_pct else vol


def vol_of_vol(realized: pd.Series, window: int) -> pd.Series:
    """Std ของ realized vol — จับ regime change (spec §2.4).

    Args:
        realized: series ผลลัพธ์จาก :func:`realized_vol`
        window: rolling window (เช่น 20)

    Returns:
        Series ของ vol-of-vol; NaN ในช่วง warm-up
    """
    window = _validate_window(window)
    rv = _as_series(realized, "realized")
    return rv.rolling(window=window, min_periods=window).std(ddof=1)


# ---------------------------------------------------------------- ATR


def true_range(ohlc: pd.DataFrame) -> pd.Series:
    """True Range ของ Wilder: ``max(H-L, |H-C_prev|, |L-C_prev|)``.

    แถวแรกใช้ ``H-L`` เพราะยังไม่มี close ก่อนหน้า

    Args:
        ohlc: DataFrame ที่มีคอลัมน์ High, Low, Close (case-insensitive)

    Returns:
        Series ของ true range

    Raises:
        TypeError: ถ้า input ไม่ใช่ DataFrame
        KeyError: ถ้าไม่มีคอลัมน์ High/Low/Close
    """
    if not isinstance(ohlc, pd.DataFrame):
        raise TypeError(f"ohlc must be a DataFrame, got {type(ohlc).__name__}")

    cols = {str(c).lower(): c for c in ohlc.columns}
    missing = [c for c in ("high", "low", "close") if c not in cols]
    if missing:
        raise KeyError(f"ohlc missing column(s): {missing}; got {list(ohlc.columns)}")

    high = ohlc[cols["high"]].astype("float64")
    low = ohlc[cols["low"]].astype("float64")
    prev_close = ohlc[cols["close"]].astype("float64").shift(1)  # shift(+1) = อดีต, ปลอดภัย

    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1, skipna=True)
    return tr


def atr_pct(
    ohlc: pd.DataFrame,
    window: int,
    *,
    method: str = "wilder",
) -> pd.Series:
    """ATR เป็น % ของราคาปิด (spec §2.4): ``ATR(window) / close × 100``.

    Args:
        ohlc: DataFrame ที่มี High, Low, Close
        window: ATR period (เช่น 14)
        method: ``"wilder"`` (RMA, ค่ามาตรฐาน) หรือ ``"sma"`` (rolling mean)

    Returns:
        Series ของ ATR%; NaN ในช่วง warm-up (แถวแรก ``window`` แถว)

    Raises:
        ValueError: ถ้า window < 1 หรือ method ไม่รู้จัก
    """
    window = _validate_window(window)
    if method not in ("wilder", "sma"):
        raise ValueError(f"method must be 'wilder' or 'sma', got {method!r}")

    tr = true_range(ohlc)

    if method == "sma":
        atr = tr.rolling(window=window, min_periods=window).mean()
    else:
        # Wilder's RMA = EWM ที่ alpha = 1/window; ewm ใช้เฉพาะข้อมูลถึง t → ไม่ lookahead
        atr = tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()

    # TR แถวแรกไม่มี prev_close จริง → กันไม่ให้ค่า seed ที่ไม่สมบูรณ์รั่วออกไป
    if len(atr) > 0:
        atr.iloc[:window] = np.nan

    cols = {str(c).lower(): c for c in ohlc.columns}
    close = ohlc[cols["close"]].astype("float64")
    close = close.where(close != 0)  # กันหารศูนย์ → NaN
    return atr / close * 100.0


# ---------------------------------------------------------------- z-score


def rolling_z(
    series: pd.Series,
    window: int,
    clip: Optional[float] = 3.0,
    *,
    min_periods: Optional[int] = None,
) -> pd.Series:
    """Time-series rolling z-score (spec §3.2): ``(x_t - mean_w) / std_w``.

    Window จบที่ t และ **รวม** x_t เอง (ตามนิยาม z-score ของ spec) →
    ไม่มีข้อมูลอนาคตเข้ามาเกี่ยวข้อง

    กรณี std = 0 (ค่าคงที่) → คืน 0.0 แทน ±inf เพราะ "ไม่เบี่ยงเบนจากค่าเฉลี่ย"
    คือคำตอบที่ถูกต้องเชิงเศรษฐศาสตร์ และกัน inf รั่วเข้า CUSI

    Args:
        series: input series
        window: rolling window (เช่น 252)
        clip: clip ค่าที่ ±clip; ``None`` = ไม่ clip
        min_periods: จำนวนจุดขั้นต่ำ; ``None`` = ``window`` (เข้มที่สุด)

    Returns:
        Series ของ z-score; NaN ในช่วง warm-up

    Raises:
        ValueError: ถ้า window < 1 หรือ clip <= 0
    """
    window = _validate_window(window)
    if clip is not None and clip <= 0:
        raise ValueError(f"clip must be > 0 or None, got {clip}")

    x = _as_series(series, "series")
    mp = window if min_periods is None else max(1, int(min_periods))

    roll = x.rolling(window=window, min_periods=mp)
    mean = roll.mean()
    std = roll.std(ddof=1)

    # std == 0 หรือ NaN → z = 0 (แต่เฉพาะจุดที่ mean คำนวณได้ ไม่งั้นต้องคง NaN ไว้)
    z = (x - mean) / std.where(std > 0)
    z = z.where(~((std.notna()) & (std <= 0)), 0.0)
    z = z.where(mean.notna())  # warm-up ยังคงเป็น NaN

    if clip is not None:
        z = z.clip(lower=-float(clip), upper=float(clip))
    return z


def rolling_percentile(series: pd.Series, window: int, *, min_periods: Optional[int] = None) -> pd.Series:
    """Percentile rank (0–100) ของค่าปัจจุบันเทียบ window ย้อนหลัง.

    ใช้กับ ``percentile_3y`` ใน schema §4.4

    Args:
        series: input series
        window: lookback window (เช่น 756 = 3 ปี)
        min_periods: จำนวนจุดขั้นต่ำ; ``None`` = ``min(window, 20)``

    Returns:
        Series ของ percentile 0–100; NaN ในช่วง warm-up
    """
    window = _validate_window(window)
    x = _as_series(series, "series")
    mp = min(window, 20) if min_periods is None else max(2, int(min_periods))

    def _rank(arr: np.ndarray) -> float:
        valid = arr[~np.isnan(arr)]
        if valid.size < 2:
            return np.nan
        # เทียบค่าล่าสุดกับทั้ง window (รวมตัวเอง) → ไม่มีอนาคต
        return float((valid <= valid[-1]).sum()) / float(valid.size) * 100.0

    return x.rolling(window=window, min_periods=mp).apply(_rank, raw=True)


# ---------------------------------------------------------------- drawdown / change


def drawdown_from_high(prices: pd.Series, window: int) -> pd.Series:
    """% ห่างจาก rolling high (spec §3.2 — AUDJPY / MXNJPY drawdown).

    ``(P_t / max(P, window) - 1) × 100`` → ค่าเป็นลบหรือศูนย์เสมอ
    rolling max จบที่ t และรวม P_t เอง จึงไม่มี lookahead
    (ถ้าใช้ max ของทั้ง series = lookahead แบบคลาสสิก — ห้าม)

    Args:
        prices: series ของราคา
        window: lookback (เช่น 20)

    Returns:
        Series ของ drawdown % (<= 0); NaN ในช่วง warm-up
    """
    window = _validate_window(window)
    px = _as_series(prices, "prices")
    roll_max = px.rolling(window=window, min_periods=window).max()
    roll_max = roll_max.where(roll_max != 0)  # กันหารศูนย์
    return (px / roll_max - 1.0) * 100.0


def pct_change_n(prices: pd.Series, periods: int) -> pd.Series:
    """% เปลี่ยนแปลงเทียบ ``periods`` คาบก่อนหน้า (เช่น 1d, 5d return).

    Args:
        prices: series ของราคา
        periods: จำนวนคาบย้อนหลัง

    Returns:
        Series ของ % change

    Raises:
        ValueError: ถ้า periods < 1
    """
    periods = _validate_window(periods, "periods")
    px = _as_series(prices, "prices")
    prev = px.shift(periods)  # อดีตเท่านั้น
    prev = prev.where(prev != 0)
    return (px / prev - 1.0) * 100.0


# ---------------------------------------------------------------- yields


def spread(a: pd.Series, b: pd.Series) -> pd.Series:
    """ส่วนต่างของ yield สองเส้น (spec §2.2 #9): ``a - b``.

    Align ตาม index แบบ outer แล้วปล่อยเป็น NaN ถ้าฝั่งใดฝั่งหนึ่งขาด —
    **ไม่ forward-fill** เพราะจะกลายเป็นการเดาค่าที่ยังไม่ประกาศ

    Args:
        a: yield ขาลบตัวตั้ง (เช่น US10Y) เป็น % (4.21 = 4.21%)
        b: yield ตัวลบ (เช่น JGB10Y) เป็น %

    Returns:
        Series ของ spread เป็น % point
    """
    sa = _as_series(a, "a")
    sb = _as_series(b, "b")
    idx = sa.index.union(sb.index)
    return sa.reindex(idx) - sb.reindex(idx)


def delta_bps(series: pd.Series, periods: int) -> pd.Series:
    """การเปลี่ยนแปลงของ yield/spread เป็น basis points เทียบ ``periods`` ก่อนหน้า.

    Input เป็น % point (4.21 = 4.21%) → ผลคูณ 100 เพื่อให้เป็น bps

    Args:
        series: series ของ yield หรือ spread เป็น %
        periods: จำนวนคาบย้อนหลัง (เช่น 5 หรือ 20)

    Returns:
        Series ของ Δ เป็น bps

    Raises:
        ValueError: ถ้า periods < 1
    """
    periods = _validate_window(periods, "periods")
    x = _as_series(series, "series")
    return (x - x.shift(periods)) * 100.0
