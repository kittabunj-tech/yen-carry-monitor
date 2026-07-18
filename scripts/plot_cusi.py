"""วาดกราฟ CUSI ย้อนหลังไว้ตรวจสอบด้วยตา (sanity check ก่อนทำ Phase 3/4).

ไม่ใช่ส่วนหนึ่งของ pipeline หลัก — อ่าน ``data/cusi_history.json`` ที่ถูกสร้างไว้แล้ว

    python scripts/plot_cusi.py
    python scripts/plot_cusi.py --out chart.png --annotate 2024-08-05
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib

matplotlib.use("Agg")  # headless — ไม่ต้องมีหน้าจอ
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from pipeline.config import get_logger, load_config, resolve_path, setup_logging  # noqa: E402

log = get_logger(__name__)

#: เหตุการณ์อ้างอิงตาม spec §3.4 (ใช้ตรวจว่า CUSI จับได้ไหม)
DEFAULT_EVENTS: Dict[str, str] = {
    "2022-12-20": "BOJ YCC tweak",
    "2024-08-05": "Yen carry unwind",
}


def load_history(cfg: Dict[str, Any]) -> pd.DataFrame:
    """อ่าน cusi_history.json เป็น DataFrame.

    Args:
        cfg: config dict

    Returns:
        DataFrame index = date, คอลัมน์ ``value``, ``level``

    Raises:
        FileNotFoundError: ถ้ายังไม่เคยรัน pipeline
    """
    path = resolve_path(cfg, "cusi_history_json")
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found — run: python pipeline/run_all.py --mode daily")

    payload = json.loads(path.read_text(encoding="utf-8"))
    df = pd.DataFrame(payload["history"])
    if df.empty:
        raise ValueError("cusi_history.json มี history ว่าง")
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


def plot(
    df: pd.DataFrame,
    cfg: Dict[str, Any],
    out_path: Path,
    events: Optional[Dict[str, str]] = None,
) -> Path:
    """วาดกราฟ CUSI พร้อมแถบสี 4 ระดับและ annotation เหตุการณ์.

    Args:
        df: DataFrame จาก :func:`load_history`
        cfg: config dict
        out_path: ไฟล์ PNG ปลายทาง
        events: mapping ``"YYYY-MM-DD" -> ชื่อเหตุการณ์``

    Returns:
        พาธไฟล์ที่บันทึกแล้ว
    """
    events = DEFAULT_EVENTS if events is None else events
    levels: List[Dict[str, Any]] = cfg["cusi"]["levels"]

    fig, ax = plt.subplots(figsize=(14, 6.5))
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")

    # ---- แถบสีพื้นหลังตามระดับ §3.3
    lower = 0.0
    for level in levels:
        upper = min(float(level["max"]), 100.0)
        ax.axhspan(lower, upper, color=level.get("color", "#888"), alpha=0.13, zorder=0)
        # ไม่ใส่ emoji/ข้อความไทยในกราฟ — ฟอนต์ default ของ matplotlib ไม่มี glyph
        # จะกลายเป็นกล่องสี่เหลี่ยม (tofu) ใช้ ASCII อย่างเดียว
        ax.text(
            df.index[0], (lower + upper) / 2, f"  {level['name']}",
            va="center", ha="left", fontsize=9, color=level.get("color", "#888"), alpha=0.85, zorder=1,
        )
        lower = upper

    # ---- เส้น CUSI
    ax.plot(df.index, df["value"], color="#58a6ff", linewidth=1.6, zorder=3, label="CUSI")

    # ---- annotation เหตุการณ์
    for date_str, label in events.items():
        ts = pd.Timestamp(date_str)
        if not (df.index[0] <= ts <= df.index[-1]):
            continue
        nearest = df.index[df.index.get_indexer([ts], method="nearest")[0]]
        value = float(df.loc[nearest, "value"])
        ax.axvline(nearest, color="#f85149", linestyle="--", linewidth=1.1, alpha=0.8, zorder=2)
        ax.annotate(
            f"{label}\n{nearest.date()} · {value:.0f}",
            xy=(nearest, value),
            xytext=(12, 22),
            textcoords="offset points",
            fontsize=9,
            color="#f0f6fc",
            bbox=dict(boxstyle="round,pad=0.4", fc="#161b22", ec="#f85149", alpha=0.95),
            arrowprops=dict(arrowstyle="->", color="#f85149", lw=1.1),
            zorder=5,
        )

    last = df.iloc[-1]
    ax.scatter([df.index[-1]], [last["value"]], s=55, color="#58a6ff", zorder=6, edgecolor="#0d1117")

    ax.set_ylim(0, 100)
    ax.set_xlim(df.index[0], df.index[-1])
    ax.set_title(
        f"CUSI - Carry Unwind Stress Index  |  latest {last['value']:.1f} ({last['level']})  "
        f"|  {df.index[0].date()} to {df.index[-1].date()}",
        color="#f0f6fc", fontsize=13, pad=14,
    )
    ax.set_ylabel("CUSI (0–100)", color="#8b949e")
    ax.grid(True, alpha=0.13, color="#8b949e", linestyle=":")
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    for spine in ax.spines.values():
        spine.set_color("#30363d")
    ax.tick_params(colors="#8b949e", labelsize=9)
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130, facecolor=fig.get_facecolor())
    plt.close(fig)

    log.info("saved chart → %s", out_path)
    return out_path


def main(argv: Optional[list] = None) -> int:
    """Entry point ของ CLI.

    Args:
        argv: argument list; ``None`` = ใช้ ``sys.argv``

    Returns:
        exit code
    """
    parser = argparse.ArgumentParser(description="วาดกราฟ CUSI ย้อนหลัง")
    parser.add_argument("--out", default="data/cusi_chart.png", help="ไฟล์ PNG ปลายทาง")
    parser.add_argument("--annotate", nargs="*", default=None, help="วันที่เพิ่มเติม YYYY-MM-DD")
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg)

    try:
        df = load_history(cfg)
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        return 1

    events = dict(DEFAULT_EVENTS)
    for date_str in args.annotate or []:
        events.setdefault(date_str, "event")

    out = plot(df, cfg, Path(args.out), events)

    valid = df["value"]
    log.info(
        "CUSI summary: n=%d | last=%.1f (%s) | min=%.1f | max=%.1f (%s) | mean=%.1f",
        len(valid), valid.iloc[-1], df["level"].iloc[-1],
        valid.min(), valid.max(), valid.idxmax().date(), valid.mean(),
    )
    log.info("level distribution: %s", df["level"].value_counts().to_dict())
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
