"""BOJ/Fed Event Layer (Phase 6C) — ปฏิทิน BOJ MPM + FOMC + CPI ญี่ปุ่น.

แหล่งข้อมูล (ตรวจสอบโครงสร้างจริงแล้ว 2026-07):
  * BOJ MPM  — scrape ตารางในหน้า EN ของ BOJ
      กติกากรอง: การประชุมจริงเป็น **คู่สองวันเสมอ** ("July 30 (Thurs.), 31 (Fri.)")
      แถววันเดียวในตารางเดียวกันคือวันออก minutes/opinions ไม่ใช่การประชุม
  * FOMC     — scrape หน้า calendar ของ Fed (section ต่อปี "YYYY FOMC Meetings")
      วันตัดสิน = วันสุดท้ายของช่วง เช่น "28-29" → วันที่ 29
  * CPI ญี่ปุ่น — **ไม่ scrape**: ปฏิทิน e-stat เป็น Drupal ที่ render ด้วย JS
      (ตรวจแล้ว ไม่มีข้อมูลใน HTML ดิบ) จึงใช้กติกา "ศุกร์ที่สามของเดือน" ติดธง
      ``approx: true`` + รายการ override ใน config สำหรับ pin วันจริง

ความทนทาน: ถ้า scrape พังใช้ cache (``data/events.json``) ของรอบก่อน —
ปฏิทินเปลี่ยนปีละครั้ง ความสดไม่ใช่เรื่องสำคัญเท่าความไม่พัง

Backward compatible: โมดูลนี้ **เพิ่ม** ไฟล์ใหม่และ field ใหม่เท่านั้น
ไม่แตะ field เดิมใน latest.json
"""

from __future__ import annotations

import html as htmllib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pipeline.config import get_logger, load_config, resolve_path

log = get_logger(__name__)

#: เดือนย่อ 3 ตัวอักษร → เลขเดือน (BOJ ใช้ "Sept." ซึ่งตัดเหลือ "Sep" ได้พอดี)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

#: field มาตรฐานของ event หนึ่งรายการ
EVENT_FIELDS = ("type", "label", "start_date", "date", "approx", "source")


def _parse_month(token: Optional[str]) -> Optional[int]:
    """แปลงชื่อเดือน (เต็ม/ย่อ/มีจุด) เป็นเลขเดือน.

    Args:
        token: เช่น ``"July"``, ``"Sept."``, ``"Jan"``

    Returns:
        1–12 หรือ ``None`` ถ้าไม่รู้จัก
    """
    if not token:
        return None
    return _MONTHS.get(token.strip().rstrip(".")[:3].title())


# ---------------------------------------------------------------- BOJ


def parse_boj(html: str) -> List[Dict[str, Any]]:
    """ดึงวันประชุม MPM จากหน้า EN ของ BOJ.

    หน้าแบ่ง section ต่อปีด้วย anchor ``id="pYYYY"`` และแถวการประชุมเป็น
    คู่สองวัน (อาจคร่อมเดือน เช่น "Apr. 30 (Thurs.), May 1 (Fri.)")

    Args:
        html: เนื้อหา HTML ของหน้า BOJ

    Returns:
        list ของ event dict (วัน ``date`` = วันที่สองของการประชุม = วันแถลงผล)
    """
    text = re.sub(r"<script.*?</script>", "", htmllib.unescape(html), flags=re.S)
    sections = re.split(r'id="p(\d{4})"', text)

    events: List[Dict[str, Any]] = []
    seen = set()
    for i in range(1, len(sections) - 1, 2):
        year = int(sections[i])
        plain = re.sub(r"[|\s]+", " ", re.sub(r"<[^>]+>", "|", sections[i + 1]))
        for m in re.finditer(
            r"([A-Z][a-z]+)\.?\s+(\d{1,2})\s*\([^)]+\)\s*,\s*"
            r"(?:([A-Z][a-z]+)\.?\s+)?(\d{1,2})\s*\([^)]+\)",
            plain,
        ):
            mo1 = _parse_month(m.group(1))
            mo2 = _parse_month(m.group(3)) if m.group(3) else mo1
            if not mo1 or not mo2:
                continue
            try:
                start = date(year, mo1, int(m.group(2)))
                end = date(year, mo2, int(m.group(4)))
            except ValueError:
                continue
            # การประชุมจริง: วันติดกัน (กันจับคู่มั่วจากข้อความอื่นในหน้า)
            if not (timedelta(0) < (end - start) <= timedelta(days=3)):
                continue
            if end in seen:
                continue
            seen.add(end)
            events.append({
                "type": "boj_mpm",
                "label": "BOJ Monetary Policy Meeting",
                "start_date": start.isoformat(),
                "date": end.isoformat(),
                "approx": False,
                "source": "boj",
            })
    log.info("BOJ: พบการประชุม %d ครั้ง", len(events))
    return events


