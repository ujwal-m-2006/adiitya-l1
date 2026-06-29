#!/usr/bin/env python3
"""
pipeline_utils.py
=================
Shared utilities for the Aditya-L1 SFF Pipeline.
All scripts import from here.
"""

import json
import logging
import os
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from dotenv import load_dotenv

# ── Paths ──────────────────────────────────────────────────────
PIPELINE_ROOT = Path(__file__).parent
CONFIG_FILE   = PIPELINE_ROOT / "config.yaml"
ENV_FILE      = PIPELINE_ROOT / ".env"
STATE_FILE    = PIPELINE_ROOT / "data" / ".pipeline_state.json"
LOG_DIR       = PIPELINE_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


# Auto-load .env on import so all modules get credentials
load_dotenv(ENV_FILE)


def load_config() -> dict:
    with open(CONFIG_FILE, "r") as f:
        raw = yaml.safe_load(f)

    # Expand ${ENV_VAR} placeholders
    def expand(obj):
        if isinstance(obj, str):
            import re
            return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), m.group(0)), obj)
        if isinstance(obj, dict):
            return {k: expand(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [expand(i) for i in obj]
        return obj

    return expand(raw)


def setup_logger(name: str, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not logger.handlers:
        # Console handler — force utf-8 on Windows
        import io
        ch = logging.StreamHandler(
            stream=io.TextIOWrapper(
                __import__('sys').stdout.buffer,
                encoding='utf-8', errors='replace'
            )
        )
        ch.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(ch)

        # File handler — rotate daily
        log_file = LOG_DIR / f"{name}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        logger.addHandler(fh)

    return logger


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_json(path: Path) -> Any:
    with open(path, "r") as f:
        return json.load(f)


class PipelineState:
    """Lightweight persistent state across cron runs."""

    @staticmethod
    def load() -> dict:
        if STATE_FILE.exists():
            try:
                return load_json(STATE_FILE)
            except Exception:
                pass
        return {}

    @staticmethod
    def save(state: dict) -> None:
        save_json(state, STATE_FILE)


def classify_flux(flux: float) -> tuple[str, float]:
    """
    Classify GOES/SoLEXS 1–8 Å flux into GOES flare class.
    Returns (class_letter, multiplier) e.g. ("M", 3.2) for M3.2
    """
    thresholds = [
        (1e-4, "X"),
        (1e-5, "M"),
        (1e-6, "C"),
        (1e-7, "B"),
    ]
    for thresh, cls in thresholds:
        if flux >= thresh:
            val = round(flux / thresh, 1)
            return cls, val
    return "A", round(flux / 1e-8, 1)


def geo_storm_label(kp: float) -> str:
    if kp >= 8:  return "SEVERE (G4-G5)"
    if kp >= 6:  return "HIGH (G3)"
    if kp >= 5:  return "MODERATE (G2)"
    if kp >= 4:  return "LOW (G1)"
    return "QUIET"
