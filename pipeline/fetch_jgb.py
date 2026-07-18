"""JGB 10Y yield — MOF CSV เป็น primary, JGB=F futures เป็น fallback (spec §2.3).

Fallback chain:
  1. MOF CSV (ทางการ, daily)  → source = ``"MOF"``
  2. JGB=F futures proxy       → source = ``"JGB=F_proxy"`` (ทิศทางเท่านั้น ไม่ใช่ระดับจริง)
  3. parquet cache ของรอบก่อน  → source = ``"cache"``

ข้อควรรู้เรื่องไฟล์ MOF:
  * เข้ารหัส Shift-JIS (cp932) ไม่ใช่ UTF-8
  * วันที่เป็นศักราชญี่ปุ่น เช่น ``R8.7.1`` = เรวะ 8 ปี = 2026-07-01
  * ค่าที่ไม่มีใช้ ``-``
  * ไฟล์ประวัติ (jgbcm_all.csv) อัพเดทช้ากว่าไฟล์เดือนปัจจุบัน → ต้องรวมกัน
"""

from __future__ import annotations

import io
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests

from pipeline.config import get_logger, load_config, resolve_path

log = get_logger(__name__)

#: ชื่อคอลัมน์ผลลัพธ์มาตรฐานของโมดูลนี้
OUTPUT_COLUMNS = ["date", "yield", "source"]


# ---------------------------------------------------------------- date parsing


def parse_japanese_era_date(token: str, era_base: Dict[str, int]) -> Optional[pd.Timestamp]:
    """แปลงวันที่ศักราชญี่ปุ่น (``R8.7.1``) เป็น :class:`~pandas.Timestamp`.

    Args:
        token: string วันที่จาก MOF เช่น ``"R8.7.1"``, ``"H31.4.30"``
        era_base: mapping ตัวอักษรศักราช → ปีฐาน (เช่น ``{"R": 2018}``)

    Returns:
        Timestamp หรือ ``None`` ถ้า parse ไม่ได้

    Examples:
        >>> parse_japanese_era_date("R8.7.1", {"R": 2018})
        Timestamp('2026-07-01 00:00:00')
    """
    token = str(token).strip()
    if len(token) < 2:
        return None

    era = token[0].upper()
    base = era_base.get(era)
    if base is None:
        return None

    try:
        year_str, month_str, day_str = token[1:].split(".")
        return pd.Timestamp(year=base + int(year_str), month=int(month_str), day=int(day_str))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- MOF


def _http_get(url: str, timeout: float, retries: int, backoff_base: float, backoff_max: float) -> Optional[bytes]:
    """HTTP GET พร้อม retry + exponential backoff.

    Args:
        url: ปลายทาง
        timeout: timeout ต่อครั้ง (วินาที)
        retries: จำนวนครั้งที่พยายาม
        backoff_base: ฐาน backoff
        backoff_max: เพดาน backoff

    Returns:
        response body เป็น bytes หรือ ``None`` ถ้าล้มเหลวทุกครั้ง
    """
    for attempt in range(1, max(1, int(retries)) + 1):
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "yen-carry-monitor/1.0"})
            resp.raise_for_status()
            # MOF คืนหน้า HTML 404 พร้อม status 200 ในบางกรณี → ตรวจเนื้อหาด้วย
            if resp.content.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
                log.warning("%s returned HTML, not CSV", url)
                return None
            return resp.content
        except requests.RequestException as exc:
            log.warning("GET %s failed (attempt %d/%d): %s", url, attempt, retries, exc)
            if attempt < retries:
                delay = min(backoff_base * (2 ** (attempt - 1)), backoff_max)
                time.sleep(delay)
    return None


def parse_mof_csv(content: bytes, cfg_jgb: Dict[str, Any]) -> pd.DataFrame:
    """Parse ไฟล์ CSV ของ MOF เป็น DataFrame ``(date, yield)``.

    Args:
        content: bytes ของไฟล์ CSV (Shift-JIS)
        cfg_jgb: section ``jgb`` จาก config

    Returns:
        DataFrame คอลัมน์ ``date``, ``yield`` เรียงตามวัน (อาจว่างถ้า parse ไม่ได้)
    """
    encoding = cfg_jgb.get("mof_encoding", "cp932")
    col_10y = cfg_jgb.get("mof_column", "10年")
    missing = cfg_jgb.get("mof_missing_token", "-")
    era_base = cfg_jgb.get("era_base", {"S": 1925, "H": 1988, "R": 2018})

    try:
        text = content.decode(encoding, errors="replace")
    except LookupError:  # pragma: no cover - encoding ไม่รู้จัก
        log.error("unknown encoding %s", encoding)
        return pd.DataFrame(columns=["date", "yield"])

    # แถวแรกเป็นชื่อเรื่อง, แถวสองเป็น header จริง → skiprows=1
    try:
        df = pd.read_csv(io.StringIO(text), skiprows=1, dtype=str)
    except Exception as exc:
        log.error("MOF CSV parse failed: %s", exc)
        return pd.DataFrame(columns=["date", "yield"])

    if col_10y not in df.columns:
        log.error("MOF CSV missing column %r; columns=%s", col_10y, list(df.columns)[:8])
        return pd.DataFrame(columns=["date", "yield"])

    date_col = df.columns[0]  # 基準日
    out = pd.DataFrame(
        {
            "date": df[date_col].map(lambda t: parse_japanese_era_date(t, era_base)),
            "yield": pd.to_numeric(df[col_10y].replace(missing, pd.NA), errors="coerce"),
        }
    )
    out = out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    return out


