"""
logging_config.py
-----------------
Centralised logging setup.
- Rotating file handler for all logs  → logs/app.log
- Rotating file handler for errors    → logs/errors.log
- StreamHandler for stdout (preserved)
Call setup_logging() once from main.py before anything else.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from datetime import datetime
from zoneinfo import ZoneInfo
from src.config import LOGS_DIR

APP_LOG  = LOGS_DIR / "app.log"
ERR_LOG  = LOGS_DIR / "errors.log"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S %Z"


class TZFormatter(logging.Formatter):
    """Logging formatter that renders timestamps in a specific timezone."""
    def __init__(self, fmt=None, datefmt=None, tz: ZoneInfo = ZoneInfo("Asia/Kolkata")):
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.tz = tz

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=self.tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()

# 10 MB per file, keep 5 backups
MAX_BYTES  = 10 * 1024 * 1024
BACKUP_COUNT = 5


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger with rotating file + stdout handlers."""
    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers if called more than once (e.g. during tests)
    if root.handlers:
        return

    formatter = TZFormatter(LOG_FORMAT, datefmt=DATE_FORMAT, tz=ZoneInfo("Asia/Kolkata"))

    # ── stdout ────────────────────────────────────────────────────────────────
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.setFormatter(formatter)

    # ── logs/app.log — INFO and above ─────────────────────────────────────────
    app_handler = RotatingFileHandler(
        APP_LOG, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    app_handler.setLevel(logging.INFO)
    app_handler.setFormatter(formatter)

    # ── logs/errors.log — WARNING and above ───────────────────────────────────
    err_handler = RotatingFileHandler(
        ERR_LOG, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(formatter)

    root.addHandler(stdout_handler)
    root.addHandler(app_handler)
    root.addHandler(err_handler)
