"""Fetch OHLCV จาก yfinance — daily 3 ปี + intraday 1h ล่าสุด.

ยุทธศาสตร์ (spec §4.7):
  * batch download ลด API call
  * retry 3 ครั้ง แบบ exponential backoff
  * ticker ไหนพัง → log warning แล้วไปต่อ (ไม่ล้มทั้ง pipeline)
  * cache parquet ใน data/raw/ → ถ้า fetch พังทั้งหมดยังใช้ค่าเก่าได้ + ธง stale
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from pipeline.config import get_logger, load_config, resolve_path

log = get_logger(__name__)

_OHLCV = ["Open", "High", "Low", "Close", "Volume"]


@dataclass
class FetchResult:
    """ผลลัพธ์การดึงข้อมูลตลาดหนึ่งรอบ.

    Attributes:
        daily: mapping ``key -> DataFrame`` ของ OHLCV รายวัน
        intraday: mapping ``key -> DataFrame`` ของ OHLCV 1 ชั่วโมง
        failed: รายชื่อ key ที่ดึงไม่สำเร็จเลย (ไม่มีแม้แต่ cache)
        stale: รายชื่อ key ที่ใช้ข้อมูลจาก cache เพราะ fetch ล้มเหลว
    """

    daily: Dict[str, pd.DataFrame] = field(default_factory=dict)
    intraday: Dict[str, pd.DataFrame] = field(default_factory=dict)
    failed: List[str] = field(default_factory=list)
    stale: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- utils


def _sleep_backoff(attempt: int, base: float, cap: float) -> None:
    """หน่วงเวลาแบบ exponential backoff ก่อน retry รอบถัดไป.

    Args:
        attempt: ลำดับความพยายามที่เพิ่งล้มเหลว (เริ่มที่ 1)
        base: ฐานของ backoff เป็นวินาที
        cap: เพดานเวลาหน่วงสูงสุด
    """
    delay = min(base * (2 ** (attempt - 1)), cap)
    log.info("retrying in %.1fs (attempt %d)", delay, attempt + 1)
    time.sleep(delay)


def _chunks(items: Sequence[str], size: int) -> List[List[str]]:
    """แบ่ง list เป็นก้อนละ ``size``.

    Args:
        items: รายการต้นทาง
        size: ขนาดก้อน

    Returns:
        list ของ list ย่อย
    """
    size = max(1, int(size))
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """ทำความสะอาด DataFrame จาก yfinance ให้เหลือ OHLCV + index เป็น DatetimeIndex.

    Args:
        df: DataFrame ดิบจาก yfinance

    Returns:
        DataFrame ที่เรียง index แล้ว ไม่มีแถวซ้ำ และไม่มีแถวที่ Close เป็น NaN
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=_OHLCV)

    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)

    keep = [c for c in _OHLCV if c in out.columns]
    out = out[keep]
    out.index = pd.to_datetime(out.index, errors="coerce", utc=False)
    out = out[out.index.notna()]
    out = out[~out.index.duplicated(keep="last")].sort_index()

    if "Close" in out.columns:
        out = out[out["Close"].notna()]
    return out


def _split_batch(raw: pd.DataFrame, symbols: Sequence[str]) -> Dict[str, pd.DataFrame]:
    """แยกผลลัพธ์ batch download ของ yfinance ออกเป็นราย ticker.

    Args:
        raw: DataFrame จาก ``yf.download`` (MultiIndex columns เมื่อหลาย ticker)
        symbols: รายชื่อ symbol ที่ขอไป

    Returns:
        mapping ``symbol -> DataFrame`` (เฉพาะตัวที่มีข้อมูล)
    """
    out: Dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out

    if isinstance(raw.columns, pd.MultiIndex):
        # yfinance group_by='ticker' → level 0 = symbol
        level0 = set(raw.columns.get_level_values(0))
        for sym in symbols:
            if sym in level0:
                sub = _normalise(raw[sym])
                if not sub.empty:
                    out[sym] = sub
    elif len(symbols) == 1:
        sub = _normalise(raw)
        if not sub.empty:
            out[symbols[0]] = sub
    return out


# ---------------------------------------------------------------- cache


def _cache_path(raw_dir: Path, key: str, kind: str) -> Path:
    """สร้างพาธไฟล์ parquet ของ cache.

    Args:
        raw_dir: โฟลเดอร์ data/raw
        key: ชื่อภายในของ ticker (เช่น ``usdjpy``)
        kind: ``"daily"`` หรือ ``"intraday"``

    Returns:
        พาธไฟล์ parquet
    """
    return raw_dir / f"{key}_{kind}.parquet"


