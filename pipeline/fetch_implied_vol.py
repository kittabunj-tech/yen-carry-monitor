"""Implied Vol Upgrade (Phase 6B) — 1M ATM IV + 25Δ risk reversal ของ JPY.

ผลประเมินแหล่งข้อมูล (ทดสอบจริง 2026-07-20)
--------------------------------------------
* **Databento** (CME 6J futures options) — คุณภาพดีสุด แต่จ่ายเงิน + ต้องมี
  API key → ขัดหลัก zero-cost ของโปรเจกต์ (เก็บไว้เป็น future upgrade)
* **CME delayed quotes** — endpoint ไม่เป็นทางการ ถูก Akamai บล็อกบอท
  และขัด ToS → ใช้จาก GitHub Actions ไม่ได้
* **Yahoo FXY options** (เลือก) — FXY = Invesco JPY ETF มี listed options
  ดึง chain ผ่าน yfinance ที่ใช้อยู่แล้ว ฟรี ไม่มี key
  ข้อแลก: chain บาง (strike ห่าง $1 ≈ 1.8%) และ delayed

บทเรียนจากการ probe: field ``impliedVolatility`` ของ Yahoo เป็นค่า
placeholder (0.00001) นอกเวลาตลาด **ทุก ticker รวม SPY** — จึงห้ามใช้
โมดูลนี้ invert Black-Scholes จาก ``lastPrice`` เอง (คงอยู่ข้ามคืน/สุดสัปดาห์)
พร้อม quality gate จากอายุของ trade ล่าสุด

CONVENTION เครื่องหมายของ risk reversal (สำคัญ — สลับแล้วความหมายกลับด้าน)
---------------------------------------------------------------------------
FXY เป็น "ฝั่งเยน" (FXY ขึ้น = เยนแข็ง = USDJPY ลง) ดังนั้น::

    RR_usdjpy(25Δ) = σ(USDJPY call) − σ(USDJPY put) = −RR_fxy

spec §2.4: "risk reversal ติดลบลึก = ตลาดจ่ายแพงเพื่อ hedge JPY แข็ง"
เป็น USDJPY convention → JSON รายงาน ``rr_25d`` ใน convention นี้
(hedge เยนแข็ง = FXY call แพง = RR_fxy บวก = ``rr_25d`` **ติดลบ**)

Backward compatible: เพิ่ม field/ไฟล์ใหม่เท่านั้น CUSI ยังใช้ realized vol
เป็น default (สลับได้ที่ ``cusi.fx_vol_source`` เมื่อสะสม history พอ)
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pipeline.config import get_logger, load_config, resolve_path

log = get_logger(__name__)

SQRT_2PI = math.sqrt(2.0 * math.pi)


# ================================================================ Black-Scholes
# ใช้ r = 0: อายุสัญญา ~1 เดือน ผลของ rate เล็กกว่า noise จาก strike ห่าง $1 มาก


def _norm_cdf(x: float) -> float:
    """CDF ของ normal มาตรฐาน (ผ่าน erf — ไม่ต้องพึ่ง scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, sigma: float, is_call: bool) -> float:
    """ราคา option แบบ Black-Scholes (r = 0, ไม่มีปันผล).

    Args:
        spot: ราคา underlying
        strike: strike
        t_years: อายุคงเหลือ (ปี)
        sigma: volatility ต่อปี (0.10 = 10%)
        is_call: True = call

    Returns:
        ราคาทฤษฎี
    """
    if t_years <= 0 or sigma <= 0:
        intrinsic = (spot - strike) if is_call else (strike - spot)
        return max(intrinsic, 0.0)
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t_years) / sq
    d2 = d1 - sq
    if is_call:
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol_from_price(
    price: float, spot: float, strike: float, t_years: float, is_call: bool,
    *, lo: float = 0.001, hi: float = 3.0, tol: float = 1e-6, max_iter: int = 100,
) -> Optional[float]:
    """Invert BS หา implied vol ด้วย bisection (ทนทานกว่า Newton บนราคาบาง ๆ).

    Args:
        price: ราคา option ที่สังเกต
        spot: ราคา underlying
        strike: strike
        t_years: อายุคงเหลือ (ปี)
        is_call: True = call
        lo: ขอบล่างของ σ ที่ค้น
        hi: ขอบบน
        tol: ความละเอียด
        max_iter: จำนวนรอบสูงสุด

    Returns:
        σ ต่อปี หรือ ``None`` ถ้าราคาอยู่นอกช่วงที่ inversion ได้
        (ต่ำกว่า intrinsic หรือสูงเกินขอบ)
    """
    if price <= 0 or spot <= 0 or strike <= 0 or t_years <= 0:
        return None
    intrinsic = max((spot - strike) if is_call else (strike - spot), 0.0)
    if price <= intrinsic + 1e-9:
        return None
    if price >= bs_price(spot, strike, t_years, hi, is_call):
        return None

    a, b = lo, hi
    for _ in range(max_iter):
        mid = 0.5 * (a + b)
        if bs_price(spot, strike, t_years, mid, is_call) < price:
            a = mid
        else:
            b = mid
        if b - a < tol:
            break
    return 0.5 * (a + b)


