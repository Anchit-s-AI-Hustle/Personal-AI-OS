"""
Logging configuration.

Logs to both stdout and a daily-rotating file in `logs/`.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

from config import settings

_CONFIGURED = False
_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: Optional[str] = None) -> None:
    """Configure root logger. Safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = (level or settings.log_level).upper()
    numeric_level = getattr(logging, log_level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    # Wipe any defaults set by libraries.
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FMT)

    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(numeric_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Rotating file
    log_path: Path = settings.logs_dir / "personal_ai_os.log"
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=14, encoding="utf-8"
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Tame chatty deps.
    for noisy in (
        "googleapiclient.discovery_cache",
        "googleapiclient.discovery",
        "urllib3",
        "httpx",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # huggingface_hub re-emits warnings via the logging system too — bump it
    # one level higher so the auth nag and symlink advisory disappear from
    # our log file. Real errors (download failure, 4xx/5xx) still come through.
    for hf_logger in (
        "huggingface_hub",
        "huggingface_hub.utils._http",
        "huggingface_hub.file_download",
    ):
        logging.getLogger(hf_logger).setLevel(logging.ERROR)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)
