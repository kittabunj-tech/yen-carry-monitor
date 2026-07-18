"""Config loading, path resolution and logging setup for the pipeline.

ทุกโมดูลใน pipeline/ ต้องดึงค่าพารามิเตอร์ผ่านที่นี่เท่านั้น
(ห้าม hardcode ค่าที่อยู่ใน config.yaml)
"""

from __future__ import annotations

import logging
import logging.config
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

#: repo root = โฟลเดอร์แม่ของ pipeline/
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH: Path = Path(__file__).resolve().parent / "config.yaml"


@lru_cache(maxsize=4)
def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load and cache ``config.yaml``.

    Args:
        path: พาธไปยังไฟล์ config; ``None`` = ใช้ ``pipeline/config.yaml``

    Returns:
        dict ของ config ทั้งไฟล์

    Raises:
        FileNotFoundError: ถ้าไฟล์ config ไม่มีอยู่จริง
        ValueError: ถ้า YAML ไม่ใช่ mapping
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.is_file():
        raise FileNotFoundError(f"config not found: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    if not isinstance(cfg, dict):
        raise ValueError(f"config must be a YAML mapping, got {type(cfg).__name__}")
    return cfg


def resolve_path(cfg: Dict[str, Any], key: str, *, mkdir: bool = False) -> Path:
    """แปลง path จาก ``config['paths'][key]`` (relative กับ repo root) เป็น absolute.

    Args:
        cfg: dict จาก :func:`load_config`
        key: ชื่อ key ใน section ``paths``
        mkdir: ถ้า True สร้างโฟลเดอร์แม่ให้ด้วย

    Returns:
        absolute :class:`~pathlib.Path`

    Raises:
        KeyError: ถ้าไม่มี key นี้ใน config
    """
    rel = cfg["paths"][key]
    out = (REPO_ROOT / rel).resolve()
    if mkdir:
        parent = out if out.suffix == "" else out.parent
        parent.mkdir(parents=True, exist_ok=True)
    return out


def setup_logging(cfg: Optional[Dict[str, Any]] = None, level: Optional[str] = None) -> None:
    """ตั้งค่า root logger ตาม section ``logging`` ของ config.

    เรียกซ้ำได้อย่างปลอดภัย (idempotent) — ใช้ ``force=True``

    Args:
        cfg: dict จาก :func:`load_config`; ``None`` = โหลดเอง
        level: override log level (เช่น ``"DEBUG"`` จาก CLI flag)
    """
    cfg = cfg if cfg is not None else load_config()
    log_cfg = cfg.get("logging", {})
    logging.basicConfig(
        level=getattr(logging, (level or log_cfg.get("level", "INFO")).upper(), logging.INFO),
        format=log_cfg.get("format", "%(asctime)s | %(levelname)s | %(name)s | %(message)s"),
        datefmt=log_cfg.get("datefmt", "%Y-%m-%d %H:%M:%S"),
        force=True,
    )
    # yfinance/urllib3 คุยเยอะเกินไปที่ระดับ INFO
    for noisy in ("yfinance", "urllib3", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """คืน logger ที่ชื่อสั้นลง (ตัด prefix ``pipeline.``) เพื่อให้ log อ่านง่าย."""
    return logging.getLogger(name.replace("pipeline.", "").replace("__main__", "main"))