def call_delta(spot: float, strike: float, t_years: float, sigma: float) -> float:
    """Delta ของ call = N(d1) — ใช้หา strike ที่ 25Δ.

    Args:
        spot: ราคา underlying
        strike: strike
        t_years: อายุคงเหลือ (ปี)
        sigma: volatility ต่อปี

    Returns:
        delta ใน (0, 1)
    """
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t_years) / sq
    return _norm_cdf(d1)


# ================================================================ interpolation


def _interp(x: float, pts: List[Tuple[float, float]]) -> Optional[float]:
    """Linear interpolation บนจุด (x, y) ที่เรียงตาม x — ไม่ extrapolate.

    Args:
        x: จุดที่ต้องการ
        pts: list ของ (x, y) อย่างน้อย 2 จุด

    Returns:
        ค่า interpolate หรือ ``None`` ถ้า x อยู่นอกช่วง
    """
    pts = sorted(pts)
    if len(pts) < 2 or not (pts[0][0] <= x <= pts[-1][0]):
        return None
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return None  # pragma: no cover


def atm_iv_from_smile(smile: List[Tuple[float, float]], spot: float) -> Optional[float]:
    """IV ที่ K = spot จาก smile (strike, iv) ด้านเดียว (calls หรือ puts).

    Args:
        smile: list ของ (strike, iv)
        spot: ราคา underlying

    Returns:
        ATM IV หรือ ``None``
    """
    return _interp(spot, smile)


def iv_at_delta(
    smile: List[Tuple[float, float]], spot: float, t_years: float,
    target_delta: float, *, is_call: bool,
) -> Optional[float]:
    """IV ที่ระดับ delta เป้าหมาย (เช่น 0.25) โดย interpolate IV เทียบ delta.

    delta ของแต่ละ strike คำนวณจาก IV ของ strike นั้นเอง (ตามธรรมเนียมตลาด)
    put ใช้ |delta| = 1 − N(d1)

    Args:
        smile: list ของ (strike, iv)
        spot: ราคา underlying
        t_years: อายุคงเหลือ (ปี)
        target_delta: เช่น 0.25
        is_call: ด้านของ smile

    Returns:
        IV ที่ delta เป้าหมาย หรือ ``None`` ถ้า wing ไม่พอ
    """
    pts = []
    for k, iv in smile:
        d = call_delta(spot, k, t_years, iv)
        pts.append((d if is_call else 1.0 - d, iv))
    return _interp(target_delta, pts)


# ================================================================ chain → metrics


