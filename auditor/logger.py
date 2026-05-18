"""Shared logging configuration for auditor-mvp.

All modules import `get_logger(__name__)` — they do NOT call basicConfig themselves.
Bootstrap is done once in api.py / run.py entrypoints via `setup_logging()`.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(log_file: str | Path | None = "auditor.log", level: int = logging.DEBUG) -> None:
    """Call once at process start (entrypoint).  Safe to call multiple times."""
    root = logging.getLogger("auditor")
    if root.handlers:
        return  # already configured

    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # ── stderr handler (INFO+) ────────────────────────────────────────────────
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # ── file handler (DEBUG+) — captures every detail ────────────────────────
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the 'auditor' hierarchy.

    Usage::
        from auditor.logger import get_logger
        log = get_logger(__name__)
        log.info("starting")
    """
    # Strip leading 'auditor.' so the path stays tidy in log output
    clean = name.removeprefix("auditor.") if name.startswith("auditor.") else name
    return logging.getLogger(f"auditor.{clean}")
