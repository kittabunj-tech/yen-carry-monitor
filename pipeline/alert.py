"""Telegram alert เมื่อระดับ CUSI เปลี่ยน หรือกระโดดแรง (spec §4.6).

Trigger:
  1. ระดับ CUSI เปลี่ยน (ขึ้นหรือลง) เช่น WATCH → STRESS
  2. CUSI กระโดดเกิน ``cusi.alert_jump_threshold`` (10 จุด) ใน 1 วัน

โมดูลนี้ทำ 3 อย่าง:
  * เขียน alert ลง ``data/alerts.json`` (ประวัติแบบ rolling)
  * เติม ``alerts[]`` ใน ``latest.json`` ให้ frontend แสดง (Phase 3 รองรับแล้ว)
  * ส่ง Telegram ถ้าตั้ง secrets ไว้

ออกแบบให้ **ไม่ทำให้ workflow ล้ม** — ถ้าส่งไม่สำเร็จจะ log แล้วคืน exit 0
เพราะข้อมูลถูกอัพเดทไปแล้ว การแจ้งเตือนล้มเหลวไม่ควรทำให้ commit ล้มตาม

    python pipeline/alert.py                  # ตรวจและส่งถ้าเข้าเงื่อนไข
    python pipeline/alert.py --dry-run        # แสดงข้อความโดยไม่ส่งจริง
    python pipeline/alert.py --test           # ส่งข้อความทดสอบ (เช็ค token/chat_id)
    python pipeline/alert.py --force          # ส่งสถานะปัจจุบันแม้ระดับไม่เปลี่ยน
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import get_logger, load_config, resolve_path, setup_logging  # noqa: E402

log = get_logger(__name__)

#: จำนวน alert สูงสุดที่เก็บใน history (คุมขนาดไฟล์ตาม §4.7)
MAX_HISTORY = 100

#: จำนวน alert ที่ส่งไปแสดงบนหน้าเว็บ
MAX_IN_LATEST = 20

#: ชื่อ env var ของ secrets
ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT = "TELEGRAM_CHAT_ID"

#: ชื่อไทยของ component สำหรับข้อความแจ้งเตือน
COMPONENT_LABELS = {
    "jpy_momentum": "JPY Momentum",
    "audjpy_drawdown": "AUDJPY Drawdown",
    "spread_compression": "Spread Compression",
    "vix": "VIX",
    "fx_realized_vol": "FX Realized Vol",
    "cot_positioning": "COT Positioning",
    "move": "MOVE",
    "nikkei_momentum": "Nikkei Momentum",
    "mxnjpy_drawdown": "MXNJPY Drawdown",
}


# ---------------------------------------------------------------- state I/O


def _read_json(path: Path, default: Any) -> Any:
    """อ่าน JSON แบบไม่ระเบิดถ้าไฟล์หายหรือเสีย.

    Args:
        path: พาธไฟล์
        default: ค่าที่คืนเมื่ออ่านไม่ได้

    Returns:
        payload หรือ ``default``
    """
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("อ่าน %s ไม่ได้: %s", path.name, exc)
        return default


def _write_json(payload: Any, path: Path) -> None:
    """เขียน JSON แบบ atomic.

    Args:
        payload: ข้อมูลที่จะเขียน
        path: ปลายทาง
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- logic


