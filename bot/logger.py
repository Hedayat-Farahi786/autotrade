"""Structured, rotating logging used everywhere in the bot.

Two sinks are configured:

* a human-readable console/stream handler, and
* a rotating file handler under ``LOG_DIR`` capturing everything at DEBUG.

A dedicated ``audit`` logger records every received message, parsed intent and
MT5 action as one-line JSON so post-mortems and compliance reviews are trivial.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

_CONFIGURED = False


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    os.makedirs(log_dir, exist_ok=True)
    level = getattr(logging, log_level.upper(), logging.INFO)

    root = logging.getLogger("gtmo")
    root.setLevel(logging.DEBUG)
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(level)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    fileh = RotatingFileHandler(
        os.path.join(log_dir, "bot.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    fileh.setLevel(logging.DEBUG)
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    # Audit logger → newline-delimited JSON, never propagated to console.
    audit = logging.getLogger("gtmo.audit")
    audit.setLevel(logging.INFO)
    audit.propagate = False
    audit_handler = RotatingFileHandler(
        os.path.join(log_dir, "audit.jsonl"),
        maxBytes=20 * 1024 * 1024,
        backupCount=20,
        encoding="utf-8",
    )
    audit_handler.setLevel(logging.INFO)
    audit_handler.setFormatter(logging.Formatter("%(message)s"))
    audit.addHandler(audit_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"gtmo.{name}")


def audit(event: str, **fields: Any) -> None:
    """Emit a single structured audit record."""

    record: Dict[str, Any] = {"ts": round(time.time(), 3), "event": event}
    record.update(fields)
    logging.getLogger("gtmo.audit").info(json.dumps(record, default=str))