# ---------------------------------------------------------------- FOMC


def parse_fomc(html: str) -> List[Dict[str, Any]]:
    """ดึงวันประชุม FOMC จากหน้า calendar ของ Fed.

    Args:
        html: เนื้อหา HTML ของหน้า Fed

    Returns:
        list ของ event dict (``date`` = วันสุดท้าย = วันแถลง)
    """
    parts = re.split(r"(\d{4}) FOMC Meetings", html)
    events: List[Dict[str, Any]] = []

    for i in range(1, len(parts) - 1, 2):
        year = int(parts[i])
        for m in re.finditer(
            r'fomc-meeting__month[^>]*>(?:<strong>)?([A-Za-z/]+)(?:</strong>)?</div>\s*'
            r'<div class="fomc-meeting__date[^>]*>([^<]+)</div>',
            parts[i + 1],
        ):
            months, days_raw = m.group(1), m.group(2)
            days = re.findall(r"\d+", re.sub(r"\(.*?\)", "", days_raw))
            if not days or "unscheduled" in days_raw.lower():
                continue
            mo_start = _parse_month(months.split("/")[0])
            mo_end = _parse_month(months.split("/")[-1])
            if not mo_start or not mo_end:
                continue
            try:
                start = date(year, mo_start, int(days[0]))
                end = date(year, mo_end, int(days[-1]))
            except ValueError:
                continue
            events.append({
                "type": "fomc",
                "label": "FOMC Meeting",
                "start_date": start.isoformat(),
                "date": end.isoformat(),
                "approx": False,
                "source": "federalreserve",
            })
    log.info("FOMC: พบการประชุม %d ครั้ง", len(events))
    return events


# ---------------------------------------------------------------- Japan CPI


def third_friday(year: int, month: int) -> date:
    """ศุกร์ที่สามของเดือน — วันประกาศ CPI ญี่ปุ่นโดยประมาณ (8:30 JST).

    Args:
        year: ปี ค.ศ.
        month: เดือน

    Returns:
        วันที่ศุกร์ที่สาม
    """
    d = date(year, month, 1)
    fridays = [d + timedelta(days=x) for x in range(21)
               if (d + timedelta(days=x)).weekday() == 4]
    return fridays[2]


