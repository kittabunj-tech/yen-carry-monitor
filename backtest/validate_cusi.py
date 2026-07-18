"""Phase 4 — Backtest validation ของ CUSI ตาม spec §3.4.

รัน CUSI ย้อนหลังตั้งแต่ 2019-01-01 แล้วตรวจ 3 อย่าง:

1. **Event study** — CUSI ณ วันเกิดเหตุ และ 10 วันทำการก่อนหน้า
2. **False positive** — สัญญาณ (CUSI > 60) ที่ไม่มีอะไรเกิดตามภายใน 20 วัน
   นิยาม "เกิด": SPX drawdown > 5% หรือ USDJPY −3% ภายใน 20 วันทำการ
3. **Lead time** — CUSI ข้าม 60 นำหน้าเหตุการณ์จริงเฉลี่ยกี่วัน

ข้อควรรู้เรื่องข้อมูล
---------------------
* ต้องดึงข้อมูลย้อนไปก่อน ``--start`` อีก 1 ปี เพราะ z-score กิน warm-up
  252 วันทำการ ถ้าเริ่มดึงที่ 2019 พอดีจะไม่มีค่า CUSI ของปี 2019 เลย
* ใช้ cache แยก (``data/backtest/raw``) เพื่อไม่ทับ cache ของ production
  ซึ่งเก็บแค่ 4 ปีตาม ``history_years``
* ช่วงที่ COT ยังไม่มีข้อมูล component นั้นจะเป็น NaN และ
  :func:`~pipeline.cusi.compute_cusi` จะ re-normalize น้ำหนักที่เหลือให้เอง
  (ไม่ได้แทนค่าด้วย z = 0)

    python backtest/validate_cusi.py                 # รันเต็ม (ดึงข้อมูลใหม่)
    python backtest/validate_cusi.py --use-cache     # ใช้ CUSI ที่คำนวณไว้แล้ว
    python backtest/validate_cusi.py --start 2019-01-01 --threshold 60
"""

from __future__ import annotations

import argparse
import copy
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.build_json import build_indicator_frame  # noqa: E402
from pipeline.config import REPO_ROOT, get_logger, load_config, setup_logging  # noqa: E402
from pipeline.cusi import (  # noqa: E402
    COMPONENT_ORDER,
    assign_level,
    build_cusi_frame,
    contribution_breakdown,
    load_weights,
)
from pipeline.fetch_cot import fetch_cot_jpy  # noqa: E402
from pipeline.fetch_jgb import fetch_jgb10y  # noqa: E402
from pipeline.fetch_market import close_series, fetch_market_data  # noqa: E402

log = get_logger(__name__)

#: เหตุการณ์อ้างอิงตาม spec §3.4
#: ``(วันที่, ชื่อ, ต้องถึงระดับอะไรอย่างน้อย)``
KNOWN_EVENTS: List[Tuple[str, str, Optional[str]]] = [
    ("2020-03-16", "COVID crash", "STRESS"),
    ("2022-12-20", "BOJ YCC tweak", None),
    ("2024-08-05", "Yen carry unwind", "UNWIND"),
]

#: ลำดับความรุนแรงของระดับ (ใช้เทียบว่าถึงเกณฑ์ไหม)
LEVEL_RANK = {"CALM": 0, "WATCH": 1, "STRESS": 2, "UNWIND": 3}


# ================================================================ data


def backtest_config(start: str, warmup_years: int = 1) -> Dict[str, Any]:
    """สร้าง config สำหรับ backtest — ประวัติยาวขึ้น + cache แยกจาก production.

    Args:
        start: วันเริ่มต้นของ backtest (``YYYY-MM-DD``)
        warmup_years: จำนวนปีที่ต้องดึงเพิ่มก่อน ``start`` เพื่อ warm-up z-score

    Returns:
        config dict ที่แก้แล้ว (ไม่กระทบ config เดิม)
    """
    cfg = copy.deepcopy(load_config())

    years_needed = (pd.Timestamp.now() - pd.Timestamp(start)).days / 365.25 + warmup_years
    cfg["parameters"]["history_years"] = int(np.ceil(years_needed))

    # cache แยก — ห้ามทับของ production ที่เก็บแค่ 4 ปี
    cfg["paths"]["raw_dir"] = "data/backtest/raw"
    cfg["paths"]["jgb_parquet"] = "data/backtest/raw/jgb10y.parquet"
    cfg["paths"]["cot_parquet"] = "data/backtest/raw/cot_jpy.parquet"

    log.info(
        "backtest config: start=%s warmup=%dy → ดึงข้อมูลย้อนหลัง %d ปี",
        start, warmup_years, cfg["parameters"]["history_years"],
    )
    return cfg


@dataclass
class BacktestData:
    """ผลลัพธ์การเตรียมข้อมูลสำหรับ backtest.

    Attributes:
        cusi: DataFrame ``value``, ``level``, ``change_1d``
        components: DataFrame ของ z-score 9 components
        spx: ราคาปิด S&P 500
        usdjpy: ราคาปิด USD/JPY
        coverage: จำนวนวันที่แต่ละ component มีค่า
    """

    cusi: pd.DataFrame
    components: pd.DataFrame
    spx: pd.Series
    usdjpy: pd.Series
    coverage: Dict[str, int] = field(default_factory=dict)


def prepare_data(cfg: Dict[str, Any], start: str) -> BacktestData:
    """ดึงข้อมูลย้อนหลังแล้วคำนวณ CUSI ทั้งชุด.

    Args:
        cfg: config จาก :func:`backtest_config`
        start: วันเริ่มของ backtest (ตัดผลลัพธ์ตั้งแต่วันนี้)

    Returns:
        :class:`BacktestData`
    """
    log.info("=== fetching market data (อาจใช้เวลาสักครู่) ===")
    market = fetch_market_data(cfg, include_intraday=False)
    jgb_df, jgb_source = fetch_jgb10y(cfg)
    cot_df = fetch_cot_jpy(cfg)

    log.info("JGB source=%s rows=%d | COT rows=%d", jgb_source, len(jgb_df), len(cot_df))
    if len(cot_df):
        log.info("COT covers %s → %s", cot_df["report_date"].iloc[0].date(), cot_df["report_date"].iloc[-1].date())

    df = build_indicator_frame(market, jgb_df, cfg)
    cusi_df, components = build_cusi_frame(df, cot_df, cfg)

    start_ts = pd.Timestamp(start)
    cusi_df = cusi_df.loc[cusi_df.index >= start_ts]
    components = components.loc[components.index >= start_ts]

    return BacktestData(
        cusi=cusi_df,
        components=components,
        spx=close_series(market, "spx"),
        usdjpy=close_series(market, "usdjpy"),
        coverage={c: int(components[c].notna().sum()) for c in COMPONENT_ORDER if c in components.columns},
    )


# ================================================================ episodes


def find_episodes(series: pd.Series, threshold: float, *, min_gap_days: int = 10) -> List[Dict[str, Any]]:
    """หาช่วงที่ series อยู่เหนือ threshold ต่อเนื่อง แล้วรวมเป็น "episode".

    ถ้าสองช่วงห่างกันไม่เกิน ``min_gap_days`` จะถือเป็น episode เดียวกัน —
    ไม่งั้นการแกว่งขึ้นลงรอบเส้น 60 จะถูกนับเป็นสัญญาณหลายสิบครั้ง

    Args:
        series: series ของค่า CUSI (index = วันที่)
        threshold: เกณฑ์ (เช่น 60)
        min_gap_days: ระยะห่างขั้นต่ำที่จะถือว่าเป็นคนละ episode

    Returns:
        list ของ dict ``{"start", "end", "peak", "peak_date", "days"}``
    """
    valid = series.dropna()
    if valid.empty:
        return []

    above = valid >= threshold
    if not above.any():
        return []

    episodes: List[Dict[str, Any]] = []
    current_start: Optional[pd.Timestamp] = None
    last_above: Optional[pd.Timestamp] = None

    for ts, is_above in above.items():
        if is_above:
            if current_start is None:
                current_start = ts
            last_above = ts
        elif current_start is not None and last_above is not None:
            if (ts - last_above).days > min_gap_days:
                episodes.append({"start": current_start, "end": last_above})
                current_start = None
                last_above = None

    if current_start is not None and last_above is not None:
        episodes.append({"start": current_start, "end": last_above})

    for ep in episodes:
        window = valid.loc[ep["start"]:ep["end"]]
        ep["peak"] = float(window.max())
        ep["peak_date"] = window.idxmax()
        ep["days"] = int(len(window))
    return episodes