def detect_alerts(
    latest: Dict[str, Any],
    state: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """เทียบสถานะปัจจุบันกับครั้งก่อน แล้วสร้าง alert ที่เข้าเงื่อนไข (§4.6).

    Args:
        latest: payload ของ latest.json
        state: สถานะครั้งก่อน ``{"level", "value", "as_of"}``
        cfg: config dict

    Returns:
        list ของ alert (ว่างถ้าไม่มีอะไรเปลี่ยน)
    """
    cusi = latest.get("cusi")
    if not cusi or cusi.get("value") is None:
        log.info("ไม่มีค่า CUSI — ข้ามการแจ้งเตือน")
        return []

    value = float(cusi["value"])
    level = cusi.get("level")
    as_of = cusi.get("as_of")
    jump_threshold = float(cfg["cusi"].get("alert_jump_threshold", 10.0))

    prev_level = state.get("level")
    prev_value = state.get("value")
    prev_as_of = state.get("as_of")

    # ข้อมูลยังเป็นวันเดิม + ระดับเดิม → ไม่มีอะไรใหม่ (กันส่งซ้ำทุกชั่วโมง)
    if prev_as_of == as_of and prev_level == level:
        log.info("ไม่มีการเปลี่ยนแปลง (level=%s as_of=%s)", level, as_of)
        return []

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    alerts: List[Dict[str, Any]] = []

    # ---- 1) ระดับเปลี่ยน
    if prev_level and level and prev_level != level:
        direction = "ขึ้น" if _rank(level, cfg) > _rank(prev_level, cfg) else "ลง"
        alerts.append({
            "ts": ts,
            "type": "level_change",
            "from": prev_level,
            "to": level,
            "value": round(value, 1),
            "reason": f"ระดับ CUSI เปลี่ยน{direction}จาก {prev_level} เป็น {level} (CUSI {value:.1f})",
        })

    # ---- 2) กระโดดแรงใน 1 วัน
    change = cusi.get("change_1d")
    if change is not None and abs(float(change)) >= jump_threshold:
        alerts.append({
            "ts": ts,
            "type": "jump",
            "from": prev_level or level,
            "to": level,
            "value": round(value, 1),
            "reason": f"CUSI เปลี่ยน {float(change):+.1f} จุดใน 1 วัน (เกินเกณฑ์ {jump_threshold:.0f})",
        })

    if not alerts and prev_level is None:
        log.info("รันครั้งแรก — บันทึกสถานะไว้เฉย ๆ ไม่ส่งแจ้งเตือน")

    return alerts


def _rank(level: Optional[str], cfg: Dict[str, Any]) -> int:
    """ลำดับความรุนแรงของระดับ (ใช้บอกว่าขึ้นหรือลง).

    Args:
        level: ชื่อระดับ
        cfg: config dict

    Returns:
        ดัชนีของระดับใน config (ยิ่งมาก = ยิ่งรุนแรง)
    """
    names = [str(lv["name"]) for lv in cfg["cusi"]["levels"]]
    return names.index(level) if level in names else -1


def format_message(latest: Dict[str, Any], alerts: List[Dict[str, Any]], dashboard_url: str = "") -> str:
    """สร้างข้อความ Telegram (HTML) — ค่า CUSI, ระดับ, top-3 components, ลิงก์ (§4.6).

    Args:
        latest: payload ของ latest.json
        alerts: alert ที่ตรวจพบ
        dashboard_url: ลิงก์ไปหน้า dashboard

    Returns:
        ข้อความพร้อมส่ง
    """
    cusi = latest.get("cusi") or {}
    value = cusi.get("value")
    level = cusi.get("level", "—")
    emoji = cusi.get("level_emoji", "")
    change = cusi.get("change_1d")

    lines = [f"{emoji} <b>CUSI {value:.1f} — {level}</b>" if value is not None else "<b>CUSI</b>"]

    if change is not None:
        lines.append(f"เปลี่ยนแปลง 1 วัน: <b>{float(change):+.1f}</b>")
    if cusi.get("as_of"):
        lines.append(f"ข้อมูล ณ {cusi['as_of']}")

    lines.append("")
    for a in alerts:
        lines.append(f"⚠️ {a['reason']}")

    # ---- top-3 components ที่ผลักดัน (§4.6)
    components = cusi.get("components") or {}
    if components:
        ranked = sorted(components.items(), key=lambda kv: kv[1].get("contribution", 0), reverse=True)[:3]
        lines.append("")
        lines.append("<b>Top 3 ที่ผลักดัน:</b>")
        for name, c in ranked:
            label = COMPONENT_LABELS.get(name, name)
            lines.append(f"  • {label}: z {c.get('z', 0):+.2f} (น้ำหนัก {c.get('weight', 0)*100:.0f}%)")

    # ---- เตือนเรื่องคุณภาพข้อมูล ถ้ามี
    dq = latest.get("data_quality") or {}
    problems = []
    if dq.get("missing"):
        problems.append(f"ขาด: {', '.join(dq['missing'])}")
    if dq.get("stale"):
        problems.append(f"ค้าง: {', '.join(dq['stale'])}")
    if problems:
        lines.append("")
        lines.append(f"<i>คุณภาพข้อมูล — {' · '.join(problems)}</i>")

    if dashboard_url:
        lines.append("")
        lines.append(f'<a href="{dashboard_url}">เปิด dashboard</a>')

    return "\n".join(lines)


def send_telegram(message: str, token: str, chat_id: str, *, timeout: float = 20.0) -> bool:
    """ส่งข้อความผ่าน Telegram Bot API.

    Args:
        message: ข้อความ (HTML)
        token: bot token จาก @BotFather
        chat_id: chat id ปลายทาง
        timeout: timeout ของ request

    Returns:
        True ถ้าส่งสำเร็จ
    """
    import requests

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=timeout,
        )
        if resp.status_code == 200:
            log.info("ส่ง Telegram สำเร็จ")
            return True
        log.error("Telegram ตอบ %s: %s", resp.status_code, resp.text[:300])
    except Exception as exc:
        log.error("ส่ง Telegram ไม่สำเร็จ: %s", exc)
    return False


