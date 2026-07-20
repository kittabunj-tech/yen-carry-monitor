"""CUSI-Lite (Phase 6D) — mini-CUSI จากแท่ง 1h อัพเดททุกชั่วโมงคู่ daily CUSI.

ใช้เฉพาะ 3 components ที่มีข้อมูล intraday จริงตาม spec Phase 6D:

  * ``jpy_momentum_1h`` — z ของ return N แท่งของ USDJPY (กลับเครื่องหมาย:
    USDJPY ร่วง = เยนแข็ง = เสี่ยง)
  * ``vix_1h``          — 0.5×z(ระดับ VIX) + 0.5×z(การเปลี่ยน N แท่ง)
    (สูตรเดียวกับ component VIX ใน CUSI เต็ม เพื่อความสอดคล้อง)
  * ``fx_vol_1h``       — z ของ realized vol บนแท่ง 1h ของ USDJPY

หลักออกแบบที่ต่างจาก CUSI เต็ม:
  * **ไม่ align ข้ามซีรีส์** — VIX มี ~6.5 แท่ง/วัน (RTH) ส่วน FX มี ~23 แท่ง/วัน
    การ join รายชั่วโมงสร้างรูมากกว่าข้อมูล แต่ละ component จึงคำนวณ z บนแท่ง
    ของตัวเองแล้วรวมจาก **ค่า z ล่าสุด** ของแต่ละตัว
  * **ตลาดเปิดไม่พร้อมกัน** — สุดสัปดาห์/นอกเวลา US แท่ง VIX จะค้าง:
    component ที่แท่งล่าสุดเก่ากว่า ``max_bar_age_hours`` ถูกตัดออกแล้ว
    re-normalize น้ำหนักที่เหลือ (ปรัชญาเดียวกับ CUSI เต็ม) ถ้าเหลือน้ำหนัก
    ไม่ถึง ``min_weight_coverage`` → ไม่รายงานตัวเลข (คืน available=false)

Lookahead: ทุก z ใช้ :func:`~pipeline.indicators.rolling_z` (window จบที่แท่ง
ปัจจุบัน) — มีเทสต์ truncation invariance เช่นเดียวกับ CUSI เต็ม

Backward compatible: เพิ่ม field ใหม่ ``cusi_lite`` ใน latest.json เท่านั้น
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from pipeline import indicators as ind
from pipeline.config import get_logger, load_config, resolve_path
from pipeline.cusi import assign_level, level_meta

log = get_logger(__name__)

#: ticker key ที่แต่ละ component ต้องใช้ (ทั้งหมดตั้ง intraday: true ใน config อยู่แล้ว)
REQUIRED_KEYS = ("usdjpy", "vix")


# ---------------------------------------------------------------- helpers


def _close_1h(df: Optional[pd.DataFrame]) -> pd.Series:
    """ดึง Close ที่สะอาดจาก DataFrame แท่ง 1h.

    Args:
        df: OHLCV 1h (อาจ ``None``/ว่าง)

    Returns:
        Series ของ Close (tz-aware ตามต้นทาง); ว่างถ้าไม่มีข้อมูล
    """
    if df is None or df.empty or "Close" not in df.columns:
        return pd.Series(dtype="float64")
    s = df["Close"].astype("float64").dropna()
    return s[~s.index.duplicated(keep="last")].sort_index()


def _bar_age_hours(series: pd.Series, now: datetime) -> Optional[float]:
    """อายุของแท่งล่าสุดเป็นชั่วโมง.

    Args:
        series: series ที่ index เป็นเวลาแท่ง
        now: เวลาปัจจุบัน (tz-aware)

    Returns:
        ชั่วโมง หรือ ``None`` ถ้า series ว่าง
    """
    if series.empty:
        return None
    last = series.index[-1]
    last_utc = last.tz_convert(timezone.utc) if last.tzinfo else last.tz_localize(timezone.utc)
    return (now - last_utc).total_seconds() / 3600.0


def _last_z(series: pd.Series, window: int, clip: float, min_periods: int) -> Optional[float]:
    """ค่า z ล่าสุดของ series (คืน ``None`` ถ้า warm-up ยังไม่พอ).

    Args:
        series: input
        window: rolling window (แท่ง)
        clip: clip ±
        min_periods: จุดขั้นต่ำ

    Returns:
        z ล่าสุด หรือ ``None``
    """
    if series.dropna().empty:
        return None
    z = ind.rolling_z(series.dropna(), window, clip, min_periods=min_periods)
    val = z.dropna()
    return float(val.iloc[-1]) if len(val) else None


# ---------------------------------------------------------------- components


def compute_lite_components(
    intraday: Dict[str, pd.DataFrame],
    cfg: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """คำนวณ z ล่าสุดของทั้ง 3 components จากแท่ง 1h.

    ทุกตัวปรับทิศทางแล้วให้ **บวก = เสี่ยงมากขึ้น** (ธรรมเนียมเดียวกับ CUSI เต็ม)

    Args:
        intraday: mapping ``key -> OHLCV 1h`` (จาก FetchResult.intraday หรือ cache)
        cfg: config dict
        now: เวลาปัจจุบัน (ฉีดได้ในเทสต์)

    Returns:
        mapping ``component -> {"z", "bar_time", "age_hours", "bars"}``
        (component ที่คำนวณไม่ได้จะไม่อยู่ใน dict)
    """
    lc = cfg["cusi_lite"]
    now = now or datetime.now(timezone.utc)
    win, mp, clip = int(lc["z_window_bars"]), int(lc["z_min_periods"]), float(lc["clip"])
    mom_bars, vol_bars = int(lc["momentum_bars"]), int(lc["vol_bars"])

    usdjpy = _close_1h(intraday.get("usdjpy"))
    vix = _close_1h(intraday.get("vix"))

    out: Dict[str, Dict[str, Any]] = {}

    def add(name: str, z: Optional[float], src: pd.Series) -> None:
        """บันทึก component ถ้าคำนวณได้."""
        if z is None or not np.isfinite(z):
            return
        out[name] = {
            "z": round(z, 3),
            "bar_time": src.index[-1].isoformat(),
            "age_hours": round(_bar_age_hours(src, now) or 0.0, 1),
            "bars": int(len(src)),
        }

    # ---- JPY momentum: USDJPY ร่วง = เสี่ยง → กลับเครื่องหมาย
    if len(usdjpy) > mom_bars:
        ret = ind.pct_change_n(usdjpy, mom_bars)
        z = _last_z(ret, win, clip, mp)
        add("jpy_momentum_1h", -z if z is not None else None, usdjpy)

    # ---- VIX: 0.5×z(level) + 0.5×z(change) — สูตรเดียวกับ CUSI เต็ม
    if len(vix) > mom_bars:
        z_level = _last_z(vix, win, clip, mp)
        z_chg = _last_z(vix.diff(mom_bars), win, clip, mp)
        parts = [v for v in (z_level, z_chg) if v is not None]
        if parts:
            add("vix_1h", sum(parts) / len(parts), vix)

    # ---- FX realized vol บนแท่ง 1h
    if len(usdjpy) > vol_bars + 1:
        rv = ind.realized_vol(usdjpy, vol_bars, trading_days=int(lc["bars_per_year_fx"]))
        z = _last_z(rv, win, clip, mp)
        add("fx_vol_1h", z, usdjpy)

    return out


# ---------------------------------------------------------------- composite


def compute_cusi_lite(
    intraday: Dict[str, pd.DataFrame],
    cfg: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """รวม components เป็น CUSI-Lite 0–100 (สเกลและระดับเดียวกับ CUSI เต็ม).

    Args:
        intraday: mapping ``key -> OHLCV 1h``
        cfg: config dict; ``None`` = โหลดเอง
        now: เวลาปัจจุบัน (ฉีดได้ในเทสต์)

    Returns:
        dict block สำหรับ latest.json — ``available: false`` พร้อมเหตุผล
        เมื่อข้อมูลไม่พอ (ไม่คืน ``None`` เพื่อให้ frontend แสดงสาเหตุได้)
    """
    cfg = cfg if cfg is not None else load_config()
    lc = cfg["cusi_lite"]
    now = now or datetime.now(timezone.utc)

    weights = {str(k): float(v) for k, v in lc["weights"].items()}
    total_w = sum(weights.values())
    if abs(total_w - 1.0) > 1e-6:
        raise ValueError(f"cusi_lite.weights ต้องรวมกัน = 1.0 (ได้ {total_w:.4f})")

    comps = compute_lite_components(intraday, cfg, now=now)

    # ---- ตัด component ที่แท่งค้างเกินเกณฑ์ แล้ว re-normalize
    max_age = float(lc["max_bar_age_hours"])
    stale = [k for k, v in comps.items() if v["age_hours"] > max_age]
    active = {k: v for k, v in comps.items() if k not in stale}

    used_weight = sum(weights.get(k, 0.0) for k in active)
    base = {
        "components": comps,
        "stale": stale,
        "weights": weights,
        "as_of": max((v["bar_time"] for v in active.values()), default=None),
        "computed_at": now.isoformat(timespec="seconds"),
        "note": "1h bars · 3 components (jpy momentum / vix / fx vol)",
    }

    if used_weight < float(lc["min_weight_coverage"]):
        log.warning("CUSI-Lite: น้ำหนักใช้ได้ %.0f%% < เกณฑ์ — ไม่รายงานตัวเลข", used_weight * 100)
        return {**base, "available": False,
                "reason": f"ข้อมูลสดไม่พอ (น้ำหนักใช้ได้ {used_weight*100:.0f}%)"}

    weighted_z = sum(active[k]["z"] * weights[k] for k in active) / used_weight

    scale = cfg["cusi"]["scale"]  # ใช้สเกลเดียวกับ CUSI เต็ม — เทียบกันตรง ๆ ได้
    value = float(scale["center"]) + (weighted_z / float(scale["divisor"])) * float(scale["span"])
    value = max(float(scale["floor"]), min(float(scale["ceiling"]), value))
    level = assign_level(value, cfg)

    log.info("CUSI-Lite: %.1f (%s) | z=%.2f | active=%s stale=%s",
             value, level, weighted_z, sorted(active), stale or "-")
    return {
        **base, "available": True,
        "value": round(value, 1),
        "level": level,
        "level_color": level_meta(level, cfg)["color"],
        "weighted_z": round(weighted_z, 3),
        "weight_coverage": round(used_weight, 2),
    }


# ---------------------------------------------------------------- cache loader


def load_cached_intraday(cfg: Dict[str, Any]) -> Dict[str, pd.DataFrame]:
    """อ่านแท่ง 1h จาก parquet cache (โหมด daily/weekly ที่ไม่ได้ดึง intraday).

    Args:
        cfg: config dict

    Returns:
        mapping เฉพาะ key ที่ CUSI-Lite ใช้ (ว่างได้ถ้าไม่มี cache)
    """
    from pipeline.fetch_market import read_cache

    raw_dir = resolve_path(cfg, "raw_dir")
    out: Dict[str, pd.DataFrame] = {}
    for key in REQUIRED_KEYS:
        df = read_cache(raw_dir / f"{key}_intraday.parquet")
        if df is not None and not df.empty:
            out[key] = df
    return out


if __name__ == "__main__":  # pragma: no cover
    from pipeline.config import setup_logging
    _cfg = load_config()
    setup_logging(_cfg)
    _block = compute_cusi_lite(load_cached_intraday(_cfg), _cfg)
    import json
    log.info("\n%s", json.dumps(_block, ensure_ascii=False, indent=1))