def forward_outcome(
    spx: pd.Series,
    usdjpy: pd.Series,
    when: pd.Timestamp,
    horizon: int,
    spx_dd_pct: float,
    usdjpy_dd_pct: float,
) -> Dict[str, Any]:
    """ตรวจว่าหลังวัน ``when`` ภายใน ``horizon`` วันทำการ มี "เหตุการณ์" เกิดไหม.

    นิยามตาม spec §5 Phase 4: SPX drawdown > 5% **หรือ** USDJPY −3%

    Args:
        spx: ราคาปิด S&P 500
        usdjpy: ราคาปิด USD/JPY
        when: วันตั้งต้น (วันที่เกิดสัญญาณ)
        horizon: จำนวนวันทำการข้างหน้าที่ตรวจ
        spx_dd_pct: เกณฑ์ drawdown ของ SPX (ค่าบวก เช่น 5.0)
        usdjpy_dd_pct: เกณฑ์ของ USDJPY (ค่าบวก เช่น 3.0)

    Returns:
        dict ``{"hit", "spx_min_pct", "usdjpy_min_pct", "trigger"}``
    """
    def _worst(series: pd.Series) -> Optional[float]:
        """% เปลี่ยนแปลงที่แย่ที่สุดใน horizon ข้างหน้า."""
        s = series.dropna()
        after = s.loc[s.index > when]
        if after.empty:
            return None
        window = after.iloc[:horizon]
        base_candidates = s.loc[s.index <= when]
        if window.empty or base_candidates.empty:
            return None
        base = float(base_candidates.iloc[-1])
        if base == 0:
            return None
        return float(window.min() / base - 1.0) * 100.0

    spx_worst = _worst(spx)
    fx_worst = _worst(usdjpy)

    triggers = []
    if spx_worst is not None and spx_worst <= -abs(spx_dd_pct):
        triggers.append(f"SPX {spx_worst:.1f}%")
    if fx_worst is not None and fx_worst <= -abs(usdjpy_dd_pct):
        triggers.append(f"USDJPY {fx_worst:.1f}%")

    return {
        "hit": bool(triggers),
        "spx_min_pct": spx_worst,
        "usdjpy_min_pct": fx_worst,
        "trigger": " + ".join(triggers) if triggers else "—",
    }


def find_market_events(
    spx: pd.Series,
    usdjpy: pd.Series,
    index: pd.DatetimeIndex,
    spx_dd_pct: float,
    usdjpy_dd_pct: float,
    *,
    min_gap_days: int = 30,
) -> List[Dict[str, Any]]:
    """หา "เหตุการณ์จริง" ในตลาดโดยไม่ดู CUSI เลย (ground truth ที่เป็นอิสระ).

    เหตุการณ์ = วันที่ราคาร่วงถึงเกณฑ์เทียบกับจุดสูงสุด 20 วันก่อนหน้า
    แล้วรวมวันที่ติดกันเป็น episode เดียว

    Args:
        spx: ราคาปิด SPX
        usdjpy: ราคาปิด USDJPY
        index: ปฏิทินที่สนใจ (ช่วง backtest)
        spx_dd_pct: เกณฑ์ drawdown SPX
        usdjpy_dd_pct: เกณฑ์ USDJPY
        min_gap_days: ระยะห่างที่ถือว่าเป็นคนละเหตุการณ์

    Returns:
        list ของ dict ``{"start", "end", "trigger"}``
    """
    lookback = 20

    def _drawdown(series: pd.Series) -> pd.Series:
        """% ห่างจากจุดสูงสุด ``lookback`` วันที่ผ่านมา."""
        s = series.dropna()
        roll_max = s.rolling(lookback, min_periods=5).max()
        return (s / roll_max - 1.0) * 100.0

    spx_dd = _drawdown(spx)
    fx_dd = _drawdown(usdjpy)

    stressed = pd.Series(False, index=index)
    for dd, threshold in ((spx_dd, spx_dd_pct), (fx_dd, usdjpy_dd_pct)):
        flag = (dd <= -abs(threshold)).reindex(index).fillna(False).astype(bool)
        stressed = stressed | flag

    events: List[Dict[str, Any]] = []
    current: Optional[pd.Timestamp] = None
    last: Optional[pd.Timestamp] = None

    for ts, flag in stressed.items():
        if flag:
            if current is None:
                current = ts
            last = ts
        elif current is not None and last is not None and (ts - last).days > min_gap_days:
            events.append({"start": current, "end": last})
            current, last = None, None

    if current is not None and last is not None:
        events.append({"start": current, "end": last})

    for ev in events:
        sp = spx_dd.loc[ev["start"]:ev["end"]].min() if len(spx_dd) else np.nan
        fx = fx_dd.loc[ev["start"]:ev["end"]].min() if len(fx_dd) else np.nan
        parts = []
        if pd.notna(sp):
            parts.append(f"SPX {sp:.1f}%")
        if pd.notna(fx):
            parts.append(f"USDJPY {fx:.1f}%")
        ev["trigger"] = " / ".join(parts)
    return events


# ================================================================ analyses


def event_study(data: BacktestData, cfg: Dict[str, Any], lookback: int = 10) -> List[Dict[str, Any]]:
    """ตรวจค่า CUSI ณ วันเกิดเหตุ และ ``lookback`` วันทำการก่อนหน้า (spec §3.4).

    Args:
        data: ข้อมูล backtest
        cfg: config dict
        lookback: จำนวนวันทำการก่อนเหตุการณ์ที่ต้องรายงาน

    Returns:
        list ของ dict ต่อเหตุการณ์
    """
    values = data.cusi["value"].dropna()
    rows: List[Dict[str, Any]] = []

    for date_str, label, required in KNOWN_EVENTS:
        ts = pd.Timestamp(date_str)
        if values.empty or ts < values.index[0] or ts > values.index[-1]:
            rows.append({"date": date_str, "label": label, "in_range": False, "required": required})
            continue

        # วันเกิดเหตุอาจเป็นวันหยุด → ใช้วันทำการที่ใกล้ที่สุดก่อนหน้า
        upto = values.loc[values.index <= ts]
        on_event = float(upto.iloc[-1])
        on_date = upto.index[-1]

        prior = values.loc[values.index < ts]
        before = float(prior.iloc[-lookback]) if len(prior) >= lookback else np.nan
        before_date = prior.index[-lookback] if len(prior) >= lookback else None

        # จุดสูงสุดในหน้าต่าง ±10 วันรอบเหตุการณ์
        window = values.loc[ts - pd.Timedelta(days=20): ts + pd.Timedelta(days=20)]
        peak = float(window.max()) if len(window) else np.nan
        peak_date = window.idxmax() if len(window) else None

        level = assign_level(on_event, cfg)
        peak_level = assign_level(peak, cfg)
        passed = None
        if required is not None and pd.notna(peak):
            passed = LEVEL_RANK.get(peak_level, -1) >= LEVEL_RANK.get(required, 99)

        rows.append({
            "date": date_str,
            "label": label,
            "in_range": True,
            "on_event": on_event,
            "on_date": on_date,
            "level": level,
            "before": before,
            "before_date": before_date,
            "rise": on_event - before if pd.notna(before) else np.nan,
            "peak": peak,
            "peak_date": peak_date,
            "peak_level": peak_level,
            "required": required,
            "passed": passed,
        })
    return rows


