"""Phase 6E — สร้าง data/episodes.json สำหรับ Historical Episode Explorer.

รันครั้งเดียว (หรือเมื่อเพิ่มเหตุการณ์ใหม่ใน config) — ข้อมูลอดีตไม่เปลี่ยน
จึงไม่ต้องเข้า workflow ใด ๆ::

    python backtest/build_episodes.py
    python backtest/build_episodes.py --dry-run    # ดูสรุปโดยไม่เขียนไฟล์

ใช้ท่อเดียวกับ backtest (cache แยกที่ data/backtest/raw ครอบคลุม 2017→ปัจจุบัน)
แล้วตัดหน้าต่าง [anchor − pre_days, anchor + post_days] เป็นวันทำการต่อเหตุการณ์

การ normalize ต่อ series (เพื่อ replay เทียบข้ามเหตุการณ์ได้):
  * ราคา (usdjpy, audjpy, nikkei) → index = 100 ณ day 0
  * VIX / spread / realized vol / CUSI / COT → ค่าดิบ (ระดับมีความหมายอยู่แล้ว)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.validate_cusi import backtest_config  # noqa: E402
from pipeline.build_json import build_indicator_frame  # noqa: E402
from pipeline.config import REPO_ROOT, get_logger, load_config, resolve_path, setup_logging  # noqa: E402
from pipeline.cusi import build_cusi_frame  # noqa: E402
from pipeline.fetch_cot import cot_series, fetch_cot_jpy  # noqa: E402
from pipeline.fetch_jgb import fetch_jgb10y  # noqa: E402
from pipeline.fetch_market import fetch_market_data  # noqa: E402

log = get_logger(__name__)

#: series ที่ส่งออกต่อเหตุการณ์: (ชื่อใน JSON, คอลัมน์ใน frame, normalize เป็น index100?)
SERIES_SPEC = [
    ("cusi", "__cusi__", False),
    ("usdjpy", "usdjpy", True),
    ("audjpy", "audjpy", True),
    ("nikkei", "nikkei", True),
    ("vix", "vix", False),
    ("spread_us_jp", "spread_us_jp", False),
    ("fx_realized_vol_10d", "fx_realized_vol_10d", False),
    ("cot_net_k", "__cot__", False),
]


def _round_list(values: pd.Series, digits: int = 3) -> List[Optional[float]]:
    """แปลง Series เป็น list ที่ JSON-safe (NaN → None).

    Args:
        values: input
        digits: ตำแหน่งทศนิยม

    Returns:
        list ของ float/None
    """
    out: List[Optional[float]] = []
    for v in values:
        out.append(round(float(v), digits) if pd.notna(v) and np.isfinite(v) else None)
    return out


def extract_episode(
    frame: pd.DataFrame,
    episode: Dict[str, Any],
    pre_days: int,
    post_days: int,
) -> Optional[Dict[str, Any]]:
    """ตัดหน้าต่างรอบ anchor ของเหตุการณ์หนึ่ง แล้ว normalize.

    Args:
        frame: indicator frame (มีคอลัมน์ __cusi__ / __cot__ เติมแล้ว)
        episode: dict จาก config (id, anchor, during_days, title, desc)
        pre_days: วันทำการก่อน anchor
        post_days: วันทำการหลัง anchor

    Returns:
        dict ของ episode หรือ ``None`` ถ้า anchor อยู่นอกช่วงข้อมูล
    """
    anchor = pd.Timestamp(episode["anchor"])
    idx = frame.index
    pos_candidates = idx[idx <= anchor]
    if not len(pos_candidates):
        log.warning("%s: anchor %s อยู่ก่อนช่วงข้อมูล — ข้าม", episode["id"], episode["anchor"])
        return None
    pos = idx.get_loc(pos_candidates[-1])

    lo, hi = max(0, pos - pre_days), min(len(idx) - 1, pos + post_days)
    window = frame.iloc[lo : hi + 1]
    offsets = list(range(lo - pos, hi - pos + 1))
    anchor_idx = pos - lo          # ตำแหน่ง day 0 ภายในหน้าต่าง
    # (ห้ามใช้ pre_days ตรง ๆ — หน้าต่างที่โดน clip ที่ขอบข้อมูลจะสั้นกว่านั้น)

    series: Dict[str, List[Optional[float]]] = {}
    for name, col, normalize in SERIES_SPEC:
        if col not in window.columns:
            continue
        s = window[col].astype("float64")
        if normalize:
            base = s.iloc[anchor_idx] if pd.notna(s.iloc[anchor_idx]) else s.dropna().iloc[0]
            if pd.isna(base) or base == 0:
                continue
            s = s / base * 100.0
        series[name] = _round_list(s)

    cusi_window = window["__cusi__"].dropna()
    during = int(episode.get("during_days", 5))
    return {
        "id": episode["id"],
        "title": episode["title"],
        "desc": episode.get("desc", ""),
        "anchor": episode["anchor"],
        "dates": [str(d.date()) for d in window.index],
        "day_offset": offsets,
        "phases": {"pre": [offsets[0], -1], "during": [0, during],
                   "post": [during + 1, offsets[-1]]},
        "series": series,
        "stats": {
            "cusi_peak": round(float(cusi_window.max()), 1) if len(cusi_window) else None,
            # ต้องใช้ idxmax (ข้าม NaN) — np.argmax บน array ที่มี NaN
            # คืนตำแหน่ง NaN ตัวแรกเพราะ NaN ชนะการเปรียบเทียบ
            "cusi_peak_offset": int(offsets[window.index.get_loc(cusi_window.idxmax())])
            if len(cusi_window) else None,
            "cusi_at_anchor": round(float(window["__cusi__"].iloc[anchor_idx]), 1)
            if pd.notna(window["__cusi__"].iloc[anchor_idx]) else None,
        },
    }


def main(argv: Optional[list] = None) -> int:
    """Entry point.

    Args:
        argv: argument list

    Returns:
        exit code
    """
    parser = argparse.ArgumentParser(description="สร้าง data/episodes.json (Phase 6E)")
    parser.add_argument("--dry-run", action="store_true", help="สรุปอย่างเดียว ไม่เขียนไฟล์")
    args = parser.parse_args(argv)

    base_cfg = load_config()
    ep_cfg = base_cfg["episodes"]
    earliest = min(pd.Timestamp(e["anchor"]) for e in ep_cfg["list"])
    # ต้องมี warm-up z 1 ปีก่อน pre-window ของเหตุการณ์แรกสุด
    start = (earliest - pd.Timedelta(days=30 + ep_cfg["window_pre_days"] * 2)).strftime("%Y-%m-%d")
    cfg = backtest_config(start)
    setup_logging(cfg)

    log.info("=== ดึงข้อมูลย้อนหลัง (ใช้ cache backtest ถ้ามี) ===")
    market = fetch_market_data(cfg, include_intraday=False)
    jgb_df, _ = fetch_jgb10y(cfg)
    cot_df = fetch_cot_jpy(cfg)

    frame = build_indicator_frame(market, jgb_df, cfg)
    cusi_df, _ = build_cusi_frame(frame, cot_df, cfg)
    frame = frame.copy()
    frame["__cusi__"] = cusi_df["value"]

    # COT: ffill จากวันประกาศ (available_from) — convention เดียวกับ CUSI, หน่วยพันสัญญา
    net = cot_series(cot_df, use_available_from=True) / 1000.0
    net.index = pd.DatetimeIndex(net.index)
    frame["__cot__"] = net.reindex(net.index.union(frame.index)).ffill(limit=10).reindex(frame.index)

    episodes = []
    for e in ep_cfg["list"]:
        out = extract_episode(frame, e, int(ep_cfg["window_pre_days"]), int(ep_cfg["window_post_days"]))
        if out:
            episodes.append(out)
            log.info("✓ %-10s %s | จุด %d | CUSI peak %.1f (day %+d)",
                     out["id"], out["anchor"], len(out["dates"]),
                     out["stats"]["cusi_peak"] or -1, out["stats"]["cusi_peak_offset"] or 0)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "สร้างด้วย backtest/build_episodes.py — ข้อมูลอดีตไม่เปลี่ยน รันซ้ำเมื่อเพิ่มเหตุการณ์เท่านั้น",
        "window": {"pre_days": ep_cfg["window_pre_days"], "post_days": ep_cfg["window_post_days"]},
        "episodes": episodes,
    }

    if args.dry_run:
        log.info("dry-run: %d เหตุการณ์ ไม่เขียนไฟล์", len(episodes))
        return 0

    out_path = resolve_path(base_cfg, "episodes_json", mkdir=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    log.info("เขียน %s (%.1f KB, %d เหตุการณ์)", out_path.name,
             out_path.stat().st_size / 1024, len(episodes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