def write_cache(df: pd.DataFrame, path: Path) -> None:
    """เขียน DataFrame ลง parquet (ล้มเหลวได้โดยไม่ทำให้ pipeline พัง).

    Args:
        df: ข้อมูลที่จะ cache
        path: ปลายทาง
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)
        log.debug("cached %d rows -> %s", len(df), path.name)
    except Exception as exc:  # pragma: no cover - ขึ้นกับ engine ของเครื่อง
        log.warning("cache write failed for %s: %s", path.name, exc)


def read_cache(path: Path) -> Optional[pd.DataFrame]:
    """อ่าน cache parquet ถ้ามี.

    Args:
        path: พาธไฟล์ cache

    Returns:
        DataFrame หรือ ``None`` ถ้าไม่มี/อ่านไม่ได้
    """
    if not path.is_file():
        return None
    try:
        return _normalise(pd.read_parquet(path))
    except Exception as exc:
        log.warning("cache read failed for %s: %s", path.name, exc)
        return None


def is_stale(df: pd.DataFrame, max_age_hours: float) -> bool:
    """ตรวจว่าข้อมูลเก่าเกินเกณฑ์หรือไม่.

    Args:
        df: DataFrame ที่มี DatetimeIndex
        max_age_hours: อายุสูงสุดที่ยอมรับได้ (ชั่วโมง)

    Returns:
        True ถ้าแถวล่าสุดเก่ากว่าเกณฑ์ หรือ DataFrame ว่าง
    """
    if df is None or df.empty:
        return True
    last = df.index[-1]
    last_ts = last.tz_convert(timezone.utc) if getattr(last, "tzinfo", None) else last.tz_localize(timezone.utc)
    return (datetime.now(timezone.utc) - last_ts) > timedelta(hours=float(max_age_hours))


# ---------------------------------------------------------------- download


def download_batch(
    symbols: Sequence[str],
    *,
    period: Optional[str] = None,
    start: Optional[str] = None,
    interval: str = "1d",
    retries: int = 3,
    backoff_base: float = 2.0,
    backoff_max: float = 30.0,
) -> Dict[str, pd.DataFrame]:
    """ดาวน์โหลดหลาย ticker พร้อมกันด้วย retry + exponential backoff.

    Args:
        symbols: รายชื่อ yfinance symbol
        period: เช่น ``"5d"`` (ใช้กับ intraday); ใช้คู่กับ ``start`` ไม่ได้
        start: วันเริ่ม ``YYYY-MM-DD`` (ใช้กับ daily)
        interval: ``"1d"`` หรือ ``"1h"``
        retries: จำนวนครั้งที่พยายาม
        backoff_base: ฐาน backoff (วินาที)
        backoff_max: เพดาน backoff (วินาที)

    Returns:
        mapping ``symbol -> DataFrame``; symbol ที่ไม่มีข้อมูลจะไม่ปรากฏใน dict
    """
    import yfinance as yf  # import ในฟังก์ชันเพื่อให้ tests ของ indicators ไม่ต้องพึ่ง yfinance

    symbols = list(symbols)
    if not symbols:
        return {}

    kwargs: Dict[str, Any] = {
        "interval": interval,
        "auto_adjust": False,
        "actions": False,
        "progress": False,
        "group_by": "ticker",
        "threads": True,
    }
    if period:
        kwargs["period"] = period
    if start:
        kwargs["start"] = start

    last_exc: Optional[Exception] = None
    for attempt in range(1, max(1, int(retries)) + 1):
        try:
            log.info("download %s interval=%s (attempt %d/%d)", symbols, interval, attempt, retries)
            raw = yf.download(symbols, **kwargs)
            got = _split_batch(raw, symbols)
            if got:
                missing = [s for s in symbols if s not in got]
                if missing:
                    log.warning("no data returned for: %s", missing)
                return got
            log.warning("empty response for batch %s", symbols)
        except Exception as exc:
            last_exc = exc
            log.warning("download failed (attempt %d/%d): %s", attempt, retries, exc)

        if attempt < retries:
            _sleep_backoff(attempt, backoff_base, backoff_max)

    if last_exc is not None:
        log.error("batch %s failed after %d attempts: %s", symbols, retries, last_exc)
    return {}


# ---------------------------------------------------------------- orchestration


def fetch_market_data(
    cfg: Optional[Dict[str, Any]] = None,
    *,
    include_intraday: bool = True,
    use_cache_on_failure: bool = True,
) -> FetchResult:
    """ดึง OHLCV ของทุก ticker ใน config (daily 3 ปี + intraday 1h).

    Args:
        cfg: config dict; ``None`` = โหลดจาก config.yaml
        include_intraday: ดึง intraday 1h ด้วยหรือไม่ (โหมด daily/weekly ปิดได้)
        use_cache_on_failure: ถ้า fetch พัง ให้ fallback ไปใช้ parquet cache

    Returns:
        :class:`FetchResult`
    """
    cfg = cfg if cfg is not None else load_config()
    tickers: Dict[str, Any] = cfg["tickers"]
    params = cfg["parameters"]
    fetch_cfg = cfg["fetch"]
    raw_dir = resolve_path(cfg, "raw_dir", mkdir=True)

    sym_to_key = {meta["ticker"]: key for key, meta in tickers.items()}
    result = FetchResult()

    # ---- daily -----------------------------------------------------
    start = (datetime.now(timezone.utc) - timedelta(days=365 * int(params["history_years"]) + 30)).strftime("%Y-%m-%d")
    daily_syms = [meta["ticker"] for meta in tickers.values()]

    fetched: Dict[str, pd.DataFrame] = {}
    for batch in _chunks(daily_syms, fetch_cfg["batch_size"]):
        fetched.update(
            download_batch(
                batch,
                start=start,
                interval="1d",
                retries=fetch_cfg["retries"],
                backoff_base=fetch_cfg["backoff_base_sec"],
                backoff_max=fetch_cfg["backoff_max_sec"],
            )
        )

    for key, meta in tickers.items():
        sym = meta["ticker"]
        cache_file = _cache_path(raw_dir, key, "daily")
        df = fetched.get(sym)
        if df is not None and not df.empty:
            write_cache(df, cache_file)
            result.daily[key] = df
            continue

        cached = read_cache(cache_file) if use_cache_on_failure else None
        if cached is not None and not cached.empty:
            log.warning("%s (%s): using cached data from %s", key, sym, cached.index[-1].date())
            result.daily[key] = cached
            result.stale.append(key)
        else:
            log.error("%s (%s): no data and no cache", key, sym)
            result.failed.append(key)

    # ---- intraday --------------------------------------------------
    if include_intraday:
        intraday_keys = [k for k, m in tickers.items() if m.get("intraday")]
        intraday_syms = [tickers[k]["ticker"] for k in intraday_keys]

        fetched_i: Dict[str, pd.DataFrame] = {}
        for batch in _chunks(intraday_syms, fetch_cfg["batch_size"]):
            fetched_i.update(
                download_batch(
                    batch,
                    period=params["intraday_period"],
                    interval=params["intraday_interval"],
                    retries=fetch_cfg["retries"],
                    backoff_base=fetch_cfg["backoff_base_sec"],
                    backoff_max=fetch_cfg["backoff_max_sec"],
                )
            )

        for key in intraday_keys:
            sym = tickers[key]["ticker"]
            cache_file = _cache_path(raw_dir, key, "intraday")
            df = fetched_i.get(sym)
            if df is not None and not df.empty:
                write_cache(df, cache_file)
                result.intraday[key] = df
            else:
                cached = read_cache(cache_file) if use_cache_on_failure else None
                if cached is not None and not cached.empty:
                    result.intraday[key] = cached
                    log.warning("%s: intraday from cache", key)
                else:
                    log.warning("%s: no intraday data", key)

    log.info(
        "market fetch done: %d daily, %d intraday, %d stale, %d failed",
        len(result.daily),
        len(result.intraday),
        len(result.stale),
        len(result.failed),
    )
    return result


def close_series(result: FetchResult, key: str) -> pd.Series:
    """ดึง Close series ของ ticker หนึ่งตัวจาก :class:`FetchResult`.

    Args:
        result: ผลจาก :func:`fetch_market_data`
        key: ชื่อภายในของ ticker

    Returns:
        Series ของ Close; Series ว่างถ้าไม่มีข้อมูล
    """
    df = result.daily.get(key)
    if df is None or df.empty or "Close" not in df.columns:
        return pd.Series(dtype="float64")
    return df["Close"].astype("float64")


if __name__ == "__main__":  # pragma: no cover
    from pipeline.config import setup_logging

    _cfg = load_config()
    setup_logging(_cfg)
    _res = fetch_market_data(_cfg)
    for _k, _df in _res.daily.items():
        log.info("%-8s %5d rows  last=%s  close=%.4f", _k, len(_df), _df.index[-1].date(), _df["Close"].iloc[-1])