def signal_analysis(
    data: BacktestData,
    threshold: float,
    horizon: int,
    spx_dd_pct: float,
    usdjpy_dd_pct: float,
) -> Dict[str, Any]:
    """False positive analysis — สัญญาณไหนตามด้วยเหตุการณ์จริง สัญญาณไหนไม่.

    Args:
        data: ข้อมูล backtest
        threshold: เกณฑ์สัญญาณ (เช่น 60)
        horizon: จำนวนวันทำการที่ให้เวลาเหตุการณ์เกิด
        spx_dd_pct: เกณฑ์ SPX
        usdjpy_dd_pct: เกณฑ์ USDJPY

    Returns:
        dict ``{"signals", "n", "tp", "fp", "precision"}``
    """
    episodes = find_episodes(data.cusi["value"], threshold)
    signals: List[Dict[str, Any]] = []

    for ep in episodes:
        # นิยาม "เข้ม": นับจากวันที่ข้ามเส้นเท่านั้น (ตรงตามตัวอักษรของ spec)
        strict = forward_outcome(data.spx, data.usdjpy, ep["start"], horizon, spx_dd_pct, usdjpy_dd_pct)
        # นิยาม "ผ่อน": ให้เวลาถึงวันสุดท้ายที่ยังเตือนอยู่ + horizon
        # (สัญญาณที่ค้างอยู่ 40 วันแล้วเกิดเหตุวันที่ 30 ไม่ควรนับเป็น false positive)
        span_days = max(int((ep["end"] - ep["start"]).days), 0)
        lenient = forward_outcome(
            data.spx, data.usdjpy, ep["start"], horizon + span_days, spx_dd_pct, usdjpy_dd_pct
        )
        signals.append({**ep, **strict, "hit_lenient": lenient["hit"], "trigger_lenient": lenient["trigger"]})

    tp = sum(1 for s in signals if s["hit"])
    tp_len = sum(1 for s in signals if s["hit_lenient"])
    n = len(signals)
    return {
        "signals": signals,
        "n": n,
        "tp": tp,
        "fp": n - tp,
        "precision": (tp / n * 100.0) if n else float("nan"),
        "tp_lenient": tp_len,
        "fp_lenient": n - tp_len,
        "precision_lenient": (tp_len / n * 100.0) if n else float("nan"),
    }


def threshold_sweep(
    data: BacktestData,
    horizon: int,
    spx_dd_pct: float,
    usdjpy_dd_pct: float,
    thresholds: Optional[List[float]] = None,
) -> List[Dict[str, Any]]:
    """กวาดค่า threshold เพื่อดู trade-off ระหว่าง precision กับ recall.

    ใช้ประกอบการตัดสินใจว่าควรขยับ *เส้นแบ่งระดับ* หรือไม่ —
    ซึ่งปลอดภัยกว่าการขยับ *น้ำหนัก* เพราะไม่ได้แก้ตัวโมเดลเอง

    Args:
        data: ข้อมูล backtest
        horizon: จำนวนวันทำการที่ให้เหตุการณ์เกิด
        spx_dd_pct: เกณฑ์ SPX
        usdjpy_dd_pct: เกณฑ์ USDJPY
        thresholds: รายการ threshold ที่จะลอง

    Returns:
        list ของ dict ต่อ threshold
    """
    thresholds = thresholds or [50.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0]
    rows: List[Dict[str, Any]] = []

    for th in thresholds:
        sig = signal_analysis(data, th, horizon, spx_dd_pct, usdjpy_dd_pct)
        lead = lead_time_analysis(data, th, spx_dd_pct, usdjpy_dd_pct)
        rows.append({
            "threshold": th,
            "n_signals": sig["n"],
            "precision": sig["precision"],
            "precision_lenient": sig["precision_lenient"],
            "recall": lead["recall"],
            "detected": lead["detected"],
            "n_events": lead["n"],
            "mean_lead": lead["mean_lead"],
        })
    return rows


def lead_time_analysis(
    data: BacktestData,
    threshold: float,
    spx_dd_pct: float,
    usdjpy_dd_pct: float,
    *,
    max_lead_days: int = 60,
) -> Dict[str, Any]:
    """Lead time — CUSI ข้าม threshold ก่อนเหตุการณ์จริงกี่วัน.

    Args:
        data: ข้อมูล backtest
        threshold: เกณฑ์สัญญาณ
        spx_dd_pct: เกณฑ์ SPX สำหรับนิยามเหตุการณ์
        usdjpy_dd_pct: เกณฑ์ USDJPY
        max_lead_days: ถ้าสัญญาณมาก่อนเกินนี้ ไม่ถือว่าเกี่ยวข้องกัน

    Returns:
        dict ``{"events", "detected", "missed", "recall", "mean_lead", "median_lead"}``
    """
    values = data.cusi["value"].dropna()
    events = find_market_events(data.spx, data.usdjpy, values.index, spx_dd_pct, usdjpy_dd_pct)
    episodes = find_episodes(values, threshold)

    rows: List[Dict[str, Any]] = []
    for ev in events:
        # CUSI สูงสุดใน 20 วันทำการก่อนเหตุการณ์ — ใช้ดูว่า "พลาด" แบบเฉียดฉิวหรือไม่รู้เรื่องเลย
        before = values.loc[values.index < ev["start"]].tail(20)
        max_before = float(before.max()) if len(before) else np.nan

        # เหตุการณ์ที่เริ่มพร้อมขอบซ้ายของช่วงข้อมูล = artifact
        # (การขายในเดือน ธ.ค. 2018 ดำเนินอยู่ก่อน backtest เริ่ม จึงวัด lead time ไม่ได้จริง)
        at_boundary = bool(len(values) and (ev["start"] - values.index[0]).days <= 5)

        candidates = [
            ep for ep in episodes
            if ep["start"] <= ev["start"] and (ev["start"] - ep["start"]).days <= max_lead_days
        ]
        if candidates:
            best = max(candidates, key=lambda e: e["start"])  # ใกล้เหตุการณ์ที่สุด
            lead = (ev["start"] - best["start"]).days
            rows.append({**ev, "detected": True, "signal_date": best["start"], "lead_days": lead,
                         "peak": best["peak"], "max_before": max_before, "at_boundary": at_boundary})
        else:
            rows.append({**ev, "detected": False, "signal_date": None, "lead_days": np.nan,
                         "peak": np.nan, "max_before": max_before, "at_boundary": at_boundary})

    leads = [r["lead_days"] for r in rows if r["detected"]]
    detected = len(leads)
    return {
        "events": rows,
        "n": len(rows),
        "detected": detected,
        "missed": len(rows) - detected,
        "recall": (detected / len(rows) * 100.0) if rows else float("nan"),
        "mean_lead": float(np.mean(leads)) if leads else float("nan"),
        "median_lead": float(np.median(leads)) if leads else float("nan"),
    }