def compute_metrics(
    calls: List[Dict[str, Any]],
    puts: List[Dict[str, Any]],
    spot: float,
    dte_days: float,
    cfg_iv: Dict[str, Any],
    *, now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """คำนวณ ATM IV + 25Δ RR จาก chain ดิบ พร้อม quality gate.

    Args:
        calls: list ของ dict ``{strike, last_price, last_trade}`` (last_trade = ISO)
        puts: เดียวกันฝั่ง put
        spot: ราคา FXY
        dte_days: วันถึง expiry
        cfg_iv: section ``implied_vol`` จาก config
        now: เวลาปัจจุบัน (ฉีดได้ในเทสต์)

    Returns:
        dict metric + ``quality`` ("ok" / "stale_quotes" / "insufficient")
    """
    now = now or datetime.now(timezone.utc)
    max_age = float(cfg_iv.get("max_quote_age_days", 5))
    min_px = float(cfg_iv.get("min_option_price", 0.05))
    iv_lo, iv_hi = cfg_iv.get("iv_bounds", [0.005, 0.60])
    t_years = dte_days / 365.0

    def usable(rows: List[Dict[str, Any]], is_call: bool) -> Tuple[List[Tuple[float, float]], int]:
        """กรองสัญญาใช้ได้ → smile (strike, iv) + จำนวนที่สดจริง."""
        smile, fresh = [], 0
        for r in rows:
            px = r.get("last_price") or 0.0
            if px < min_px:
                continue
            ts = r.get("last_trade")
            age_ok = False
            if ts:
                try:
                    age = (now - datetime.fromisoformat(str(ts)).astimezone(timezone.utc)).days
                    age_ok = age <= max_age
                except ValueError:
                    pass
            iv = implied_vol_from_price(px, spot, float(r["strike"]), t_years, is_call)
            if iv is None or not (iv_lo <= iv <= iv_hi):
                continue
            smile.append((float(r["strike"]), iv))
            if age_ok:
                fresh += 1
        return sorted(smile), fresh

    c_smile, c_fresh = usable(calls, True)
    p_smile, p_fresh = usable(puts, False)

    out: Dict[str, Any] = {
        "n_calls_used": len(c_smile), "n_puts_used": len(p_smile),
        "iv_atm_1m_pct": None, "rr_25d_pct": None,
        "rr_convention": "usdjpy (ติดลบ = hedge เยนแข็งแพง)",
    }

    if len(c_smile) < 2 and len(p_smile) < 2:
        out["quality"] = "insufficient"
        return out

    # ---- ATM: เฉลี่ยสองฝั่งเท่าที่ interpolate ได้
    atms = [v for v in (atm_iv_from_smile(c_smile, spot), atm_iv_from_smile(p_smile, spot)) if v]
    if atms:
        out["iv_atm_1m_pct"] = round(sum(atms) / len(atms) * 100.0, 2)

    # ---- 25Δ RR (ต้องมี wing ทั้งสองฝั่ง)
    iv_c25 = iv_at_delta(c_smile, spot, t_years, 0.25, is_call=True) if len(c_smile) >= 2 else None
    iv_p25 = iv_at_delta(p_smile, spot, t_years, 0.25, is_call=False) if len(p_smile) >= 2 else None
    if iv_c25 is not None and iv_p25 is not None:
        # RR ของ FXY แล้วกลับเครื่องหมายเป็น USDJPY convention (ดู docstring บนสุด)
        out["rr_25d_pct"] = round(-(iv_c25 - iv_p25) * 100.0, 2)

    if out["iv_atm_1m_pct"] is None:
        out["quality"] = "insufficient"
    elif c_fresh + p_fresh == 0:
        out["quality"] = "stale_quotes"      # invert ได้แต่ trade เก่าเกิน — ไม่เก็บเข้า history
    else:
        out["quality"] = "ok"
    return out


# ================================================================ fetch


def fetch_implied_vol(cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """ดึง FXY option chain แล้วคำนวณ metric หนึ่งรอบ.

    Args:
        cfg: config dict; ``None`` = โหลดเอง

    Returns:
        dict metric (รวม ``quality``) หรือ ``None`` ถ้าดึง chain ไม่ได้เลย
    """
    cfg = cfg if cfg is not None else load_config()
    cfg_iv = cfg.get("implied_vol", {})
    symbol = cfg_iv.get("symbol", "FXY")
    target = int(cfg_iv.get("target_dte_days", 30))
    dte_min, dte_max = cfg_iv.get("dte_range", [20, 45])

    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        expiries = t.options
        spot = float(t.history(period="5d")["Close"].iloc[-1])
    except Exception as exc:
        log.warning("implied vol: ดึง chain %s ไม่ได้: %s", symbol, exc)
        return None

    today = date.today()
    candidates = []
    for e in expiries or ():
        try:
            dte = (date.fromisoformat(e) - today).days
        except ValueError:
            continue
        if dte_min <= dte <= dte_max:
            candidates.append((abs(dte - target), e, dte))
    if not candidates:
        log.warning("implied vol: ไม่มี expiry ในช่วง %d–%d วัน", dte_min, dte_max)
        return None
    _, expiry, dte = min(candidates)

    try:
        ch = t.option_chain(expiry)
        rows = lambda df: [
            {"strike": float(r.strike), "last_price": float(r.lastPrice or 0),
             "last_trade": str(r.lastTradeDate) if r.lastTradeDate is not None else None}
            for r in df.itertuples()
        ]
        calls, puts = rows(ch.calls), rows(ch.puts)
    except Exception as exc:
        log.warning("implied vol: อ่าน chain %s/%s ไม่ได้: %s", symbol, expiry, exc)
        return None

    out = compute_metrics(calls, puts, spot, dte, cfg_iv)
    out.update({
        "source": f"{symbol}_options_yahoo", "expiry": expiry, "dte_days": dte,
        "spot": round(spot, 2),
        "asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    log.info("implied vol: ATM=%s%% RR25Δ=%s%% (exp %s, %dd, quality=%s)",
             out["iv_atm_1m_pct"], out["rr_25d_pct"], expiry, dte, out["quality"])
    return out


# ================================================================ history


def update_history(metric: Optional[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """append metric ลง ``data/implied_vol_history.json`` (เก็บเฉพาะ quality=ok).

    z-score ของ CUSI ต้องการ history ยาว — ไฟล์นี้คือการ "เริ่มออม" ตั้งแต่วันนี้
    (ไม่มีแหล่ง history ย้อนหลังฟรี) เก็บ 1 จุด/วัน วันซ้ำ = ทับด้วยค่าล่าสุด

    Args:
        metric: ผลจาก :func:`fetch_implied_vol` (``None``/quality แย่ = ไม่ append)
        cfg: config dict

    Returns:
        payload ทั้งไฟล์ (``{"series": {date: {...}}}``)
    """
    path = resolve_path(cfg, "implied_vol_history_json", mkdir=True)
    payload: Dict[str, Any] = {"series": {}}
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("อ่าน implied history ไม่ได้: %s", exc)

    series = payload.setdefault("series", {})
    if metric and metric.get("quality") == "ok":
        series[date.today().isoformat()] = {
            "iv_atm_1m_pct": metric["iv_atm_1m_pct"],
            "rr_25d_pct": metric["rr_25d_pct"],
            "expiry": metric["expiry"],
        }

    # prune เกิน 3 ปี (ให้พอสำหรับ z window 252 + เผื่อ)
    cutoff = (date.today() - timedelta(days=3 * 366)).isoformat()
    payload["series"] = {k: v for k, v in sorted(series.items()) if k >= cutoff}
    payload["count"] = len(payload["series"])
    payload["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover
        log.warning("เขียน implied history ไม่ได้: %s", exc)
    return payload


def read_history_series(cfg: Dict[str, Any]) -> Dict[str, float]:
    """อ่าน history เป็น mapping ``date → iv_atm`` สำหรับประกอบ indicator frame.

    Args:
        cfg: config dict

    Returns:
        dict (ว่างถ้ายังไม่มีไฟล์)
    """
    path = resolve_path(cfg, "implied_vol_history_json")
    if not path.is_file():
        return {}
    try:
        series = json.loads(path.read_text(encoding="utf-8")).get("series", {})
        return {k: v["iv_atm_1m_pct"] for k, v in series.items()
                if v.get("iv_atm_1m_pct") is not None}
    except (json.JSONDecodeError, OSError, KeyError):
        return {}


if __name__ == "__main__":  # pragma: no cover
    from pipeline.config import setup_logging
    _cfg = load_config()
    setup_logging(_cfg)
    _m = fetch_implied_vol(_cfg)
    if _m:
        log.info("ผล: %s", json.dumps(_m, ensure_ascii=False, indent=1))
        update_history(_m, _cfg)
