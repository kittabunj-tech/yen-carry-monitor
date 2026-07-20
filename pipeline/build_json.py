"""ประกอบ data/latest.json + indicators_history.json ตาม schema ใน spec §4.4.

Phase 1: ช่อง ``cusi`` เป็น ``null`` (Phase 2 จะมาเติม)
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from pipeline import indicators as ind
from pipeline.config import get_logger, load_config, resolve_path
from pipeline.cusi import build_cusi_block, build_cusi_frame, build_cusi_history, load_weights
from pipeline.fetch_cot import cot_series
from pipeline.fetch_events import build_events_block, fetch_events
from pipeline.fetch_implied_vol import fetch_implied_vol, read_history_series, update_history
from pipeline.cusi_lite import compute_cusi_lite, load_cached_intraday
from pipeline.fetch_jgb import jgb_series
from pipeline.fetch_market import FetchResult, close_series

log = get_logger(__name__)


# ---------------------------------------------------------------- helpers


def _round(value: Any, digits: int = 2) -> Optional[float]:
    """ปัดเศษอย่างปลอดภัย: NaN / inf / None → ``None`` (เป็น ``null`` ใน JSON).

    Args:
        value: ค่าที่จะปัด
        digits: จำนวนตำแหน่งทศนิยม

    Returns:
        float ที่ปัดแล้ว หรือ ``None``
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, digits)


def _last(series: pd.Series) -> Optional[float]:
    """ค่าสุดท้ายที่ไม่ใช่ NaN ของ series.

    Args:
        series: input series

    Returns:
        ค่าล่าสุด หรือ ``None`` ถ้าไม่มีค่าที่ใช้ได้
    """
    if series is None or len(series) == 0:
        return None
    clean = series.dropna()
    return float(clean.iloc[-1]) if len(clean) else None


def _iso(ts: Any) -> Optional[str]:
    """แปลง timestamp เป็น ISO-8601 string.

    Args:
        ts: Timestamp / datetime / None

    Returns:
        ISO string หรือ ``None``
    """
    if ts is None or (isinstance(ts, float) and math.isnan(ts)):
        return None
    try:
        return pd.Timestamp(ts).isoformat()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- indicator blocks