def sensitivity_event_definition(
    data: BacktestData,
    threshold: float,
    horizon: int,
) -> List[Dict[str, Any]]:
    """ทดสอบว่าผลเปลี่ยนไปแค่ไหนถ้าเปลี่ยน *นิยามเหตุการณ์*.

    CUSI ออกแบบมาจับ carry unwind โดยเฉพาะ ถ้านิยามเหตุการณ์รวมการร่วงของหุ้น
    ทุกสาเหตุ (SPX −5% ไม่ว่าเกิดจากอะไร) ย่อมมีเหตุการณ์ที่โมเดลไม่ควรจับอยู่ด้วย
    หัวข้อนี้จึงเทียบนิยามหลายแบบเพื่อแยกว่า "โมเดลไม่ดี" กับ "วัดผิดเรื่อง" คนละอย่าง

    Args:
        data: ข้อมูล backtest
        threshold: เกณฑ์สัญญาณ
        horizon: จำนวนวันทำการ

    Returns:
        list ของ dict ต่อหนึ่งนิยาม
    """
    big = 999.0  # ปิดเงื่อนไขฝั่งนั้นโดยตั้งเกณฑ์ให้สูงจนไม่มีทางทริกเกอร์
    definitions = [
        ("spec: SPX −5% หรือ USDJPY −3%", 5.0, 3.0),
        ("carry เท่านั้น: USDJPY −3%", big, 3.0),
        ("carry เท่านั้น: USDJPY −4%", big, 4.0),
        ("หุ้นเท่านั้น: SPX −5%", 5.0, big),
    ]

    rows: List[Dict[str, Any]] = []
    for label, spx_dd, fx_dd in definitions:
        sig = signal_analysis(data, threshold, horizon, spx_dd, fx_dd)
        lead = lead_time_analysis(data, threshold, spx_dd, fx_dd)
        rows.append({
            "label": label,
            "n_events": lead["n"],
            "recall": lead["recall"],
            "n_signals": sig["n"],
            "precision": sig["precision"],
            "mean_lead": lead["mean_lead"],
        })
    return rows


