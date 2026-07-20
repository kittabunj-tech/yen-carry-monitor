"""CUSI — Carry Unwind Stress Index (spec §3).

รวม 9 components เป็น composite z-score ถ่วงน้ำหนัก → scale 0–100 → 4 ระดับ alert

ทิศทางเครื่องหมาย (§3.2 คอลัมน์ "ยิ่งสูง = ยิ่งเสี่ยง")
--------------------------------------------------------
ทุก component ถูกทำให้ **ค่าบวก = เสี่ยงมากขึ้น** เสมอ ก่อนถ่วงน้ำหนัก
component ที่ตัวเลขดิบ "ยิ่งต่ำ ยิ่งเสี่ยง" (เช่น USDJPY ร่วง, spread แคบลง,
drawdown ลึก, COT short ลึก) จะถูกกลับเครื่องหมายด้วย ``sign = -1``

หมายเหตุทางคณิตศาสตร์: ``z(-x) ≡ -z(x)`` เพราะ mean กลับเครื่องหมายตาม
ส่วน std ไม่เปลี่ยน และ clip ที่ ±3 สมมาตร → จะกลับเครื่องหมายก่อนหรือหลัง
คำนวณ z ก็ได้ผลเท่ากันเป๊ะ (มีเทสต์ยืนยัน)

LOOKAHEAD (spec §6 จุดที่ต้องระวังที่สุด)
------------------------------------------
1. z-score ทุกตัวใช้ :func:`~pipeline.indicators.rolling_z` ซึ่ง window จบที่ t
2. COT เป็น weekly: ข้อมูลวัน**อังคาร** ประกาศบ่ายวัน**ศุกร์** จึง forward-fill
   จาก ``available_from`` (= report_date + 3 วัน) **ไม่ใช่** ``report_date``
   ถ้า ffill จาก report_date จะรู้ข้อมูลล่วงหน้า 3 วัน = lookahead
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from pipeline import indicators as ind
from pipeline.config import get_logger, load_config

log = get_logger(__name__)

#: นิยาม component ทั้ง 9 ตัวตามตาราง §3.2
#: ``(ชื่อ component, คอลัมน์ต้นทางใน df_indicators, sign)``
#: sign = -1 หมายถึง "ค่าดิบยิ่งต่ำ ยิ่งเสี่ยง" จึงต้องกลับเครื่องหมาย
COMPONENT_SPECS: Dict[str, Tuple[str, int]] = {
    "jpy_momentum": ("usdjpy_chg_5d_pct", -1),      # USDJPY ร่วง = JPY แข็ง = เสี่ยง
    "audjpy_drawdown": ("audjpy_dd_pct", -1),        # dd ติดลบลึก = เสี่ยง
    "spread_compression": ("spread_chg_20d_bps", -1),  # Δ spread ติดลบ = แคบลง = เสี่ยง
    "fx_realized_vol": ("fx_realized_vol_10d", +1),  # vol สูง = เสี่ยง
    "move": ("move", +1),                            # bond vol สูง = เสี่ยง
    "nikkei_momentum": ("nikkei_chg_5d_pct", -1),    # Nikkei ร่วง = เสี่ยง
    "mxnjpy_drawdown": ("mxnjpy_dd_pct", -1),        # canary drawdown ลึก = เสี่ยง
}

#: ลำดับคอลัมน์มาตรฐานของ components DataFrame
COMPONENT_ORDER: List[str] = [
    "jpy_momentum",
    "audjpy_drawdown",
    "spread_compression",
    "vix",
    "fx_realized_vol",
    "cot_positioning",
    "move",
    "nikkei_momentum",
    "mxnjpy_drawdown",
]


# ---------------------------------------------------------------- weights


def load_weights(cfg: Optional[Dict[str, Any]] = None, *, tolerance: float = 1e-6) -> Dict[str, float]:
    """โหลดและ validate น้ำหนักจาก ``config.yaml`` (spec §3.2).

    Args:
        cfg: config dict; ``None`` = โหลดเอง
        tolerance: ความคลาดเคลื่อนที่ยอมให้ผลรวมต่างจาก 1.0

    Returns:
        mapping ``component -> weight``

    Raises:
        ValueError: ถ้าน้ำหนักรวมไม่เท่ากับ 1.0, มีค่าติดลบ,
            หรือมีชื่อ component ที่ระบบไม่รู้จัก / ขาดหายไป
    """
    cfg = cfg if cfg is not None else load_config()
    weights = {str(k): float(v) for k, v in cfg["cusi"]["weights"].items()}

    unknown = set(weights) - set(COMPONENT_ORDER)
    if unknown:
        raise ValueError(f"unknown CUSI component(s) in config: {sorted(unknown)}")

    missing = set(COMPONENT_ORDER) - set(weights)
    if missing:
        raise ValueError(f"missing CUSI weight(s) in config: {sorted(missing)}")

    negative = {k: v for k, v in weights.items() if v < 0}
    if negative:
        raise ValueError(f"CUSI weights must be non-negative, got: {negative}")

    total = sum(weights.values())
    if abs(total - 1.0) > tolerance:
        raise ValueError(f"CUSI weights must sum to 1.0, got {total:.6f} ({weights})")

    return weights


# ---------------------------------------------------------------- COT


def align_cot_daily(
    df_cot: pd.DataFrame,
    index: pd.DatetimeIndex,
    cfg: Dict[str, Any],
) -> pd.Series:
    """แปลง COT weekly → daily z-score โดยไม่ leak อนาคต (spec §5 Phase 2 ข้อ 3).

    ขั้นตอน:
        1. คำนวณ z-score บนความถี่ **weekly** (ไม่ใช่บน series ที่ ffill แล้ว
           เพราะค่าซ้ำจะทำให้ std แคบผิดปกติ)
        2. วาง z ไว้ที่ ``available_from`` (= report_date + 3 วัน = วันศุกร์)
           ซึ่งเป็นวันแรกที่ตลาดรู้ข้อมูลนี้จริง ๆ
        3. forward-fill เป็น daily โดยจำกัดจำนวนวันตาม ``cot_max_ffill_days``

    ผลลัพธ์: ค่าที่วัน t มาจากรายงานที่ประกาศแล้ว ณ วัน t เท่านั้น

    Args:
        df_cot: DataFrame จาก :func:`~pipeline.fetch_cot.fetch_cot_jpy`
            ต้องมีคอลัมน์ ``available_from`` และ ``net_speculative``
        index: ปฏิทินรายวันเป้าหมาย
        cfg: config dict

    Returns:
        Series ของ z-score COT positioning (บวก = short หนาแน่น = เสี่ยง)
        index ตรงกับ ``index``; NaN ในวันที่ยังไม่มีรายงาน
    """
    empty = pd.Series(np.nan, index=index, name="cot_positioning")
    if df_cot is None or df_cot.empty:
        log.warning("no COT data — component 'cot_positioning' will be NaN")
        return empty

    required = {"available_from", "net_speculative"}
    if not required.issubset(df_cot.columns):
        log.error("COT frame missing columns %s", sorted(required - set(df_cot.columns)))
        return empty

    cusi_cfg = cfg["cusi"]
    weekly = (
        df_cot.dropna(subset=["available_from", "net_speculative"])
        .sort_values("available_from")
        .drop_duplicates(subset="available_from", keep="last")
    )
    if weekly.empty:
        return empty

    net = pd.Series(
        weekly["net_speculative"].to_numpy(dtype="float64"),
        index=pd.DatetimeIndex(weekly["available_from"]).normalize(),
    )

    # net ติดลบลึก = short หนาแน่น = เสี่ยง → กลับเครื่องหมายให้ "บวก = เสี่ยง"
    z_weekly = -ind.rolling_z(
        net,
        int(cusi_cfg["cot_z_window_weeks"]),
        float(cfg["parameters"]["z_clip"]),
    )

    # reindex เข้าปฏิทินร่วม แล้ว ffill
    union = z_weekly.index.union(index)
    on_union = z_weekly.reindex(union)
    filled = on_union.ffill()

    # จำกัดอายุการ ffill เป็น "วันปฏิทิน" จริง ๆ
    # (ใช้ ffill(limit=n) ตรง ๆ ไม่ได้ เพราะ limit นับเป็น *จำนวนแถว* ของ index
    #  ซึ่งบน union ของ weekly+daily จะกลายเป็นช่วงเวลาที่ยาวกว่าที่ตั้งใจ)
    stamps = pd.Series(union.where(on_union.notna()), index=union).ffill()
    age_days = (union.to_series().reset_index(drop=True) - stamps.reset_index(drop=True)).dt.days
    age_days.index = union

    out = filled.where(age_days <= int(cusi_cfg["cot_max_ffill_days"])).reindex(index)
    out.name = "cot_positioning"
    return out


# ---------------------------------------------------------------- components


def _z_on_trading_days(
    series: pd.Series,
    index: pd.DatetimeIndex,
    z_window: int,
    z_clip: float,
) -> pd.Series:
    """คำนวณ rolling z บน "วันที่มีข้อมูลจริง" แล้วค่อย reindex กลับ.

    สำคัญ: indicator frame เป็น union ของทุก ticker ซึ่งรวมวันเสาร์–อาทิตย์
    เข้ามาด้วย (BTC เทรด 24/7) ถ้าคำนวณ z บน index นั้นตรง ๆ หน้าต่าง 252 *แถว*
    จะมีข้อมูลจริงแค่ ~180 จุด → ไม่ถึง ``min_periods`` → ได้ NaN ทั้งเส้น

    spec §3.2 ระบุว่า z window = 252 **วันทำการ** จึงต้อง dropna ก่อนคำนวณ
    เพื่อให้ 252 หมายถึงวันที่ตลาดเปิดจริง 252 วัน

    Args:
        series: series ต้นทาง (อาจมี NaN ปนจากการ align)
        index: index เป้าหมายที่จะ reindex กลับไป
        z_window: ขนาดหน้าต่าง (วันทำการ)
        z_clip: clip ที่ ±ค่านี้

    Returns:
        Series ของ z-score บน ``index``
    """
    valid = series.dropna()
    if valid.empty:
        return pd.Series(np.nan, index=index)
    return ind.rolling_z(valid, z_window, z_clip).reindex(index)


def compute_components(
    df_indicators: pd.DataFrame,
    df_cot: Optional[pd.DataFrame] = None,
    config: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """คำนวณ z-score ของทั้ง 9 components ตามตาราง §3.2.

    ทุกคอลัมน์ผลลัพธ์ถูกปรับทิศทางแล้วให้ **บวก = เสี่ยงมากขึ้น**

    Args:
        df_indicators: DataFrame จาก :func:`~pipeline.build_json.build_indicator_frame`
            ต้องมีคอลัมน์ต้นทางตาม :data:`COMPONENT_SPECS` (ตัวที่ขาดจะเป็น NaN)
        df_cot: COT DataFrame; ``None`` = component นี้เป็น NaN
        config: config dict; ``None`` = โหลดเอง

    Returns:
        DataFrame index เดียวกับ ``df_indicators``
        คอลัมน์ตาม :data:`COMPONENT_ORDER` ทุกค่าเป็น z-score ที่ clip แล้ว
    """
    cfg = config if config is not None else load_config()
    p = cfg["parameters"]
    z_win, z_clip = int(p["z_window"]), float(p["z_clip"])

    if df_indicators is None or df_indicators.empty:
        log.error("empty indicator frame — cannot compute components")
        return pd.DataFrame(columns=COMPONENT_ORDER)

    index = df_indicators.index
    out: Dict[str, pd.Series] = {}

    # ---- Phase 6B: เลือกแหล่งของ component fx_realized_vol
    # implied (FXY 1M ATM IV) ใช้ได้ต่อเมื่อออม history พอสำหรับ z window แล้ว
    # ไม่งั้น fallback เป็น realized เงียบ ๆ ไม่ได้ — ต้อง log ให้รู้
    specs = dict(COMPONENT_SPECS)
    if cfg["cusi"].get("fx_vol_source", "realized") == "implied":
        col = "fx_implied_vol_1m"
        need = int(cfg["cusi"].get("implied_min_history", 252))
        have = int(df_indicators[col].notna().sum()) if col in df_indicators.columns else 0
        if have >= need:
            specs["fx_realized_vol"] = (col, +1)
            log.info("fx vol source = implied (history %d วัน ≥ %d)", have, need)
        else:
            log.warning("fx_vol_source=implied แต่ history มีแค่ %d/%d วัน — ใช้ realized ต่อ",
                        have, need)

    # ---- components ที่มาจากคอลัมน์เดียวตรง ๆ
    for name, (column, sign) in specs.items():
        if column not in df_indicators.columns:
            log.warning("indicator column %r missing — component %r = NaN", column, name)
            out[name] = pd.Series(np.nan, index=index)
            continue
        z = _z_on_trading_days(df_indicators[column], index, z_win, z_clip)
        out[name] = z * sign

    # ---- VIX = 0.5×z(level) + 0.5×z(5d change) (§3.2)
    if "vix" in df_indicators.columns:
        z_level = _z_on_trading_days(df_indicators["vix"], index, z_win, z_clip)
        chg = (
            df_indicators["vix_chg_5d"]
            if "vix_chg_5d" in df_indicators.columns
            else df_indicators["vix"].dropna().diff(int(p["momentum_window"]))
        )
        z_chg = _z_on_trading_days(chg, index, z_win, z_clip)
        # ถ้าฝั่งใดฝั่งหนึ่งเป็น NaN ให้ใช้อีกฝั่งเต็ม ๆ แทนที่จะทิ้งทั้ง component
        out["vix"] = pd.concat([z_level, z_chg], axis=1).mean(axis=1, skipna=True)
    else:
        log.warning("indicator column 'vix' missing — component 'vix' = NaN")
        out["vix"] = pd.Series(np.nan, index=index)

    # ---- COT (weekly → daily, กัน lookahead)
    out["cot_positioning"] = align_cot_daily(df_cot, index, cfg)

    components = pd.DataFrame(out)[COMPONENT_ORDER]
    components.index.name = df_indicators.index.name

    coverage = components.notna().sum()
    log.info(
        "components computed: %d rows | non-null per component: %s",
        len(components),
        ", ".join(f"{k}={int(v)}" for k, v in coverage.items()),
    )
    return components


# ---------------------------------------------------------------- index


def compute_cusi(
    components: pd.DataFrame,
    weights: Dict[str, float],
    config: Optional[Dict[str, Any]] = None,
) -> pd.Series:
    """รวม components เป็น CUSI 0–100 ตามสูตร §3.2.

    ``CUSI = 50 + (weighted_z / 3) × 50`` แล้ว clip [0, 100]

    ถ้าวันไหน component บางตัวเป็น NaN จะ **re-normalize** น้ำหนักที่เหลือ
    (ไม่ถือว่า z = 0 เพราะนั่นเท่ากับเดาว่า "ปกติ" ซึ่งบิดผลลง)
    แต่ถ้าน้ำหนักที่ใช้ได้รวมกันน้อยกว่า ``min_weight_coverage`` → วันนั้นเป็น NaN

    Args:
        components: DataFrame จาก :func:`compute_components`
        weights: mapping จาก :func:`load_weights`
        config: config dict; ``None`` = โหลดเอง

    Returns:
        Series ของ CUSI 0–100 (NaN ในวันที่ข้อมูลไม่พอ)
    """
    cfg = config if config is not None else load_config()
    scale = cfg["cusi"]["scale"]
    min_cov = float(cfg["cusi"]["min_weight_coverage"])

    if components is None or components.empty:
        return pd.Series(dtype="float64", name="cusi")

    cols = [c for c in COMPONENT_ORDER if c in components.columns]
    comp = components[cols].astype("float64")
    w = pd.Series({c: float(weights[c]) for c in cols})

    available = comp.notna()
    # น้ำหนักรวมของ component ที่มีค่าจริงในวันนั้น
    weight_sum = available.mul(w, axis=1).sum(axis=1)
    weighted = comp.mul(w, axis=1).sum(axis=1, skipna=True)

    with np.errstate(invalid="ignore", divide="ignore"):
        weighted_z = weighted / weight_sum.where(weight_sum > 0)

    cusi = float(scale["center"]) + (weighted_z / float(scale["divisor"])) * float(scale["span"])
    cusi = cusi.clip(lower=float(scale["floor"]), upper=float(scale["ceiling"]))
    cusi = cusi.where(weight_sum >= min_cov)  # ข้อมูลน้อยเกินไป → ไม่รายงานตัวเลข

    cusi.name = "cusi"
    return cusi


def assign_level(cusi_value: float, config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """แปลงค่า CUSI เป็นระดับ alert ตาม §3.3.

    ขอบเขตเป็นแบบ "ไม่รวมขอบบน": 0–40 CALM, 40–60 WATCH, 60–80 STRESS, 80–100 UNWIND
    (ค่า 40.0 พอดี → WATCH)

    Args:
        cusi_value: ค่า CUSI 0–100
        config: config dict; ``None`` = โหลดเอง

    Returns:
        ``"CALM"`` / ``"WATCH"`` / ``"STRESS"`` / ``"UNWIND"``
        หรือ ``None`` ถ้า input เป็น NaN/None
    """
    cfg = config if config is not None else load_config()
    if cusi_value is None:
        return None
    try:
        value = float(cusi_value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value):
        return None

    for level in cfg["cusi"]["levels"]:
        if value < float(level["max"]):
            return str(level["name"])
    return str(cfg["cusi"]["levels"][-1]["name"])


def assign_levels(cusi: pd.Series, config: Optional[Dict[str, Any]] = None) -> pd.Series:
    """เวอร์ชัน vectorised ของ :func:`assign_level` สำหรับทั้ง series.

    Args:
        cusi: Series ของค่า CUSI
        config: config dict; ``None`` = โหลดเอง

    Returns:
        Series ของชื่อระดับ (object dtype, ``None`` ที่ NaN)
    """
    cfg = config if config is not None else load_config()
    return cusi.map(lambda v: assign_level(v, cfg))


def level_meta(level_name: Optional[str], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """คืน metadata (สี, emoji) ของระดับ — ให้ frontend Phase 3 ใช้.

    Args:
        level_name: ชื่อระดับ
        config: config dict; ``None`` = โหลดเอง

    Returns:
        dict ``{"name", "color", "emoji"}``; ค่าว่างถ้าไม่พบ
    """
    cfg = config if config is not None else load_config()
    for level in cfg["cusi"]["levels"]:
        if str(level["name"]) == level_name:
            return {"name": level["name"], "color": level.get("color"), "emoji": level.get("emoji")}
    return {"name": level_name, "color": None, "emoji": None}


# ---------------------------------------------------------------- breakdown


def contribution_breakdown(
    components: pd.DataFrame,
    weights: Dict[str, float],
    when: Optional[pd.Timestamp] = None,
) -> Dict[str, Dict[str, Any]]:
    """แยกส่วนร่วมของแต่ละ component ณ วันหนึ่ง (schema §4.4).

    * ``contribution`` = ``weight × z`` (ตรงตามตัวอย่างใน §4.4: 0.8 × 0.20 = 0.16)
    * ``share_pct``    = สัดส่วนของ |contribution| เทียบผลรวม |contribution| ทั้งหมด
      ใช้เรียงลำดับ bar chart และหา top-3 ที่ผลักดัน CUSI (§4.6)

    Args:
        components: DataFrame จาก :func:`compute_components`
        weights: mapping จาก :func:`load_weights`
        when: วันที่ต้องการ; ``None`` = แถวสุดท้ายที่มีข้อมูล

    Returns:
        mapping ``component -> {"z", "weight", "contribution", "share_pct"}``
        เรียงจาก contribution มากไปน้อย
    """
    if components is None or components.empty:
        return {}

    row = components.loc[when] if when is not None else components.iloc[-1]

    contributions = {
        name: (float(row[name]) * float(weights[name]))
        for name in COMPONENT_ORDER
        if name in components.columns and pd.notna(row.get(name))
    }
    total_abs = sum(abs(v) for v in contributions.values())

    out: Dict[str, Dict[str, Any]] = {}
    for name, contrib in sorted(contributions.items(), key=lambda kv: kv[1], reverse=True):
        out[name] = {
            "z": round(float(row[name]), 3),
            "weight": round(float(weights[name]), 4),
            "contribution": round(contrib, 4),
            "share_pct": round(abs(contrib) / total_abs * 100.0, 1) if total_abs > 0 else 0.0,
        }
    return out


# ---------------------------------------------------------------- orchestration


def build_cusi_frame(
    df_indicators: pd.DataFrame,
    df_cot: Optional[pd.DataFrame] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """คำนวณ CUSI ย้อนหลังทั้งชุดในครั้งเดียว.

    Args:
        df_indicators: indicator frame
        df_cot: COT DataFrame
        config: config dict; ``None`` = โหลดเอง

    Returns:
        tuple ``(cusi_df, components)`` โดย ``cusi_df`` มีคอลัมน์
        ``value``, ``level``, ``change_1d``
    """
    cfg = config if config is not None else load_config()
    weights = load_weights(cfg)

    components = compute_components(df_indicators, df_cot, cfg)
    value = compute_cusi(components, weights, cfg)

    cusi_df = pd.DataFrame(
        {
            "value": value,
            "level": assign_levels(value, cfg),
            "change_1d": value.diff(),
        }
    )

    valid = cusi_df["value"].dropna()
    if len(valid):
        log.info(
            "CUSI: %d valid days (%s → %s) | last=%.1f (%s) | range %.1f–%.1f | mean %.1f",
            len(valid),
            valid.index[0].date(),
            valid.index[-1].date(),
            valid.iloc[-1],
            assign_level(valid.iloc[-1], cfg),
            valid.min(),
            valid.max(),
            valid.mean(),
        )
    else:
        log.error("CUSI produced no valid values — check component coverage")

    return cusi_df, components


def build_cusi_block(
    cusi_df: pd.DataFrame,
    components: pd.DataFrame,
    weights: Dict[str, float],
    config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """สร้าง block ``cusi`` ของ latest.json ตาม schema §4.4.

    Args:
        cusi_df: จาก :func:`build_cusi_frame`
        components: จาก :func:`compute_components`
        weights: mapping น้ำหนัก
        config: config dict; ``None`` = โหลดเอง

    Returns:
        dict ตาม schema §4.4 หรือ ``None`` ถ้าไม่มีค่า CUSI ที่ใช้ได้
    """
    cfg = config if config is not None else load_config()
    if cusi_df is None or cusi_df.empty:
        return None

    valid = cusi_df["value"].dropna()
    if valid.empty:
        return None

    when = valid.index[-1]
    value = float(valid.iloc[-1])
    change = cusi_df.loc[when, "change_1d"]
    level = assign_level(value, cfg)

    return {
        "value": round(value, 1),
        "level": level,
        "level_color": level_meta(level, cfg)["color"],
        "level_emoji": level_meta(level, cfg)["emoji"],
        "change_1d": round(float(change), 1) if pd.notna(change) else None,
        "as_of": str(pd.Timestamp(when).date()),
        # น้ำหนักทั้ง 9 ตัวตามโมเดล (คงที่ ไม่ขึ้นกับข้อมูลวันนั้น) —
        # frontend ใช้แสดงตาราง methodology ให้ครบ แม้บางวัน component จะขาด
        "weights": {name: round(float(weights[name]), 4) for name in COMPONENT_ORDER},
        "components": contribution_breakdown(components, weights, when),
    }


def build_cusi_history(cusi_df: pd.DataFrame, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """สร้าง payload ของ ``data/cusi_history.json`` (date, value, level ต่อวัน).

    Args:
        cusi_df: จาก :func:`build_cusi_frame`
        config: config dict; ``None`` = โหลดเอง

    Returns:
        dict ``{"generated_from", "count", "levels", "history": [...]}``
    """
    cfg = config if config is not None else load_config()
    if cusi_df is None or cusi_df.empty:
        return {"count": 0, "history": []}

    valid = cusi_df.dropna(subset=["value"])
    history = [
        {
            "date": str(pd.Timestamp(idx).date()),
            "value": round(float(row["value"]), 1),
            "level": row["level"],
        }
        for idx, row in valid.iterrows()
    ]

    return {
        "count": len(history),
        "levels": [
            {"name": lv["name"], "max": lv["max"], "color": lv.get("color")}
            for lv in cfg["cusi"]["levels"]
        ],
        "history": history,
    }