def fetch_mof(cfg: Dict[str, Any]) -> pd.DataFrame:
    """ดึง JGB 10Y จาก MOF โดยรวมไฟล์ประวัติ + ไฟล์เดือนปัจจุบัน.

    Args:
        cfg: config dict ทั้งไฟล์

    Returns:
        DataFrame ``(date, yield)``; ว่างถ้าดึงไม่สำเร็จ
    """
    cfg_jgb = cfg["jgb"]
    fetch_cfg = cfg["fetch"]
    timeout = cfg_jgb.get("timeout_sec", 30)

    frames = []
    for url_key in ("mof_history_url", "mof_current_url"):
        url = cfg_jgb.get(url_key)
        if not url:
            continue
        content = _http_get(
            url,
            timeout=timeout,
            retries=fetch_cfg["retries"],
            backoff_base=fetch_cfg["backoff_base_sec"],
            backoff_max=fetch_cfg["backoff_max_sec"],
        )
        if content is None:
            log.warning("MOF %s unavailable", url_key)
            continue
        part = parse_mof_csv(content, cfg_jgb)
        if not part.empty:
            log.info("MOF %s: %d rows (%s → %s)", url_key, len(part), part["date"].iloc[0].date(), part["date"].iloc[-1].date())
            frames.append(part)

    if not frames:
        return pd.DataFrame(columns=["date", "yield"])

    combined = pd.concat(frames, ignore_index=True)
    # ไฟล์เดือนปัจจุบันมาทีหลัง → keep="last" ให้ค่าที่สดกว่าชนะเมื่อวันซ้ำ
    combined = combined.drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(drop=True)
    return combined


# ---------------------------------------------------------------- fallback


def _load_anchor(cfg: Dict[str, Any]) -> Optional[tuple]:
    """หา anchor (วันที่, yield) จาก MOF cache ล่าสุดที่เคยดึงสำเร็จ.

    Args:
        cfg: config dict ทั้งไฟล์

    Returns:
        tuple ``(Timestamp, yield_pct)`` หรือ ``None`` ถ้าไม่มี cache ที่ใช้ได้
    """
    path = resolve_path(cfg, "jgb_parquet")
    if not path.is_file():
        return None
    try:
        cached = pd.read_parquet(path)
        real = cached[cached.get("source", "MOF") == "MOF"].dropna(subset=["yield"])
        if real.empty:
            return None
        last = real.iloc[-1]
        return pd.Timestamp(last["date"]), float(last["yield"])
    except Exception as exc:
        log.warning("could not read JGB anchor from cache: %s", exc)
        return None


def fetch_futures_proxy(cfg: Dict[str, Any]) -> pd.DataFrame:
    """Fallback B — ประมาณ yield จากราคา JGB ETF ด้วย modified duration (spec §2.3).

    ราคาตราสารหนี้เคลื่อนสวนทางกับ yield ตามความสัมพันธ์::

        Δyield ≈ − (ΔP / P) / duration

    จึงยึดกับค่า MOF ล่าสุดที่รู้ (anchor) แล้วประมาณต่อจากนั้น::

        yield_t ≈ anchor_yield − (P_t / P_anchor − 1) × 100 / duration

    **ใช้บอกทิศทางเท่านั้น** ยิ่งห่าง anchar ยิ่งคลาดเคลื่อน → flag ใน data_quality

    Args:
        cfg: config dict ทั้งไฟล์

    Returns:
        DataFrame ``(date, yield)``; ว่างถ้าดึงไม่สำเร็จทุก symbol
    """
    from pipeline.fetch_market import download_batch

    cfg_jgb = cfg["jgb"]
    fetch_cfg = cfg["fetch"]
    proxy = cfg_jgb["proxy"]
    duration = float(proxy["duration_years"])

    symbols = [cfg_jgb["fallback_ticker"], *cfg_jgb.get("fallback_alternatives", [])]
    start = (datetime.now(timezone.utc) - timedelta(days=365 * int(cfg["parameters"]["history_years"]) + 30)).strftime("%Y-%m-%d")

    px = None
    for symbol in symbols:
        data = download_batch(
            [symbol],
            start=start,
            interval="1d",
            retries=fetch_cfg["retries"],
            backoff_base=fetch_cfg["backoff_base_sec"],
            backoff_max=fetch_cfg["backoff_max_sec"],
        )
        df = data.get(symbol)
        if df is not None and not df.empty:
            log.info("JGB proxy using %s (%d rows)", symbol, len(df))
            px = df["Close"].astype("float64").dropna()
            break
        log.warning("JGB proxy %s returned no data", symbol)

    if px is None or px.empty:
        log.error("all JGB proxy symbols failed: %s", symbols)
        return pd.DataFrame(columns=["date", "yield"])

    px.index = pd.to_datetime(px.index).tz_localize(None)

    anchor = _load_anchor(cfg)
    if anchor is not None:
        anchor_date, anchor_yield = anchor
        prior = px.loc[px.index <= anchor_date]
        anchor_price = float(prior.iloc[-1]) if len(prior) else float(px.iloc[0])
        log.info("JGB proxy anchored at %s yield=%.3f%%", anchor_date.date(), anchor_yield)
    else:
        anchor_yield = float(proxy["fallback_anchor_yield_pct"])
        anchor_price = float(px.iloc[-1])
        log.warning("no MOF anchor available — using configured anchor %.2f%%", anchor_yield)

    implied = anchor_yield - (px / anchor_price - 1.0) * 100.0 / duration
    out = pd.DataFrame({"date": px.index, "yield": implied.values}).dropna().reset_index(drop=True)
    out.attrs["symbol"] = symbol
    return out