def japan_cpi_events(cfg_events: Dict[str, Any], today: date, months_ahead: int = 6) -> List[Dict[str, Any]]:
    """สร้างรายการวันประกาศ CPI ญี่ปุ่นจากกติกา + override.

    ปฏิทินทางการ (e-stat) render ด้วย JS จึง scrape ไม่ได้ — ใช้กติกา
    "ศุกร์ที่สาม" ติดธง ``approx`` และให้ config pin วันจริงทับได้

    Args:
        cfg_events: section ``events`` จาก config
        today: วันปัจจุบัน
        months_ahead: จำนวนเดือนล่วงหน้าที่สร้าง

    Returns:
        list ของ event dict
    """
    overrides = {str(d) for d in cfg_events.get("jp_cpi_overrides", []) or []}
    events: List[Dict[str, Any]] = []

    # override ที่ผู้ใช้ pin = วันจริง ไม่ approx
    for iso in sorted(overrides):
        events.append({
            "type": "jp_cpi", "label": "Japan CPI (national)",
            "start_date": iso, "date": iso, "approx": False, "source": "config",
        })

    override_months = {iso[:7] for iso in overrides}
    y, mo = today.year, today.month
    for _ in range(months_ahead + 1):
        d = third_friday(y, mo)
        # เดือนที่มี override แล้ว ไม่ต้องสร้างจากกติกาซ้ำ
        if f"{y}-{mo:02d}" not in override_months and d >= today - timedelta(days=31):
            events.append({
                "type": "jp_cpi", "label": "Japan CPI (national)",
                "start_date": d.isoformat(), "date": d.isoformat(),
                "approx": True, "source": "rule:third_friday",
            })
        mo += 1
        if mo > 12:
            mo, y = 1, y + 1
    return events


# ---------------------------------------------------------------- fetch + merge


def _http_get(url: str, cfg: Dict[str, Any]) -> Optional[str]:
    """GET พร้อม retry ตาม config เดียวกับ fetcher อื่น.

    Args:
        url: ปลายทาง
        cfg: config dict ทั้งไฟล์

    Returns:
        HTML string หรือ ``None``
    """
    import time
    import requests

    f = cfg["fetch"]
    for attempt in range(1, int(f["retries"]) + 1):
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "yen-carry-monitor/1.0"})
            r.raise_for_status()
            return r.text
        except requests.RequestException as exc:
            log.warning("GET %s ล้มเหลว (%d/%d): %s", url, attempt, f["retries"], exc)
            if attempt < int(f["retries"]):
                time.sleep(min(f["backoff_base_sec"] * 2 ** (attempt - 1), f["backoff_max_sec"]))
    return None


def fetch_events(cfg: Optional[Dict[str, Any]] = None, *, use_network: bool = True) -> Dict[str, Any]:
    """ดึง/ประกอบปฏิทินเหตุการณ์ทั้งหมด แล้ว cache ลง ``data/events.json``.

    Args:
        cfg: config dict; ``None`` = โหลดเอง
        use_network: ``False`` = ใช้ cache อย่างเดียว (โหมด hourly)

    Returns:
        payload ``{"generated_at", "sources", "events": [...]}``
    """
    cfg = cfg if cfg is not None else load_config()
    ev_cfg = cfg.get("events", {})
    cache_path = resolve_path(cfg, "events_json", mkdir=True)
    today = datetime.now(timezone.utc).date()

    cached = _read_cache(cache_path)
    sources: Dict[str, str] = {}
    scraped: List[Dict[str, Any]] = []

    if use_network:
        for name, url, parser in (
            ("boj", ev_cfg.get("boj_url"), parse_boj),
            ("fomc", ev_cfg.get("fomc_url"), parse_fomc),
        ):
            html = _http_get(url, cfg) if url else None
            parsed = parser(html) if html else []
            if parsed:
                scraped.extend(parsed)
                sources[name] = "scrape"
            else:
                # scrape พัง → กู้ประเภทนี้จาก cache เดิม
                fallback = [e for e in cached if e.get("type") == ("boj_mpm" if name == "boj" else "fomc")]
                scraped.extend(fallback)
                sources[name] = "cache" if fallback else "none"
                log.warning("%s: scrape ไม่สำเร็จ ใช้ cache (%d รายการ)", name, len(fallback))
    else:
        scraped = [e for e in cached if e.get("type") in ("boj_mpm", "fomc")]
        sources["boj"] = sources["fomc"] = "cache" if scraped else "none"

    scraped.extend(japan_cpi_events(ev_cfg, today))
    sources["jp_cpi"] = "rule+overrides"

    # ตัดตามหน้าต่างเวลา + กันซ้ำ + เรียง
    keep_past = int(ev_cfg.get("keep_past_days", 30))
    horizon = int(ev_cfg.get("horizon_days", 400))
    lo, hi = today - timedelta(days=keep_past), today + timedelta(days=horizon)

    uniq: Dict[str, Dict[str, Any]] = {}
    for e in scraped:
        try:
            d = date.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        if lo <= d <= hi:
            uniq[f"{e['type']}:{e['date']}"] = {k: e.get(k) for k in EVENT_FIELDS}

    events = sorted(uniq.values(), key=lambda e: e["date"])
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "events": events,
    }

    try:
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover
        log.warning("เขียน events cache ไม่ได้: %s", exc)

    log.info("events: %d รายการ (%s → %s) | sources=%s",
             len(events), events[0]["date"] if events else "-",
             events[-1]["date"] if events else "-", sources)
    return payload


