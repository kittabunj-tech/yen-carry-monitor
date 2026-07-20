"""Orchestrator ของ pipeline — CLI: ``python pipeline/run_all.py --mode hourly``.

โหมดการทำงาน (spec §4.2):
    hourly  ราคา + intraday 1h, ใช้ JGB/COT จาก cache (เร็ว, รันทุกชั่วโมง)
    daily   ราคา daily เต็ม + MOF JGB สด (หลัง US close)
    weekly  daily ทั้งหมด + ดึง COT ใหม่จาก CFTC (เสาร์เช้า UTC)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

# ให้รันได้ทั้ง `python pipeline/run_all.py` และ `python -m pipeline.run_all`
if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.build_json import build_all  # noqa: E402
from pipeline.config import get_logger, load_config, resolve_path, setup_logging  # noqa: E402
from pipeline.fetch_cot import OUTPUT_COLUMNS as COT_COLUMNS  # noqa: E402
from pipeline.fetch_cot import fetch_cot_jpy  # noqa: E402
from pipeline.fetch_jgb import fetch_jgb10y  # noqa: E402
from pipeline.fetch_market import fetch_market_data  # noqa: E402

log = get_logger(__name__)

MODES = ("hourly", "daily", "weekly")


def _load_cached_cot(cfg: Dict[str, Any]) -> pd.DataFrame:
    """อ่าน COT จาก parquet cache; ถ้าไม่มี cache ให้ดึงสดแทน.

    ปกติโหมด hourly/daily ไม่ยิง CFTC ใหม่ (ข้อมูลออกสัปดาห์ละครั้ง)
    แต่ต้อง **self-healing** เมื่อ cache หาย — บน GitHub Actions runner
    เป็นเครื่องใหม่ทุกครั้ง ถ้า cache ยังไม่ถูก restore จะไม่มีไฟล์นี้เลย
    การคืน DataFrame ว่างจะทำให้ component COT หายและติดธง missing ทุกชั่วโมง
    (พฤติกรรมเดียวกับ :func:`_load_cached_jgb`)

    Args:
        cfg: config dict

    Returns:
        DataFrame ของ COT; ว่างเฉพาะกรณีที่ดึงสดก็ยังไม่สำเร็จ
    """
    path = resolve_path(cfg, "cot_parquet")
    if not path.is_file():
        log.warning("no COT cache — fetching fresh from CFTC")
        return fetch_cot_jpy(cfg)
    try:
        df = pd.read_parquet(path)
        log.info("COT from cache: %d rows", len(df))
        return df
    except Exception as exc:
        log.warning("COT cache read failed: %s — fetching fresh", exc)
        return fetch_cot_jpy(cfg)


def _load_cached_jgb(cfg: Dict[str, Any]) -> tuple:
    """อ่าน JGB จาก parquet cache (โหมด hourly).

    Args:
        cfg: config dict

    Returns:
        tuple ``(df, source)``
    """
    path = resolve_path(cfg, "jgb_parquet")
    if not path.is_file():
        log.info("no JGB cache — fetching fresh")
        return fetch_jgb10y(cfg)
    try:
        df = pd.read_parquet(path)
        source = str(df["source"].iloc[-1]) if "source" in df.columns and len(df) else "cache"
        log.info("JGB from cache: %d rows (source=%s)", len(df), source)
        return df, source
    except Exception as exc:
        log.warning("JGB cache read failed: %s — fetching fresh", exc)
        return fetch_jgb10y(cfg)


def run(mode: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """รัน pipeline หนึ่งรอบตามโหมดที่กำหนด.

    Args:
        mode: ``"hourly"`` / ``"daily"`` / ``"weekly"``
        cfg: config dict; ``None`` = โหลดเอง

    Returns:
        payload ของ latest.json

    Raises:
        ValueError: ถ้า mode ไม่ถูกต้อง
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")

    cfg = cfg if cfg is not None else load_config()
    started = time.perf_counter()
    log.info("=== yen-carry-monitor pipeline: mode=%s ===", mode)

    # ---- 1) ราคาตลาด
    market = fetch_market_data(cfg, include_intraday=(mode == "hourly"))

    # ---- 2) JGB 10Y
    if mode == "hourly":
        jgb_df, jgb_source = _load_cached_jgb(cfg)
    else:
        jgb_df, jgb_source = fetch_jgb10y(cfg)

    # ---- 3) COT (weekly เท่านั้นที่ยิง API — CFTC ออกสัปดาห์ละครั้ง)
    if mode == "weekly":
        cot_df = fetch_cot_jpy(cfg)
    else:
        cot_df = _load_cached_cot(cfg)

    # ---- 4) ประกอบ JSON (รวม CUSI)
    # hourly ไม่เขียน cusi_history ใหม่ เพื่อลด commit churn (spec §4.7)
    latest = build_all(
        market,
        jgb_df,
        jgb_source,
        cot_df,
        cfg,
        write_cot=(mode == "weekly"),
        write_cusi_history=(mode != "hourly"),
        fetch_network_events=(mode != "hourly"),
    )

    elapsed = time.perf_counter() - started
    dq = latest["data_quality"]
    cusi = latest.get("cusi")
    log.info(
        "=== done in %.1fs | CUSI=%s (%s) | tickers %d/%d | jgb=%s | missing=%s | stale=%s ===",
        elapsed,
        f"{cusi['value']:.1f}" if cusi else "n/a",
        cusi["level"] if cusi else "n/a",
        dq["tickers_ok"],
        dq["tickers_total"],
        dq["jgb_source"],
        dq["missing"] or "none",
        dq["stale"] or "none",
    )
    return latest


def main(argv: Optional[list] = None) -> int:
    """Entry point ของ CLI.

    Args:
        argv: argument list; ``None`` = ใช้ ``sys.argv``

    Returns:
        exit code (0 = สำเร็จ, 1 = ล้มเหลว, 2 = สำเร็จบางส่วน)
    """
    parser = argparse.ArgumentParser(
        prog="run_all.py",
        description="Yen Carry Monitor — data pipeline (Phase 1)",
    )
    parser.add_argument("--mode", choices=MODES, default="daily", help="รอบการทำงาน (default: daily)")
    parser.add_argument("--config", default=None, help="พาธ config.yaml (default: pipeline/config.yaml)")
    parser.add_argument("--log-level", default=None, help="override log level เช่น DEBUG")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg, args.log_level)

    try:
        latest = run(args.mode, cfg)
    except Exception:
        log.exception("pipeline failed")
        return 1

    # ข้อมูลขาดบางส่วน → exit 2 เพื่อให้ workflow ตั้ง continue-on-error ได้ (spec §5 Phase 5)
    return 2 if latest["data_quality"]["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