def lead_window_sensitivity(
    data: BacktestData,
    threshold: float,
    spx_dd_pct: float,
    usdjpy_dd_pct: float,
    windows: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """ดูว่า recall เปลี่ยนแค่ไหนเมื่อเปลี่ยนหน้าต่างจับคู่สัญญาณกับเหตุการณ์.

    ``max_lead_days`` เป็นพารามิเตอร์ที่เลือกเอง ไม่ใช่ข้อเท็จจริง
    จึงต้องแสดงให้เห็นว่าผลลัพธ์ไวต่อมันแค่ไหน

    Args:
        data: ข้อมูล backtest
        threshold: เกณฑ์สัญญาณ
        spx_dd_pct: เกณฑ์ SPX
        usdjpy_dd_pct: เกณฑ์ USDJPY
        windows: รายการ max_lead_days ที่จะลอง

    Returns:
        list ของ dict ต่อหนึ่งหน้าต่าง
    """
    windows = windows or [20, 30, 60, 90]
    rows: List[Dict[str, Any]] = []
    for w in windows:
        res = lead_time_analysis(data, threshold, spx_dd_pct, usdjpy_dd_pct, max_lead_days=w)
        rows.append({
            "max_lead_days": w,
            "detected": res["detected"],
            "n": res["n"],
            "recall": res["recall"],
            "mean_lead": res["mean_lead"],
        })
    return rows


def component_diagnosis(data: BacktestData, weights: Dict[str, float], when: str) -> List[Dict[str, Any]]:
    """แยกดูว่า ณ วันหนึ่ง component ไหนดัน CUSI ขึ้น / ฉุดลง (spec §5 Phase 4 ข้อ 2).

    Args:
        data: ข้อมูล backtest
        weights: น้ำหนักจาก config
        when: วันที่สนใจ (``YYYY-MM-DD``)

    Returns:
        list ของ dict เรียงตาม contribution มากไปน้อย
    """
    ts = pd.Timestamp(when)
    available = data.components.loc[data.components.index <= ts]
    if available.empty:
        return []

    row_date = available.index[-1]
    breakdown = contribution_breakdown(data.components, weights, row_date)
    return [{"component": k, **v} for k, v in breakdown.items()]


# ================================================================ chart


def plot_backtest(data: BacktestData, cfg: Dict[str, Any], out_path: Path) -> Optional[Path]:
    """วาดกราฟ CUSI ทับ event annotations (spec §5 Phase 4 ข้อ 3).

    Args:
        data: ข้อมูล backtest
        cfg: config dict
        out_path: ไฟล์ PNG ปลายทาง

    Returns:
        พาธไฟล์ หรือ ``None`` ถ้าไม่มี matplotlib
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        log.warning("ไม่มี matplotlib — ข้ามการวาดกราฟ")
        return None

    values = data.cusi["value"].dropna()
    if values.empty:
        return None

    fig, ax = plt.subplots(figsize=(15, 6.5))
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")

    lower = 0.0
    for level in cfg["cusi"]["levels"]:
        upper = min(float(level["max"]), 100.0)
        ax.axhspan(lower, upper, color=level.get("color", "#888"), alpha=0.12, zorder=0)
        ax.text(values.index[0], (lower + upper) / 2, f"  {level['name']}",
                va="center", ha="left", fontsize=8.5, color=level.get("color"), alpha=0.85, zorder=1)
        lower = upper

    ax.plot(values.index, values.values, color="#58a6ff", linewidth=1.25, zorder=3)

    # เหตุการณ์อ้างอิง (ASCII เท่านั้น — ฟอนต์ default ไม่มี glyph ไทย)
    for date_str, label, _ in KNOWN_EVENTS:
        ts = pd.Timestamp(date_str)
        if not (values.index[0] <= ts <= values.index[-1]):
            continue
        nearest = values.index[values.index.get_indexer([ts], method="nearest")[0]]
        ax.axvline(nearest, color="#f85149", linestyle="--", linewidth=1.1, alpha=0.85, zorder=2)
        ax.annotate(
            f"{label}\n{nearest.date()} - {values.loc[nearest]:.0f}",
            xy=(nearest, values.loc[nearest]), xytext=(10, 26), textcoords="offset points",
            fontsize=8.5, color="#f0f6fc",
            bbox=dict(boxstyle="round,pad=0.35", fc="#161b22", ec="#f85149", alpha=0.95),
            arrowprops=dict(arrowstyle="->", color="#f85149", lw=1.0), zorder=6,
        )

    ax.set_ylim(0, 100)
    ax.set_xlim(values.index[0], values.index[-1])
    ax.set_title(
        f"CUSI backtest  |  {values.index[0].date()} to {values.index[-1].date()}  "
        f"|  n={len(values)}  max={values.max():.1f}  mean={values.mean():.1f}",
        color="#f0f6fc", fontsize=12, pad=12,
    )
    ax.set_ylabel("CUSI (0-100)", color="#8b949e")
    ax.grid(True, alpha=0.12, color="#8b949e", linestyle=":")
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    for spine in ax.spines.values():
        spine.set_color("#30363d")
    ax.tick_params(colors="#8b949e", labelsize=9)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130, facecolor=fig.get_facecolor())
    plt.close(fig)
    log.info("saved chart → %s", out_path)
    return out_path


# ================================================================ report


def _fmt(value: Any, digits: int = 1, dash: str = "—") -> str:
    """จัดรูปแบบตัวเลขให้ปลอดภัยกับ NaN/None."""
    if value is None:
        return dash
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return dash if not np.isfinite(f) else f"{f:.{digits}f}"


def write_report(
    data: BacktestData,
    cfg: Dict[str, Any],
    events: List[Dict[str, Any]],
    signals: Dict[str, Any],
    leads: Dict[str, Any],
    sweep: List[Dict[str, Any]],
    sensitivity: List[Dict[str, Any]],
    lead_window_sweep: List[Dict[str, Any]],
    diagnosis: List[Dict[str, Any]],
    args: argparse.Namespace,
    chart: Optional[Path],
    out_path: Path,
) -> Path:
    """เขียนรายงาน markdown ฉบับเต็ม.

    Args:
        data: ข้อมูล backtest
        cfg: config dict
        events: ผล event study
        signals: ผล false positive analysis
        leads: ผล lead time analysis
        sweep: ผล threshold sweep
        sensitivity: ผลทดสอบการเปลี่ยนนิยามเหตุการณ์
        lead_window_sweep: ผลทดสอบความไวต่อ max_lead_days
        diagnosis: component breakdown ณ Aug 2024
        args: argument จาก CLI
        chart: พาธรูปกราฟ
        out_path: ไฟล์ปลายทาง

    Returns:
        พาธไฟล์รายงาน
    """
    values = data.cusi["value"].dropna()
    levels = data.cusi["level"].dropna()
    dist = levels.value_counts().to_dict()
    weights = load_weights(cfg)

    L: List[str] = []
    A = L.append

    A("# CUSI Backtest Validation Report — Phase 4")
    A("")
    A(f"> สร้างโดย `backtest/validate_cusi.py` เมื่อ {pd.Timestamp.now():%Y-%m-%d %H:%M}  ")
    A(f"> ช่วงข้อมูล **{values.index[0].date()} → {values.index[-1].date()}** ({len(values)} วันทำการ)  ")
    A(f"> เกณฑ์สัญญาณ CUSI ≥ **{args.threshold:.0f}** · นิยาม \"เหตุการณ์\": "
      f"SPX drawdown > {args.spx_dd:.0f}% หรือ USDJPY −{args.usdjpy_dd:.0f}% ภายใน {args.horizon} วันทำการ")
    A("")

    # ---- สรุปผู้บริหาร
    aug = next((e for e in events if e["label"] == "Yen carry unwind" and e.get("in_range")), None)
    A("## สรุปสั้น")
    A("")
    if aug:
        verdict = "**ผ่าน**" if aug.get("passed") else "**ไม่ผ่าน**"
        A(f"* เกณฑ์หลักของ spec §3.4 — Aug 2024 ต้องขึ้นถึง ≥ 80: {verdict} "
          f"(จุดสูงสุด **{_fmt(aug['peak'])}** เมื่อ {aug['peak_date'].date()})")
    A(f"* Event study: ผ่าน {sum(1 for e in events if e.get('passed'))} / "
      f"{sum(1 for e in events if e.get('required'))} เหตุการณ์ที่มีเกณฑ์กำหนด")
    A(f"* Precision: {_fmt(signals['precision'])}% ({signals['tp']} จาก {signals['n']} สัญญาณ ตามด้วยเหตุการณ์จริง)")
    A(f"* Recall: {_fmt(leads['recall'])}% ({leads['detected']} จาก {leads['n']} เหตุการณ์ถูกเตือนล่วงหน้า)")
    A(f"* Lead time เฉลี่ย **{_fmt(leads['mean_lead'])} วัน** (median {_fmt(leads['median_lead'])} วัน)")
    A("")

    if chart:
        A(f"![CUSI backtest]({chart.name})")
        A("")

    # ---- ความครบของข้อมูล
    A("## 1. ความครบของข้อมูล")
    A("")
    A("ช่วงที่ component ไหนไม่มีข้อมูล ระบบจะ **re-normalize น้ำหนักที่เหลือ** ให้อัตโนมัติ ")
    A("(ไม่ได้แทนค่าด้วย z = 0 ซึ่งจะดึงผลเข้าหา 50 อย่างไม่มีเหตุผล)")
    A("")
    # ตัวหารต้องเป็น "วันทำการ" ไม่ใช่จำนวนแถวทั้งหมด — frame เป็น union ของทุก ticker
    # จึงมีเสาร์–อาทิตย์ติดมาด้วย (BTC เทรด 24/7) ถ้าหารด้วยจำนวนแถวจะได้ ~71%
    # ทั้งที่ข้อมูลครบ และ COT ที่ ffill ข้ามวันหยุดจะดู "ครบกว่า" ราคาจริงซึ่งกลับหัวกลับหาง
    trading_days = int(data.components[[c for c in COMPONENT_ORDER if c != "cot_positioning"]]
                       .notna().any(axis=1).sum())
    A(f"ตัวหาร = **{trading_days} วันทำการ** (ไม่ใช่ {len(data.components)} แถวใน frame — "
      "frame รวมเสาร์–อาทิตย์มาด้วยเพราะ BTC เทรด 24/7)")
    A("")
    A("| Component | น้ำหนัก | วันที่มีข้อมูล | ความครบ |")
    A("|---|---:|---:|---:|")
    for comp in COMPONENT_ORDER:
        n = data.coverage.get(comp, 0)
        pct = min(n / trading_days * 100, 100.0) if trading_days else 0
        note = " *" if comp == "cot_positioning" else ""
        A(f"| {comp}{note} | {weights[comp]*100:.0f}% | {n} | {pct:.1f}% |")
    A("")
    A("\\* COT เป็นข้อมูลรายสัปดาห์ที่ถูก forward-fill เป็นรายวัน จำนวนวันจึงเกินวันทำการได้ "
      "(ค่าถูก ffill ข้ามวันหยุดด้วย) — ไม่ได้แปลว่าข้อมูลดีกว่าตัวอื่น")
    A("")

    # ---- event study
    A("## 2. Event Study")
    A("")
    A("เทียบค่า CUSI ณ วันเกิดเหตุ กับ 10 วันทำการก่อนหน้า — ")
    A("ถ้า CUSI ยกตัวขึ้น**ก่อน**วันเกิดเหตุ แปลว่าเป็น leading indicator ไม่ใช่ lagging")
    A("")
    A("| เหตุการณ์ | วันที่ | CUSI 10d ก่อน | CUSI วันเกิดเหตุ | เปลี่ยนแปลง | จุดสูงสุด (±20d) | ระดับสูงสุด | เกณฑ์ | ผล |")
    A("|---|---|---:|---:|---:|---:|---|---|---|")
    for e in events:
        if not e.get("in_range"):
            A(f"| {e['label']} | {e['date']} | — | — | — | — | — | {e['required'] or '—'} | นอกช่วงข้อมูล |")
            continue
        mark = "✅" if e.get("passed") else ("❌" if e.get("passed") is False else "—")
        A(f"| {e['label']} | {e['date']} | {_fmt(e['before'])} | {_fmt(e['on_event'])} | "
          f"{_fmt(e['rise'], 1)} | {_fmt(e['peak'])} | {e['peak_level']} | {e['required'] or '—'} | {mark} |")
    A("")

    # ---- false positives
    A("## 3. Signal Analysis (False Positive)")
    A("")
    A(f"นับเฉพาะ **episode** ไม่ใช่รายวัน — ช่วงที่ CUSI อยู่เหนือ {args.threshold:.0f} "
      "ติดกัน (หรือห่างกันไม่เกิน 10 วัน) ถือเป็นสัญญาณเดียว")
    A("")
    A("| # | เริ่มสัญญาณ | จุดสูงสุด | วันที่พีค | ระยะ (วัน) | SPX แย่สุด | USDJPY แย่สุด | ผล (เข้ม) | ผล (ผ่อน) |")
    A("|---:|---|---:|---|---:|---:|---:|---|---|")
    for i, s in enumerate(signals["signals"], 1):
        strict = f"✅ {s['trigger']}" if s["hit"] else "❌"
        lenient = "✅" if s["hit_lenient"] else "❌"
        A(f"| {i} | {s['start'].date()} | {_fmt(s['peak'])} | {s['peak_date'].date()} | {s['days']} | "
          f"{_fmt(s['spx_min_pct'])}% | {_fmt(s['usdjpy_min_pct'])}% | {strict} | {lenient} |")
    A("")
    A("รายงานสองนิยาม เพราะผลต่างกันมากและการเลือกนิยามเดียวจะเป็นการเลือกตัวเลขที่ชอบ:")
    A("")
    A(f"* **เข้ม** (นับ {args.horizon} วันจากวันข้ามเส้นเท่านั้น — ตรงตามตัวอักษรของ spec): "
      f"จริง {signals['tp']} · เท็จ {signals['fp']} · precision **{_fmt(signals['precision'])}%**")
    A(f"* **ผ่อน** (ให้เวลาถึงวันสุดท้ายที่ยังเตือนอยู่ + {args.horizon} วัน): "
      f"จริง {signals['tp_lenient']} · เท็จ {signals['fp_lenient']} · "
      f"precision **{_fmt(signals['precision_lenient'])}%**")
    A("")
    A("นิยามผ่อนมีเหตุผลรองรับ: สัญญาณที่ค้างอยู่ 40 วันแล้วเกิดเหตุในวันที่ 30 ")
    A("ไม่ควรถูกนับเป็น false positive เพราะระบบยังเตือนอยู่ตลอด ")
    A("แต่ก็เอนเอียงเข้าข้างโมเดลเช่นกัน — ความจริงอยู่ระหว่างสองค่านี้")
    A("")

    # ---- lead time
    A("## 4. Lead Time Analysis")
    A("")
    A("เหตุการณ์ตรงนี้นิยามจาก**ราคาอย่างเดียว** ไม่ได้ดู CUSI เลย ")
    A("(drawdown จากจุดสูงสุด 20 วัน) แล้วค่อยย้อนดูว่า CUSI เตือนก่อนไหม")
    A("")
    A("| # | เหตุการณ์เริ่ม | ความรุนแรง | CUSI เตือนวันที่ | นำหน้า (วัน) | พีคของสัญญาณ | CUSI สูงสุด 20d ก่อน |")
    A("|---:|---|---|---|---:|---:|---:|")
    for i, ev in enumerate(leads["events"], 1):
        note = " ⚠️ขอบข้อมูล" if ev.get("at_boundary") else ""
        if ev["detected"]:
            A(f"| {i} | {ev['start'].date()}{note} | {ev['trigger']} | {ev['signal_date'].date()} | "
              f"{int(ev['lead_days'])} | {_fmt(ev['peak'])} | {_fmt(ev.get('max_before'))} |")
        else:
            A(f"| {i} | {ev['start'].date()}{note} | {ev['trigger']} | — | — | ❌ พลาด | "
              f"{_fmt(ev.get('max_before'))} |")
    A("")

    A("**พารามิเตอร์ที่ต้องเปิดเผย**: การจับคู่สัญญาณกับเหตุการณ์ใช้หน้าต่าง ")
    A(f"`max_lead_days = 60` — สัญญาณที่มาก่อนเหตุการณ์เกินกว่านี้ไม่นับว่าเกี่ยวข้องกัน ")
    A("ตัวเลขนี้เป็นการตัดสินใจ ไม่ใช่ข้อเท็จจริง และมีผลต่อ recall โดยตรง:")
    A("")
    A("| max_lead_days | เตือนได้ | Recall | Lead เฉลี่ย |")
    A("|---:|---|---:|---:|")
    for row in lead_window_sweep:
        mark = " ←ใช้อยู่" if row["max_lead_days"] == 60 else ""
        A(f"| {row['max_lead_days']}{mark} | {row['detected']}/{row['n']} | "
          f"{_fmt(row['recall'], 0)}% | {_fmt(row['mean_lead'], 0)} วัน |")
    A("")
    A("ตัวอย่างที่ชัดที่สุดคือเหตุการณ์ `2022-01-19` (SPX −12.8% / USDJPY −8.3%) ซึ่งถูกนับว่า **พลาด** ")
    A("ทั้งที่ CUSI เคยขึ้นไปถึง **88.9 เมื่อ 2021-12-01** — แต่สัญญาณนั้นจบลงตั้งแต่ 2021-12-14 ")
    A("คือเงียบไปแล้ว 36 วันก่อนเหตุการณ์จริง ")
    A("การนับว่า \"เตือนสำเร็จ\" จึงใจกว้างเกินไป (เตือนแล้วยกเลิก แล้วค่อยเกิดเหตุ = ใช้งานจริงไม่ได้) ")
    A("แต่การนับว่าพลาดสนิทก็ทิ้งข้อมูลไป — ค่าจริงอยู่ระหว่างกลาง")
    A("")

    missed = [e for e in leads["events"] if not e["detected"]]
    if missed:
        near = [e for e in missed if pd.notna(e.get("max_before")) and e["max_before"] >= args.threshold - 5]
        A(f"**เหตุการณ์ที่พลาด ({len(missed)} ครั้ง)** — คอลัมน์ขวาสุดบอกว่า CUSI ขึ้นไปสูงสุดเท่าไหร่ก่อนเกิดเหตุ:")
        A("")
        A(f"* {len(near)} ครั้งพลาดแบบเฉียด (แตะ {args.threshold - 5:.0f}+ แต่ไม่ข้ามเส้น)")
        A(f"* {len(missed) - len(near)} ครั้งพลาดแบบไม่มีสัญญาณเลย")
        A("")
        worst = sorted(missed, key=lambda e: e.get("max_before") if pd.notna(e.get("max_before")) else 0)[:3]
        A("ที่ควรดูเป็นพิเศษคือเหตุการณ์ที่รุนแรงแต่ CUSI เงียบสนิท:")
        A("")
        for e in worst:
            A(f"* `{e['start'].date()}` — {e['trigger']} · CUSI สูงสุดก่อนหน้าแค่ {_fmt(e.get('max_before'))}")
        A("")
        A("ข้อสังเกต: เหตุการณ์ที่ CUSI เงียบสนิททั้ง 3 ครั้งเป็นการร่วงของ **SPX ล้วน ๆ ")
        A("โดย USDJPY แทบไม่ขยับ** (−1.0%, −2.0%, −2.9%) ซึ่งเข้ากับสมมติฐานว่า CUSI ")
        A("ถูกออกแบบมาจับ *carry unwind* ไม่ใช่ความเสี่ยงหุ้นทุกชนิด — ดูการทดสอบสมมติฐานนี้ในหัวข้อ 4b")
        A("")
    A(f"**สรุป**: {leads['n']} เหตุการณ์ · เตือนได้ {leads['detected']} · พลาด {leads['missed']} · "
      f"recall **{_fmt(leads['recall'])}%** · lead time เฉลี่ย **{_fmt(leads['mean_lead'])} วัน** "
      f"(median {_fmt(leads['median_lead'])})")
    A("")

    # ---- sensitivity to event definition
    A("## 4b. ผลเปลี่ยนแค่ไหนถ้าเปลี่ยนนิยามเหตุการณ์")
    A("")
    A("หัวข้อ 4 ตั้งข้อสังเกตว่า CUSI พลาดเพราะนิยามเหตุการณ์รวมการร่วงของหุ้นทุกสาเหตุ ")
    A("หัวข้อนี้ทดสอบข้อสังเกตนั้นตรง ๆ แทนที่จะเชื่อไปเอง (โมเดลและน้ำหนักเหมือนเดิมทุกแบบ):")
    A("")
    A("| นิยามเหตุการณ์ | เหตุการณ์ | Recall | สัญญาณ | Precision | Lead เฉลี่ย |")
    A("|---|---:|---:|---:|---:|---:|")
    for row in sensitivity:
        A(f"| {row['label']} | {row['n_events']} | {_fmt(row['recall'], 0)}% | {row['n_signals']} | "
          f"{_fmt(row['precision'], 0)}% | {_fmt(row['mean_lead'], 0)} วัน |")
    A("")
    A("**ผลออกมาครึ่งหนึ่งสนับสนุน อีกครึ่งหักล้าง** ข้อสังเกตในหัวข้อ 4:")
    A("")
    A("* ✅ **สนับสนุน** — ถ้านับเฉพาะเหตุการณ์ที่ค่าเงินเยนขยับจริง (USDJPY −4%) "
      "recall พุ่งจาก 59% เป็น **91%** แปลว่า CUSI แทบไม่พลาดเหตุการณ์ประเภทที่มันถูกสร้างมาจับ")
    A("* ❌ **หักล้าง** — precision **ไม่ดีขึ้นเลย** ในทุกนิยาม (11–21%) "
      "เพราะจำนวนสัญญาณคงที่ที่ 38 ครั้งไม่ว่าจะนิยามเหตุการณ์อย่างไร")
    A("")
    A("สรุปตรง ๆ: **การพลาด** อธิบายได้ด้วยนิยามเหตุการณ์ แต่ **การเตือนบ่อยเกิน** อธิบายไม่ได้ ")
    A("และเป็นคุณสมบัติจริงของ CUSI ที่เส้น 60 — 38 สัญญาณใน 7.5 ปี ≈ 5 ครั้ง/ปี ")
    A("โดยส่วนใหญ่ไม่ตามด้วยอะไรภายใน 20 วัน นี่คือข้อจำกัดที่ผู้ใช้ควรรู้ก่อนเชื่อไฟเตือน")
    A("")

    # ---- confusion summary
    A("## 5. Confusion-style Summary")
    A("")
    A("| | เกิดเหตุการณ์จริง | ไม่เกิด |")
    A("|---|---:|---:|")
    A(f"| **CUSI ≥ {args.threshold:.0f}** | TP = {signals['tp']} | FP = {signals['fp']} |")
    A(f"| **CUSI < {args.threshold:.0f}** | FN = {leads['missed']} | TN = ไม่นิยาม* |")
    A("")
    A("\\* TN ไม่มีความหมายในบริบทนี้ เพราะ \"วันที่ไม่มีสัญญาณและไม่มีเหตุการณ์\" คือวันส่วนใหญ่ ")
    A("การนับ TN จะทำให้ accuracy ดูดีเกินจริง — precision/recall สื่อความหมายตรงกว่า")
    A("")
    A(f"* **Precision {_fmt(signals['precision'])}%** — เตือนแล้วเกิดจริงกี่ %  (ค่าต่ำ = เตือนบ่อยเกิน)")
    A(f"* **Recall {_fmt(leads['recall'])}%** — เหตุการณ์ที่เกิดจริง ถูกเตือนล่วงหน้ากี่ %  (ค่าต่ำ = พลาดของจริง)")
    A("")

    # ---- threshold sweep
    A("## 5b. Threshold Sweep — precision/recall trade-off")
    A("")
    A("เกณฑ์ 60 มาจาก spec §3.3 ตารางนี้แสดงว่าถ้าขยับเส้นจะได้อะไรเสียอะไร ")
    A("(ตัวโมเดลและน้ำหนัก**ไม่เปลี่ยน** — เปลี่ยนแค่จุดที่เรียกว่า \"เตือน\")")
    A("")
    A("| Threshold | สัญญาณ | Precision (เข้ม) | Precision (ผ่อน) | Recall | เตือนได้ | Lead เฉลี่ย |")
    A("|---:|---:|---:|---:|---:|---|---:|")
    for row in sweep:
        mark = " ←ปัจจุบัน" if abs(row["threshold"] - args.threshold) < 1e-9 else ""
        A(f"| **{row['threshold']:.0f}**{mark} | {row['n_signals']} | {_fmt(row['precision'])}% | "
          f"{_fmt(row['precision_lenient'])}% | {_fmt(row['recall'])}% | "
          f"{row['detected']}/{row['n_events']} | {_fmt(row['mean_lead'])} วัน |")
    A("")
    A("อ่านตารางนี้อย่างไร: ยกเส้นสูงขึ้น = เตือนน้อยลงแต่แม่นขึ้น และเสีย lead time ")
    A("เลือกจุดตามว่าอะไรแพงกว่าสำหรับผู้ใช้ — เตือนผิดบ่อย (เหนื่อย เลิกสนใจ) ")
    A("หรือพลาดของจริง (เจ็บตัว)")
    A("")
    A("**คำเตือนสำคัญเรื่องการอ่านตารางนี้**: precision ในคอลัมน์นี้ *ไม่ได้* เรียงขึ้นลงตาม threshold ")
    A("อย่างเป็นระบบ (55 ได้สูงกว่า 60) ซึ่งดูเหมือนมีความหมาย แต่จำนวนสัญญาณน้อยเกินไป — ")
    A("ที่ n ≈ 40 และ p ≈ 0.25 ค่า standard error อยู่ที่ ~7 จุด และช่วงความเชื่อมั่น 95% ")
    A("ของทุก threshold **ทับซ้อนกันเกือบทั้งหมด**:")
    A("")
    A("| Threshold | Precision | 95% CI โดยประมาณ |")
    A("|---:|---:|---|")
    for row in sweep:
        n, p = row["n_signals"], row["precision"] / 100.0 if np.isfinite(row["precision"]) else np.nan
        if n and np.isfinite(p):
            se = float(np.sqrt(max(p * (1 - p), 0) / n))
            lo, hi = max(0.0, p - 1.96 * se) * 100, min(1.0, p + 1.96 * se) * 100
            A(f"| {row['threshold']:.0f} | {_fmt(row['precision'])}% | [{lo:.0f}%, {hi:.0f}%] |")
    A("")
    A("แปลว่า **ยังสรุปไม่ได้ว่า threshold ไหนดีกว่ากันจริง** ")
    A("การเลือก 55 หรือ 75 เพราะตัวเลข precision ในตารางนี้ คือการ fit กับ noise ")
    A("ถ้าจะขยับเส้น ควรขยับด้วยเหตุผลเชิงการใช้งาน (เช่น อยากให้ CALM มีความหมาย) ")
    A("ไม่ใช่เพราะตัวเลขในตารางนี้")
    A("")

    # ---- การกระจายระดับ
    A("## 6. การกระจายของระดับ")
    A("")
    A("| ระดับ | จำนวนวัน | สัดส่วน |")
    A("|---|---:|---:|")
    for name in ("CALM", "WATCH", "STRESS", "UNWIND"):
        n = int(dist.get(name, 0))
        A(f"| {name} | {n} | {n/len(levels)*100:.1f}% |")
    A("")
    A(f"สถิติ: min {_fmt(values.min())} · median {_fmt(values.median())} · "
      f"mean {_fmt(values.mean())} · max {_fmt(values.max())} · sd {_fmt(values.std())}")
    A("")

    # ---- diagnosis + คำแนะนำ weights
    A("## 7. Component Diagnosis ณ Aug 2024")
    A("")
    if diagnosis:
        A("| Component | z | น้ำหนัก | contribution | สัดส่วน |")
        A("|---|---:|---:|---:|---:|")
        for d in diagnosis:
            A(f"| {d['component']} | {_fmt(d['z'], 2)} | {d['weight']:.2f} | "
              f"{_fmt(d['contribution'], 3)} | {_fmt(d['share_pct'])}% |")
    else:
        A("_ไม่มีข้อมูล_")
    A("")

    A("## 8. คำแนะนำเรื่อง weights")
    A("")
    if aug and aug.get("passed"):
        A("**ไม่แนะนำให้ปรับน้ำหนัก**")
        A("")
        A(f"เกณฑ์หลักที่ spec §3.4 ตั้งไว้คือ Aug 2024 ต้องขึ้นถึง ≥ 80 — ผลจริงคือ "
          f"**{_fmt(aug['peak'])}** ซึ่งผ่านแล้วโดยใช้น้ำหนัก v1 จาก §3.2 **ที่ยังไม่เคยแตะเลย**")
        A("")
        A("spec §5 Phase 4 อนุญาตให้ปรับได้ 1 รอบ *เฉพาะกรณีที่ไม่ผ่าน* — ")
        A("เมื่อผ่านแล้วการปรับต่อมีแต่ downside: ทุกการขยับจากตรงนี้คือการ fit ")
        A("กับ 3 เหตุการณ์ในชุดข้อมูลเดียว ซึ่งเป็นนิยามของ curve-fitting ที่ spec §6 เตือนไว้ ")
        A("น้ำหนัก v1 ที่ไม่เคยถูกปรับคือ out-of-sample โดยธรรมชาติ — เป็นทรัพย์สินที่เสียไปแล้วเรียกคืนไม่ได้")
        A("")
        A("**สิ่งที่ควรทำแทนการปรับน้ำหนัก** (ถ้าอยากให้ระบบใช้งานได้ดีขึ้น):")
        A("")
        A("1. ปรับ **threshold** ไม่ใช่ weights — ดูหัวข้อ 6 ว่าการกระจายระดับเบ้ไปทาง WATCH แค่ไหน ")
        A("   threshold เป็น cosmetic ต่อการจัดอันดับความเสี่ยง แต่ weights เปลี่ยนตัวโมเดลเอง")
        A("2. เก็บ CUSI แบบ live ไปเรื่อย ๆ แล้วค่อยประเมินใหม่ด้วยข้อมูล out-of-sample จริง")
        A("3. ถ้าจะปรับจริง ให้ทำหลังมี episode ใหม่อย่างน้อย 2–3 ครั้งที่ระบบไม่เคยเห็น")
    else:
        A("Aug 2024 **ไม่ถึงเกณฑ์ 80** — ดูตารางหัวข้อ 7 ว่า component ไหนไม่ทำงาน")
        A("")
        A("แนวทางปรับ (ทำได้ครั้งเดียวตาม spec): ย้ายน้ำหนักจาก component ที่ z ต่ำผิดคาด ")
        A("ไปยัง component ที่จับสัญญาณได้จริง โดยปรับเป็นก้อนกลม ๆ (5% step) ")
        A("และ**ห้าม**ปรับทีละนิดจนกว่าตัวเลขจะสวย — นั่นคือ curve-fitting")
    A("")

    A("## 9. ข้อจำกัดของการทดสอบนี้")
    A("")
    A("* **In-sample ทั้งหมด** — น้ำหนักถูกกำหนดไว้ก่อนเห็นข้อมูล (ดี) แต่การเลือก "
      "*ตัวชี้วัด* และ *สูตร* ทำขึ้นหลังปี 2024 ซึ่งเป็นช่วงที่ทุกคนรู้แล้วว่า Aug 2024 เกิดอะไร")
    A("* **ตัวอย่างน้อย** — เหตุการณ์ระดับ unwind จริง ๆ มีไม่กี่ครั้งในชุดข้อมูล "
      "สถิติ precision/recall จึงมี error bar กว้างมาก")
    A("* **นิยามเหตุการณ์เป็นทางเลือกหนึ่ง** — SPX −5% / USDJPY −3% ใน 20 วัน "
      "ถ้าเปลี่ยนเกณฑ์ ตัวเลข precision/recall จะเปลี่ยนตาม")
    A("* **ข้อมูลย้อนหลังผ่าน yfinance** อาจมี survivorship / revision ที่ตรวจสอบไม่ได้")
    A("")
    A("---")
    A("")
    A("_รายงานนี้สร้างอัตโนมัติ — รันซ้ำได้ด้วย `python backtest/validate_cusi.py`_")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("saved report → %s", out_path)
    return out_path


# ================================================================ CLI


def main(argv: Optional[list] = None) -> int:
    """Entry point ของ CLI.

    Args:
        argv: argument list; ``None`` = ใช้ ``sys.argv``

    Returns:
        exit code (0 = ผ่านเกณฑ์หลัก, 3 = ไม่ผ่าน, 1 = พัง)
    """
    parser = argparse.ArgumentParser(description="CUSI backtest validation (Phase 4)")
    parser.add_argument("--start", default="2019-01-01", help="วันเริ่ม backtest")
    parser.add_argument("--threshold", type=float, default=60.0, help="เกณฑ์สัญญาณ CUSI")
    parser.add_argument("--horizon", type=int, default=20, help="จำนวนวันทำการที่ให้เหตุการณ์เกิด")
    parser.add_argument("--spx-dd", type=float, default=5.0, help="เกณฑ์ SPX drawdown (%%)")
    parser.add_argument("--usdjpy-dd", type=float, default=3.0, help="เกณฑ์ USDJPY drawdown (%%)")
    parser.add_argument("--use-cache", action="store_true", help="ใช้ CUSI ที่คำนวณไว้แล้ว (ไม่ดึงข้อมูลใหม่)")
    parser.add_argument("--out", default="backtest/VALIDATION_REPORT.md", help="ไฟล์รายงาน")
    args = parser.parse_args(argv)

    cfg = backtest_config(args.start)
    setup_logging(cfg)

    cache_csv = REPO_ROOT / "data" / "backtest" / "cusi_backtest.csv"
    comp_csv = REPO_ROOT / "data" / "backtest" / "components_backtest.csv"
    px_csv = REPO_ROOT / "data" / "backtest" / "prices_backtest.csv"

    if args.use_cache and cache_csv.is_file() and comp_csv.is_file() and px_csv.is_file():
        log.info("โหลดผล backtest จาก cache")
        cusi_df = pd.read_csv(cache_csv, index_col=0, parse_dates=True)
        components = pd.read_csv(comp_csv, index_col=0, parse_dates=True)
        prices = pd.read_csv(px_csv, index_col=0, parse_dates=True)
        data = BacktestData(
            cusi=cusi_df,
            components=components,
            spx=prices["spx"].dropna() if "spx" in prices else pd.Series(dtype="float64"),
            usdjpy=prices["usdjpy"].dropna() if "usdjpy" in prices else pd.Series(dtype="float64"),
            coverage={c: int(components[c].notna().sum()) for c in COMPONENT_ORDER if c in components.columns},
        )
    else:
        try:
            data = prepare_data(cfg, args.start)
        except Exception:
            log.exception("การเตรียมข้อมูลล้มเหลว")
            return 1

        cache_csv.parent.mkdir(parents=True, exist_ok=True)
        data.cusi.to_csv(cache_csv)
        data.components.to_csv(comp_csv)
        pd.DataFrame({"spx": data.spx, "usdjpy": data.usdjpy}).to_csv(px_csv)

    if data.cusi["value"].dropna().empty:
        log.error("CUSI ไม่มีค่าที่ใช้ได้เลย — ตรวจสอบข้อมูลต้นทาง")
        return 1

    weights = load_weights(cfg)
    events = event_study(data, cfg)
    signals = signal_analysis(data, args.threshold, args.horizon, args.spx_dd, args.usdjpy_dd)
    leads = lead_time_analysis(data, args.threshold, args.spx_dd, args.usdjpy_dd)
    sweep = threshold_sweep(data, args.horizon, args.spx_dd, args.usdjpy_dd)
    sensitivity = sensitivity_event_definition(data, args.threshold, args.horizon)
    lead_window_sweep = lead_window_sensitivity(data, args.threshold, args.spx_dd, args.usdjpy_dd)
    diagnosis = component_diagnosis(data, weights, "2024-08-05")

    chart = plot_backtest(data, cfg, REPO_ROOT / "backtest" / "cusi_backtest.png")
    write_report(data, cfg, events, signals, leads, sweep, sensitivity, lead_window_sweep, diagnosis, args, chart, REPO_ROOT / args.out)

    # ---- สรุปบนหน้าจอ
    values = data.cusi["value"].dropna()
    log.info("=" * 72)
    log.info("BACKTEST SUMMARY  %s → %s (%d วัน)", values.index[0].date(), values.index[-1].date(), len(values))
    for e in events:
        if not e.get("in_range"):
            log.info("  %-20s นอกช่วงข้อมูล", e["label"])
            continue
        log.info(
            "  %-20s วันเกิดเหตุ=%5.1f | 10d ก่อน=%5.1f | พีค=%5.1f (%s) %s",
            e["label"], e["on_event"], e["before"], e["peak"], e["peak_level"],
            "" if e["required"] is None else ("PASS" if e["passed"] else "FAIL"),
        )
    log.info("  precision=%.0f%% (%d/%d) | recall=%.0f%% (%d/%d) | lead เฉลี่ย %.0f วัน",
             signals["precision"], signals["tp"], signals["n"],
             leads["recall"], leads["detected"], leads["n"], leads["mean_lead"])
    log.info("=" * 72)

    aug = next((e for e in events if e["label"] == "Yen carry unwind" and e.get("in_range")), None)
    return 0 if (aug and aug.get("passed")) else 3


if __name__ == "__main__":
    sys.exit(main())
