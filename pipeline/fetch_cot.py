"""CFTC Commitments of Traders — JPY net speculative positions (spec §2.2 #11).

แหล่ง: Socrata API ของ publicreporting.cftc.gov — Legacy report, futures-only

    net_speculative = noncomm_positions_long_all − noncomm_positions_short_all

net ติดลบลึก = ตลาด short JPY หนาแน่น = เชื้อเพลิงของ unwind เยอะ

LOOKAHEAD WARNING
-----------------
``report_date`` คือวัน**อังคาร**ที่วัดสถานะ แต่ CFTC ประกาศบ่ายวัน**ศุกร์**
ข้อมูลจึงยัง "ไม่มีอยู่จริง" ในตลาดจนถึงวันศุกร์ โมดูลนี้จึงใส่คอลัมน์
``available_from`` (= report_date + release_lag_days) มาให้ด้วย —
Phase 2 ต้อง forward-fill โดยอิง ``available_from`` ไม่ใช่ ``report_date``
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from pipeline.config import get_logger, load_config, resolve_path

log = get_logger(__name__)

#: คอลัมน์ผลลัพธ์มาตรฐานของโมดูลนี้
OUTPUT_COLUMNS = [
    "report_date",
    "available_from",
    "contract",
    "noncomm_long",
    "noncomm_short",
    "net_speculative",
    "open_interest",
]


def _query_socrata(
    url: str,
    params: Dict[str, Any],
    *,
    timeout: float,
    retries: int,
    backoff_base: float,
    backoff_max: float,
) -> Optional[List[Dict[str, Any]]]:
    """เรียก Socrata API พร้อม retry + exponential backoff.

    Args:
        url: endpoint เต็มของ dataset
        params: query params (SoQL)
        timeout: timeout ต่อครั้ง (วินาที)
        retries: จำนวนครั้งที่พยายาม
        backoff_base: ฐาน backoff
        backoff_max: เพดาน backoff

    Returns:
        list ของ record (dict) หรือ ``None`` ถ้าล้มเหลวทุกครั้ง
    """
    for attempt in range(1, max(1, int(retries)) + 1):
        try:
            resp = requests.get(
                url,
                params=params,
                timeout=timeout,
                headers={"User-Agent": "yen-carry-monitor/1.0", "Accept": "application/json"},
            )
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, list):
                return payload
            log.warning("unexpected Socrata payload type: %s", type(payload).__name__)
        except (requests.RequestException, ValueError) as exc:
            log.warning("CFTC query failed (attempt %d/%d): %s", attempt, retries, exc)

        if attempt < retries:
            delay = min(backoff_base * (2 ** (attempt - 1)), backoff_max)
            log.info("retrying CFTC in %.1fs", delay)
            time.sleep(delay)
    return None


def fetch_cot_jpy(cfg: Optional[Dict[str, Any]] = None, *, use_cache: bool = True) -> pd.DataFrame:
    """ดึง COT ของ JAPANESE YEN ย้อนหลังตาม ``parameters.history_years``.

    Args:
        cfg: config dict; ``None`` = โหลดเอง
        use_cache: ถ้า API ล้มเหลว ให้ fallback ไปอ่าน parquet cache

    Returns:
        DataFrame คอลัมน์ตาม :data:`OUTPUT_COLUMNS` เรียงตามวันเก่า→ใหม่
        (ว่างถ้าไม่มีทั้ง API และ cache)
    """
    cfg = cfg if cfg is not None else load_config()
    cot_cfg = cfg["cot"]
    fetch_cfg = cfg["fetch"]
    fields = cot_cfg["fields"]
    cache_path = resolve_path(cfg, "cot_parquet", mkdir=True)

    years = int(cfg["parameters"]["history_years"])
    since = (datetime.now(timezone.utc) - timedelta(days=365 * years + 30)).strftime("%Y-%m-%dT00:00:00.000")

    url = f"https://{cot_cfg['socrata_domain']}/resource/{cot_cfg['dataset_id']}.json"
    where = (
        f"{fields['contract']} like '%{cot_cfg['contract_like']}%' "
        f"AND {fields['report_date']} >= '{since}'"
    )
    params = {
        "$where": where,
        "$order": f"{fields['report_date']} DESC",
        "$limit": cot_cfg.get("page_limit", 5000),
        "$select": ",".join(
            [fields["report_date"], fields["contract"], fields["long"], fields["short"], "open_interest_all"]
        ),
    }

    records = _query_socrata(
        url,
        params,
        timeout=cot_cfg.get("timeout_sec", 30),
        retries=fetch_cfg["retries"],
        backoff_base=fetch_cfg["backoff_base_sec"],
        backoff_max=fetch_cfg["backoff_max_sec"],
    )

    if not records:
        log.error("CFTC returned no records")
        if use_cache and cache_path.is_file():
            try:
                cached = pd.read_parquet(cache_path)
                log.warning("using cached COT (%d rows)", len(cached))
                return cached
            except Exception as exc:
                log.warning("COT cache read failed: %s", exc)
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    df = _normalise_records(records, cot_cfg, fields)
    if df.empty:
        return df

    try:
        df.to_parquet(cache_path)
    except Exception as exc:  # pragma: no cover
        log.warning("COT cache write failed: %s", exc)

    last = df.iloc[-1]
    log.info(
        "COT JPY: %d weeks, last report %s net=%s (long %s / short %s)",
        len(df),
        last["report_date"].date(),
        f"{int(last['net_speculative']):+,}",
        f"{int(last['noncomm_long']):,}",
        f"{int(last['noncomm_short']):,}",
    )
    return df


def _normalise_records(
    records: List[Dict[str, Any]],
    cot_cfg: Dict[str, Any],
    fields: Dict[str, str],
) -> pd.DataFrame:
    """แปลง record ดิบจาก Socrata เป็น DataFrame ที่สะอาดแล้ว.

    ถ้ามีหลายสัญญาที่ตรงเงื่อนไข (เช่น CME กับ IMM) จะเลือกตามลำดับใน
    ``contract_preference`` เพื่อไม่ให้เกิดแถวซ้ำต่อสัปดาห์

    Args:
        records: list ของ dict จาก API
        cot_cfg: section ``cot`` จาก config
        fields: mapping ชื่อ field

    Returns:
        DataFrame ตาม :data:`OUTPUT_COLUMNS`
    """
    raw = pd.DataFrame.from_records(records)
    if raw.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    df = pd.DataFrame(
        {
            "report_date": pd.to_datetime(raw[fields["report_date"]], errors="coerce"),
            "contract": raw[fields["contract"]].astype(str),
            "noncomm_long": pd.to_numeric(raw[fields["long"]], errors="coerce"),
            "noncomm_short": pd.to_numeric(raw[fields["short"]], errors="coerce"),
            "open_interest": pd.to_numeric(raw.get("open_interest_all"), errors="coerce"),
        }
    ).dropna(subset=["report_date", "noncomm_long", "noncomm_short"])

    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    # เลือกสัญญาเดียวต่อสัปดาห์ตามลำดับความชอบ
    preference = list(cot_cfg.get("contract_preference", []))
    rank = {name: i for i, name in enumerate(preference)}
    df["_rank"] = df["contract"].map(lambda c: rank.get(c, len(preference)))
    df = df.sort_values(["report_date", "_rank"]).drop_duplicates(subset="report_date", keep="first")

    df["net_speculative"] = df["noncomm_long"] - df["noncomm_short"]
    lag = int(cot_cfg.get("release_lag_days", 3))
    df["available_from"] = df["report_date"] + pd.Timedelta(days=lag)

    df = df.drop(columns="_rank").sort_values("report_date").reset_index(drop=True)
    return df[OUTPUT_COLUMNS]


def cot_series(df: pd.DataFrame, *, use_available_from: bool = True) -> pd.Series:
    """แปลง COT DataFrame เป็น Series ของ net speculative positions.

    Args:
        df: ผลจาก :func:`fetch_cot_jpy`
        use_available_from: ``True`` = ใช้ ``available_from`` เป็น index (ปลอดภัยจาก lookahead
            สำหรับการคำนวณ time series), ``False`` = ใช้ ``report_date`` (สำหรับการแสดงผล)

    Returns:
        Series ของ net position
    """
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    idx_col = "available_from" if use_available_from else "report_date"
    return pd.Series(
        df["net_speculative"].values,
        index=pd.DatetimeIndex(df[idx_col]),
        name="cot_jpy_net",
    ).astype("float64")


if __name__ == "__main__":  # pragma: no cover
    from pipeline.config import setup_logging

    _cfg = load_config()
    setup_logging(_cfg)
    _df = fetch_cot_jpy(_cfg)
    log.info("\n%s", _df.tail())