# ---------------------------------------------------------------- main


def run(args: argparse.Namespace) -> int:
    """ตรวจสอบและส่งการแจ้งเตือนหนึ่งรอบ.

    Args:
        args: argument จาก CLI

    Returns:
        exit code (0 เสมอ ยกเว้นอ่าน latest.json ไม่ได้)
    """
    cfg = load_config()
    setup_logging(cfg)

    latest_path = resolve_path(cfg, "latest_json")
    data_dir = resolve_path(cfg, "data_dir", mkdir=True)
    state_path = data_dir / "alert_state.json"
    history_path = data_dir / "alerts.json"

    token = os.environ.get(ENV_TOKEN, "").strip()
    chat_id = os.environ.get(ENV_CHAT, "").strip()

    # ---- โหมดทดสอบการเชื่อมต่อ
    if args.test:
        if not token or not chat_id:
            log.error("ต้องตั้ง %s และ %s ก่อน", ENV_TOKEN, ENV_CHAT)
            return 1
        ok = send_telegram(
            "✅ <b>Yen Carry Monitor</b>\nการเชื่อมต่อ Telegram ทำงานปกติ",
            token, chat_id,
        )
        return 0 if ok else 1

    latest = _read_json(latest_path, None)
    if not latest:
        log.error("อ่าน %s ไม่ได้ — รัน pipeline ก่อน", latest_path)
        return 1

    state = _read_json(state_path, {})
    alerts = detect_alerts(latest, state, cfg)

    if args.force and not alerts:
        cusi = latest.get("cusi") or {}
        alerts = [{
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "type": "manual",
            "from": cusi.get("level"),
            "to": cusi.get("level"),
            "value": cusi.get("value"),
            "reason": "ส่งด้วยตนเอง (--force)",
        }]

    if not alerts:
        # อัพเดท state ไว้เสมอ เพื่อให้รอบถัดไปเทียบกับข้อมูลล่าสุด
        _save_state(latest, state_path)
        return 0

    for a in alerts:
        log.info("ALERT [%s] %s", a["type"], a["reason"])

    # ---- เขียนประวัติ + เติมลง latest.json ให้หน้าเว็บแสดง
    history = _read_json(history_path, {"alerts": []})
    history_list = list(history.get("alerts", [])) + alerts
    history_list = history_list[-MAX_HISTORY:]

    if not args.dry_run:
        _write_json({"alerts": history_list}, history_path)
        latest["alerts"] = history_list[-MAX_IN_LATEST:]
        _write_json(latest, latest_path)
        log.info("เขียน alert ลง %s และ %s", history_path.name, latest_path.name)

    # ---- ส่ง Telegram
    message = format_message(latest, alerts, args.url or os.environ.get("DASHBOARD_URL", ""))
    if args.dry_run:
        log.info("--dry-run: ข้อความที่จะส่ง\n%s", message)
    elif token and chat_id:
        send_telegram(message, token, chat_id)
    else:
        log.warning("ไม่ได้ตั้ง %s / %s — ข้ามการส่ง (แต่บันทึก alert แล้ว)", ENV_TOKEN, ENV_CHAT)

    if not args.dry_run:
        _save_state(latest, state_path)
    return 0


def _save_state(latest: Dict[str, Any], path: Path) -> None:
    """บันทึกสถานะปัจจุบันไว้เทียบรอบถัดไป.

    Args:
        latest: payload ของ latest.json
        path: ไฟล์ state
    """
    cusi = latest.get("cusi") or {}
    _write_json(
        {
            "level": cusi.get("level"),
            "value": cusi.get("value"),
            "as_of": cusi.get("as_of"),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        path,
    )


def main(argv: Optional[list] = None) -> int:
    """Entry point ของ CLI.

    Args:
        argv: argument list; ``None`` = ใช้ ``sys.argv``

    Returns:
        exit code
    """
    parser = argparse.ArgumentParser(description="Telegram alert สำหรับ Yen Carry Monitor")
    parser.add_argument("--dry-run", action="store_true", help="แสดงข้อความโดยไม่ส่งและไม่เขียนไฟล์")
    parser.add_argument("--test", action="store_true", help="ส่งข้อความทดสอบเพื่อเช็ค token/chat_id")
    parser.add_argument("--force", action="store_true", help="ส่งสถานะปัจจุบันแม้ระดับไม่เปลี่ยน")
    parser.add_argument("--url", default="", help="ลิงก์ dashboard ที่จะแนบไปในข้อความ")
    args = parser.parse_args(argv)

    try:
        return run(args)
    except Exception:
        log.exception("alert ล้มเหลว — ไม่ทำให้ workflow ล้มตาม")
        return 0


if __name__ == "__main__":
    sys.exit(main())