def build_indicator_frame(
    market: FetchResult,
    jgb_df: pd.DataFrame,
    cfg: Dict[str, Any],
) -> pd.DataFrame:
    """สร้าง DataFrame รายวันของตัวชี้วัดที่คำนวณแล้วทั้งหมด (ใช้ร่วมกับ Phase 2).

    ทุกคอลัมน์คำนวณจากฟังก์ชันใน :mod:`pipeline.indicators` จึงปลอดภัยจาก lookahead

    Args:
        market: ผลจาก :func:`~pipeline.fetch_market.fetch_market_data`
        jgb_df: ผลจาก :func:`~pipeline.fetch_jgb.fetch_jgb10y`
        cfg: config dict

    Returns:
        DataFrame index = วันที่ (business days), คอลัมน์ = ตัวชี้วัด
    """
    p = cfg["parameters"]
    z_win, z_clip = int(p["z_window"]), float(p["z_clip"])
    frames: Dict[str, pd.Series] = {}

    # ---- ราคาดิบของแต่ละ ticker
    closes = {key: close_series(market, key) for key in cfg["tickers"]}
    for key, px in closes.items():
        if len(px):
            frames[key] = px

    # ---- momentum: คำนวณให้ทุก ticker ที่เป็นราคา (yield ใช้ Δbps แทน %)
    price_keys = [k for k, m in cfg["tickers"].items() if m.get("kind") != "yield"]
    for key in price_keys:
        px = closes.get(key, pd.Series(dtype="float64"))
        if not len(px):
            continue
        frames[f"{key}_chg_1d_pct"] = ind.pct_change_n(px, 1)
        frames[f"{key}_chg_5d_pct"] = ind.pct_change_n(px, int(p["momentum_window"]))

    # ---- drawdown + z ของ 5d return: เฉพาะตัวที่ CUSI ใช้ (§3.2) เพื่อคุมขนาด frame
    for key in ("usdjpy", "audjpy", "mxnjpy", "nikkei", "ndx", "spx"):
        px = closes.get(key, pd.Series(dtype="float64"))
        if not len(px):
            continue
        frames[f"{key}_dd_pct"] = ind.drawdown_from_high(px, int(p["drawdown_window"]))
        frames[f"{key}_z_5d"] = ind.rolling_z(frames[f"{key}_chg_5d_pct"], z_win, z_clip)

    # ---- FX volatility (spec §2.4)
    usdjpy = closes.get("usdjpy", pd.Series(dtype="float64"))
    if len(usdjpy):
        rv = ind.realized_vol(usdjpy, int(p["vol_window"]), trading_days=int(p["trading_days_per_year"]))
        frames["fx_realized_vol_10d"] = rv
        frames["fx_vol_of_vol"] = ind.vol_of_vol(rv, int(p["vol_of_vol_window"]))
        frames["fx_vol_pctile"] = ind.rolling_percentile(rv, int(p["percentile_window"]))
        frames["fx_vol_z"] = ind.rolling_z(rv, z_win, z_clip)

        usdjpy_ohlc = market.daily.get("usdjpy")
        if usdjpy_ohlc is not None and not usdjpy_ohlc.empty:
            frames["fx_atr_pct_14"] = ind.atr_pct(usdjpy_ohlc, int(p["atr_window"]))

    # ---- VIX / MOVE
    vix = closes.get("vix", pd.Series(dtype="float64"))
    if len(vix):
        frames["vix_chg_5d"] = vix - vix.shift(int(p["momentum_window"]))
        frames["vix_pctile"] = ind.rolling_percentile(vix, int(p["percentile_window"]))
        frames["vix_z"] = ind.rolling_z(vix, z_win, z_clip)
        frames["vix_chg_5d_z"] = ind.rolling_z(frames["vix_chg_5d"], z_win, z_clip)
    if len(closes.get("move", pd.Series(dtype="float64"))):
        frames["move_z"] = ind.rolling_z(closes["move"], z_win, z_clip)

    # ---- yields + spread (spec §2.2 #9)
    us10y = closes.get("us10y", pd.Series(dtype="float64"))
    jgb = jgb_series(jgb_df)
    if len(us10y):
        frames["us10y_chg_5d_bps"] = ind.delta_bps(us10y, int(p["momentum_window"]))
    us2y = closes.get("us2y", pd.Series(dtype="float64"))
    if len(us2y):
        frames["us2y_chg_5d_bps"] = ind.delta_bps(us2y, int(p["momentum_window"]))
    if len(jgb):
        frames["jgb10y"] = jgb
        frames["jgb10y_chg_5d_bps"] = ind.delta_bps(jgb, int(p["momentum_window"]))

    if len(us10y) and len(jgb):
        # align บนปฏิทินร่วม แล้ว ffill เฉพาะฝั่ง yield (วันหยุดคนละประเทศ)
        idx = us10y.index.union(jgb.index)
        us_a = us10y.reindex(idx).ffill()
        jp_a = jgb.reindex(idx).ffill()
        sp = ind.spread(us_a, jp_a)
        frames["spread_us_jp"] = sp
        frames["spread_chg_20d_bps"] = ind.delta_bps(sp, int(p["spread_change_window"]))
        frames["spread_z"] = ind.rolling_z(sp, z_win, z_clip)

    if not frames:
        log.error("no indicator series could be built")
        return pd.DataFrame()

    df = pd.DataFrame(frames).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    log.info("indicator frame: %d rows × %d cols (%s → %s)", len(df), len(df.columns), df.index[0].date(), df.index[-1].date())
    return df