# ---------------------------------------------------------------- public API


def fetch_jgb10y(cfg: Optional[Dict[str, Any]] = None, *, use_cache: bool = True) -> Tuple[pd.DataFrame, str]:
    """ดึง JGB 10Y yield ตาม fallback chain ของ spec §2.3.

    Args:
        cfg: config dict; ``None`` = โหลดเอง
        use_cache: อนุญาตให้ fallback ไปใช้ parquet cache เป็นทางเลือกสุดท้าย

    Returns:
        tuple ``(df, source)`` โดย ``df`` มีคอลัมน์ ``date``, ``yield``, ``source``
        และ ``source`` เป็นหนึ่งใน ``"MOF"`` / ``"JGB=F_proxy"`` / ``"cache"`` / ``"none"``
    """
    cfg = cfg if cfg is not None else load_config()
    cache_path = resolve_path(cfg, "jgb_parquet", mkdir=True)

    # 1) MOF (primary)
    df = fetch_mof(cfg)
    source = "MOF"

    # 2) futures proxy (fallback B)
    if df.empty:
        log.warning("MOF unavailable → falling back to bond-price proxy")
        df = fetch_futures_proxy(cfg)
        source = f"{df.attrs.get('symbol', 'bond')}_proxy" if not df.empty else "none"

    # 3) cache (last resort)
    if df.empty and use_cache and cache_path.is_file():
        try:
            cached = pd.read_parquet(cache_path)
            if not cached.empty:
                log.warning("using cached JGB data from %s", cached["date"].iloc[-1].date())
                cached["source"] = "cache"
                return cached[OUTPUT_COLUMNS], "cache"
        except Exception as exc:
            log.warning("JGB cache read failed: %s", exc)

    if df.empty:
        log.error("JGB 10Y unavailable from all sources")
        return pd.DataFrame(columns=OUTPUT_COLUMNS), "none"

    # ตัดให้เหลือเท่าที่ต้องใช้ แล้ว cache ไว้
    years = int(cfg["parameters"]["history_years"])
    cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=365 * years + 30)
    df = df[df["date"] >= cutoff].dropna(subset=["yield"]).reset_index(drop=True)
    df["source"] = source

    try:
        df.to_parquet(cache_path)
    except Exception as exc:  # pragma: no cover
        log.warning("JGB cache write failed: %s", exc)

    log.info("JGB 10Y: %d rows from %s, last=%.3f%% on %s", len(df), source, df["yield"].iloc[-1], df["date"].iloc[-1].date())
    return df[OUTPUT_COLUMNS], source


def jgb_series(df: pd.DataFrame) -> pd.Series:
    """แปลงผลลัพธ์ของ :func:`fetch_jgb10y` เป็น Series ที่ index เป็นวันที่.

    Args:
        df: DataFrame ``(date, yield, source)``

    Returns:
        Series ของ yield (%) index = date
    """
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    return pd.Series(df["yield"].values, index=pd.DatetimeIndex(df["date"]), name="jgb10y").astype("float64")


if __name__ == "__main__":  # pragma: no cover
    from pipeline.config import setup_logging

    _cfg = load_config()
    setup_logging(_cfg)
    _df, _src = fetch_jgb10y(_cfg)
    log.info("source=%s rows=%d", _src, len(_df))
    log.info("\n%s", _df.tail())