def _read_cache(path: Path) -> List[Dict[str, Any]]:
    """อ่าน event list จาก cache เดิม (ว่างถ้าไม่มี).

    Args:
        path: พาธ events.json

    Returns:
        list ของ event dict
    """
    if not path.is_file():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("events", []))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("อ่าน events cache ไม่ได้: %s", exc)
        return []


# ---------------------------------------------------------------- latest.json block


def build_events_block(
    payload: Dict[str, Any],
    cfg: Dict[str, Any],
    *,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """สร้าง block ``events`` สำหรับ latest.json (field ใหม่ — additive เท่านั้น).

    ธง ``risk_active`` = มีการประชุม (BOJ/FOMC) ภายใน ``risk_window_days``
    วันข้างหน้า (นับถึงวันแถลงผล รวมวันประชุมเอง) — spec Phase 6C:
    "ช่วง 3 วันก่อนประชุมให้ธง event risk"

    Args:
        payload: ผลจาก :func:`fetch_events`
        cfg: config dict
        today: วันปัจจุบัน (ฉีดได้ในเทสต์)

    Returns:
        dict ``{"risk_active", "risk_window_days", "upcoming", "sources"}``
    """
    ev_cfg = cfg.get("events", {})
    window = int(ev_cfg.get("risk_window_days", 3))
    risk_types = set(ev_cfg.get("risk_types", ["boj_mpm", "fomc"]))
    n_upcoming = int(ev_cfg.get("upcoming_count", 5))
    today = today or datetime.now(timezone.utc).date()

    upcoming = []
    risk_events = []
    for e in payload.get("events", []):
        try:
            d = date.fromisoformat(e["date"])
            start = date.fromisoformat(e.get("start_date") or e["date"])
        except (KeyError, ValueError):
            continue
        if d < today:
            continue
        days_until = (start - today).days if start >= today else 0
        item = {**{k: e.get(k) for k in EVENT_FIELDS}, "days_until": days_until}
        upcoming.append(item)
        # หน้าต่างเสี่ยง: ตั้งแต่ (วันเริ่ม − window) จนถึงวันแถลงผล
        if e.get("type") in risk_types and (start - timedelta(days=window)) <= today <= d:
            risk_events.append(item)

    return {
        "risk_active": bool(risk_events),
        "risk_window_days": window,
        "risk_events": risk_events,
        "upcoming": upcoming[:n_upcoming],
        "sources": payload.get("sources", {}),
    }


if __name__ == "__main__":  # pragma: no cover
    from pipeline.config import setup_logging

    _cfg = load_config()
    setup_logging(_cfg)
    _p = fetch_events(_cfg)
    _b = build_events_block(_p, _cfg)
    log.info("risk_active=%s | upcoming:", _b["risk_active"])
    for _e in _b["upcoming"]:
        log.info("  %s %-28s อีก %d วัน%s", _e["date"], _e["label"], _e["days_until"],
                 " (≈)" if _e["approx"] else "")