def build_indicators_block(
    df: pd.DataFrame,
    jgb_source: str,
    cot_df: pd.DataFrame,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """สร้าง block ``indicators`` ของ latest.json ตาม schema §4.4.

    Args:
        df: ผลจาก :func:`build_indicator_frame`
        jgb_source: แหล่งข้อมูล JGB (``"MOF"`` / ``"JGB=F_proxy"`` / ...)
        cot_df: ผลจาก :func:`~pipeline.fetch_cot.fetch_cot_jpy`
        cfg: config dict

    Returns:
        dict พร้อม serialise เป็น JSON
    """
    p = cfg["parameters"]

    def col(name: str) -> pd.Series:
        """ดึงคอลัมน์จาก df แบบไม่ระเบิดถ้าไม่มี."""
        return df[name] if name in df.columns else pd.Series(dtype="float64")

    out: Dict[str, Any] = {
        "usdjpy": {
            "last": _round(_last(col("usdjpy")), 4),
            "chg_1d_pct": _round(_last(col("usdjpy_chg_1d_pct"))),
            "chg_5d_pct": _round(_last(col("usdjpy_chg_5d_pct"))),
            "z_5d": _round(_last(col("usdjpy_z_5d"))),
        },
        "audjpy": {
            "last": _round(_last(col("audjpy")), 4),
            "chg_1d_pct": _round(_last(col("audjpy_chg_1d_pct"))),
            "dd_from_20d_high_pct": _round(_last(col("audjpy_dd_pct"))),
        },
        "jgb10y": {
            "last": _round(_last(col("jgb10y")), 3),
            "chg_5d_bps": _round(_last(col("jgb10y_chg_5d_bps")), 1),
            "source": jgb_source,
        },
        "us10y": {
            "last": _round(_last(col("us10y")), 3),
            "chg_5d_bps": _round(_last(col("us10y_chg_5d_bps")), 1),
        },
        "spread_us_jp": {
            "last": _round(_last(col("spread_us_jp")), 3),
            "chg_20d_bps": _round(_last(col("spread_chg_20d_bps")), 1),
            "z": _round(_last(col("spread_z"))),
        },
        "vix": {
            "last": _round(_last(col("vix"))),
            "chg_5d": _round(_last(col("vix_chg_5d"))),
            "percentile_3y": _round(_last(col("vix_pctile")), 1),
        },
        "move": {"last": _round(_last(col("move")))},
        "fx_vol": {
            "realized_10d": _round(_last(col("fx_realized_vol_10d"))),
            "atr_pct_14": _round(_last(col("fx_atr_pct_14"))),
            "vol_of_vol": _round(_last(col("fx_vol_of_vol"))),
            "percentile_3y": _round(_last(col("fx_vol_pctile")), 1),
        },
        "nikkei": {
            "last": _round(_last(col("nikkei"))),
            "chg_5d_pct": _round(_last(col("nikkei_chg_5d_pct"))),
        },
        "mxnjpy": {
            "last": _round(_last(col("mxnjpy")), 4),
            "dd_from_20d_high_pct": _round(_last(col("mxnjpy_dd_pct"))),
        },
    }

    # ---- COT (weekly)
    if cot_df is not None and not cot_df.empty:
        net = cot_series(cot_df, use_available_from=True)
        z = ind.rolling_z(net, min(int(p["z_window"]) // 5, max(len(net) - 1, 2)), float(p["z_clip"]))
        last_row = cot_df.iloc[-1]
        out["cot_jpy"] = {
            "net_position": int(last_row["net_speculative"]),
            "noncomm_long": int(last_row["noncomm_long"]),
            "noncomm_short": int(last_row["noncomm_short"]),
            "z": _round(_last(z)),
            "report_date": str(pd.Timestamp(last_row["report_date"]).date()),
            "available_from": str(pd.Timestamp(last_row["available_from"]).date()),
        }
    else:
        out["cot_jpy"] = {"net_position": None, "z": None, "report_date": None}

    # ---- ตัวประกอบเสริม (§2.2) ที่ยังไม่มีใน schema ตัวอย่าง
    for key, digits in (("dxy", 3), ("gold", 2), ("btc", 2), ("ndx", 2), ("spx", 2)):
        val = _last(col(key))
        if val is not None:
            out[key] = {"last": _round(val, digits), "chg_5d_pct": _round(_last(col(f"{key}_chg_5d_pct")))}

    # us2y เป็น yield → รายงานเป็น bps ไม่ใช่ %
    if _last(col("us2y")) is not None:
        out["us2y"] = {
            "last": _round(_last(col("us2y")), 3),
            "chg_5d_bps": _round(_last(col("us2y_chg_5d_bps")), 1),
        }

    return out


# ---------------------------------------------------------------- quality


def build_data_quality(
    market: FetchResult,
    jgb_source: str,
    cot_df: pd.DataFrame,
    df: pd.DataFrame,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """สร้าง block ``data_quality`` — บอกว่าอะไรขาด อะไรเก่า (spec §4.7).

    Args:
        market: ผลการดึงราคา
        jgb_source: แหล่ง JGB ที่ใช้จริง
        cot_df: COT DataFrame
        df: indicator frame
        cfg: config dict

    Returns:
        dict ของสถานะคุณภาพข้อมูล
    """
    stale_hours = float(cfg["fetch"]["stale_after_hours"])
    now = datetime.now(timezone.utc)

    missing: List[str] = list(market.failed)
    stale: List[str] = list(market.stale)

    for key, sub in market.daily.items():
        if sub.empty:
            continue
        last = sub.index[-1]
        last_ts = last.tz_convert(timezone.utc) if getattr(last, "tzinfo", None) else last.tz_localize(timezone.utc)
        # ตลาดปิดสุดสัปดาห์เป็นเรื่องปกติ → เผื่อเวลาให้ 3 วันสำหรับ daily bar
        if (now - last_ts) > timedelta(hours=stale_hours + 48) and key not in stale:
            stale.append(key)

    if jgb_source in ("none", "cache"):
        missing.append("jgb10y")
    if cot_df is None or cot_df.empty:
        missing.append("cot_jpy")

    return {
        "jgb_source": jgb_source,
        "jgb_is_proxy": jgb_source.endswith("_proxy"),
        "missing": sorted(set(missing)),
        "stale": sorted(set(stale)),
        "tickers_ok": len(market.daily),
        "tickers_total": len(cfg["tickers"]),
        "indicator_rows": int(len(df)),
    }


# ---------------------------------------------------------------- writers


def build_latest_json(
    market: FetchResult,
    jgb_df: pd.DataFrame,
    jgb_source: str,
    cot_df: pd.DataFrame,
    df: pd.DataFrame,
    cfg: Dict[str, Any],
    cusi_block: Optional[Dict[str, Any]] = None,
    events_block: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """ประกอบ payload ทั้งหมดของ ``data/latest.json`` (spec §4.4).

    Args:
        market: ผลการดึงราคา
        jgb_df: JGB DataFrame
        jgb_source: แหล่ง JGB
        cot_df: COT DataFrame
        df: indicator frame
        cfg: config dict
        cusi_block: block ``cusi`` จาก :func:`~pipeline.cusi.build_cusi_block`
            (``None`` ถ้าคำนวณไม่ได้ → JSON จะเป็น ``null`` ตาม schema)
        events_block: block ``events`` (Phase 6C — field ใหม่, additive)

    Returns:
        dict ตาม schema §4.4
    """
    tz = cfg["meta"].get("timezone", "Asia/Bangkok")
    try:
        generated_at = datetime.now(timezone.utc).astimezone(pd.Timestamp.now(tz=tz).tzinfo)
    except Exception:  # pragma: no cover - tz ผิดรูป
        generated_at = datetime.now(timezone.utc)

    data_as_of = df.index[-1] if len(df) else None

    return {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "data_as_of": _iso(data_as_of),
        "schema_version": cfg["meta"].get("version", "1.0"),
        "cusi": cusi_block,
        "indicators": build_indicators_block(df, jgb_source, cot_df, cfg),
        "alerts": [],  # Phase 5
        "data_quality": build_data_quality(market, jgb_source, cot_df, df, cfg),
        # Phase 6C — field ใหม่ (backward compatible: ผู้อ่านเก่าไม่รู้จักก็ข้ามไป)
        "events": events_block,
    }


def build_history_json(df: pd.DataFrame, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """สร้าง ``indicators_history.json`` — time series ย้อนหลัง N วัน (spec §4.3).

    เลือกเฉพาะคอลัมน์ที่ frontend ใช้วาดกราฟ เพื่อคุมขนาดไฟล์ (§4.7)

    Args:
        df: indicator frame
        cfg: config dict

    Returns:
        dict ``{"dates": [...], "series": {name: [...]}}``
    """
    days = int(cfg["parameters"]["history_days"])
    if df.empty:
        return {"dates": [], "series": {}}

    wanted = [
        "usdjpy",
        "audjpy",
        "mxnjpy",
        "nikkei",
        "vix",
        "move",
        "us10y",
        "jgb10y",
        "spread_us_jp",
        "spread_chg_20d_bps",
        "fx_realized_vol_10d",
        "fx_atr_pct_14",
        "fx_vol_of_vol",
    ]
    cols = [c for c in wanted if c in df.columns]
    tail = df[cols].tail(days)

    return {
        "window_days": days,
        "dates": [str(d.date()) for d in tail.index],
        "series": {c: [_round(v, 4) for v in tail[c]] for c in cols},
    }


def build_cot_history_json(cot_df: pd.DataFrame) -> Dict[str, Any]:
    """สร้าง ``cot_history.json`` สำหรับ bar chart ของ frontend.

    Args:
        cot_df: ผลจาก :func:`~pipeline.fetch_cot.fetch_cot_jpy`

    Returns:
        dict ``{"reports": [...]}``
    """
    if cot_df is None or cot_df.empty:
        return {"reports": []}

    return {
        "reports": [
            {
                "report_date": str(pd.Timestamp(r["report_date"]).date()),
                "available_from": str(pd.Timestamp(r["available_from"]).date()),
                "net_speculative": int(r["net_speculative"]),
                "noncomm_long": int(r["noncomm_long"]),
                "noncomm_short": int(r["noncomm_short"]),
            }
            for _, r in cot_df.iterrows()
        ]
    }


def write_json(payload: Dict[str, Any], path: Path) -> None:
    """เขียน JSON แบบ atomic (เขียน .tmp แล้ว replace) เพื่อกันไฟล์พังกลางคัน.

    Args:
        payload: dict ที่จะเขียน
        path: ปลายทาง
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, allow_nan=False)
        fh.write("\n")
    tmp.replace(path)
    log.info("wrote %s (%.1f KB)", path.name, path.stat().st_size / 1024)


def build_all(
    market: FetchResult,
    jgb_df: pd.DataFrame,
    jgb_source: str,
    cot_df: pd.DataFrame,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    write_cot: bool = True,
    write_cusi_history: bool = True,
    fetch_network_events: bool = True,
) -> Dict[str, Any]:
    """ประกอบและเขียนไฟล์ JSON ทั้งหมดในรอบเดียว.

    Args:
        market: ผลการดึงราคา
        jgb_df: JGB DataFrame
        jgb_source: แหล่ง JGB
        cot_df: COT DataFrame
        cfg: config dict; ``None`` = โหลดเอง
        write_cot: เขียน cot_history.json ด้วยหรือไม่ (โหมด weekly)
        write_cusi_history: เขียน cusi_history.json ด้วยหรือไม่ (โหมด daily/weekly)
        fetch_network_events: ยิง network ดึงปฏิทิน BOJ/FOMC ใหม่หรือไม่ (โหมด hourly = False)

    Returns:
        payload ของ latest.json
    """
    cfg = cfg if cfg is not None else load_config()

    df = build_indicator_frame(market, jgb_df, cfg)

    # ---- Events (Phase 6C) — additive: field ใหม่ ห้ามกระทบของเดิม
    # ปฏิทินเปลี่ยนปีละครั้ง → ยิง network เฉพาะโหมด daily/weekly (fetch_network_events)
    # โหมด hourly อ่าน cache แต่คำนวณธง risk ใหม่ทุกรอบ (ธงขึ้นกับ "วันนี้")
    events_block: Optional[Dict[str, Any]] = None
    try:
        payload = fetch_events(cfg, use_network=fetch_network_events)
        events_block = build_events_block(payload, cfg)
    except Exception:
        log.exception("event layer ล้มเหลว — latest.json จะไม่มี field events รอบนี้")

    # ---- Implied vol (Phase 6B) — additive เช่นกัน
    implied_metric: Optional[Dict[str, Any]] = None
    try:
        if fetch_network_events:                     # daily/weekly เท่านั้น (options ไม่ต้องถี่กว่านั้น)
            implied_metric = fetch_implied_vol(cfg)
            update_history(implied_metric, cfg)
        # เติมคอลัมน์จาก history ให้ CUSI ใช้ได้เมื่อสลับ source (มีผลก็ต่อเมื่อ
        # fx_vol_source=implied และ history ยาวพอ — ดู compute_components)
        hist = read_history_series(cfg)
        if hist and not df.empty:
            import pandas as _pd
            s = _pd.Series(hist)
            s.index = _pd.to_datetime(s.index)
            df["fx_implied_vol_1m"] = s.reindex(df.index)
    except Exception:
        log.exception("implied vol layer ล้มเหลว — ข้ามรอบนี้")

    # ---- CUSI (Phase 2) — ถ้าพังต้องไม่ทำให้ข้อมูลส่วนอื่นหายไปทั้งไฟล์
    cusi_block: Optional[Dict[str, Any]] = None
    cusi_df = pd.DataFrame()
    try:
        weights = load_weights(cfg)
        cusi_df, components = build_cusi_frame(df, cot_df, cfg)
        cusi_block = build_cusi_block(cusi_df, components, weights, cfg)
    except Exception:
        log.exception("CUSI computation failed — latest.json will carry cusi=null")

    latest = build_latest_json(market, jgb_df, jgb_source, cot_df, df, cfg, cusi_block, events_block)

    # ---- Phase 6D: CUSI-Lite จากแท่ง 1h (field ใหม่ — additive)
    try:
        intraday = market.intraday if market.intraday else load_cached_intraday(cfg)
        latest["cusi_lite"] = compute_cusi_lite(intraday, cfg)
    except Exception:
        log.exception("CUSI-Lite ล้มเหลว — latest.json จะไม่มี field นี้รอบนี้")
        latest["cusi_lite"] = None

    # ---- Phase 6B: field ใหม่ indicators.fx_implied (ห้ามแตะ fx_vol เดิม)
    if implied_metric:
        latest["indicators"]["fx_implied"] = implied_metric
    else:
        hist_path = resolve_path(cfg, "implied_vol_history_json")
        if hist_path.is_file():
            try:
                series = json.loads(hist_path.read_text(encoding="utf-8")).get("series", {})
                if series:
                    last_date = max(series)
                    latest["indicators"]["fx_implied"] = {
                        **series[last_date], "asof_date": last_date,
                        "source": "history_cache", "quality": "cached",
                    }
            except (json.JSONDecodeError, OSError):
                pass

    write_json(latest, resolve_path(cfg, "latest_json", mkdir=True))
    write_json(build_history_json(df, cfg), resolve_path(cfg, "indicators_history_json", mkdir=True))
    if write_cusi_history and not cusi_df.empty:
        write_json(build_cusi_history(cusi_df, cfg), resolve_path(cfg, "cusi_history_json", mkdir=True))
    if write_cot and cot_df is not None and not cot_df.empty:
        write_json(build_cot_history_json(cot_df), resolve_path(cfg, "cot_history_json", mkdir=True))

    return latest
