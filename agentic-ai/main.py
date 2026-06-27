"""
main.py
-------
Application entry point.

Starts:
  1. FastAPI server (serves dashboard + API)  — runs in main thread via uvicorn
  2. Email polling loop                       — runs in a background daemon thread

Railway notes:
  - Railway injects PORT; config.py resolves it via: int(os.getenv("PORT", ...))
  - Google credential files are reconstructed from env vars by config.py at import
  - SQLite path supports absolute paths for Railway persistent volume (/data/...)
"""

import os
import threading
import logging
from pathlib import Path
import uvicorn

# ── Always run from the agentic-ai directory ──────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)

# ── Logging must be configured before any other import that uses logging ──────
from src.logging_config import setup_logging
setup_logging()

from src.database.db import init_db
from src.poller import run_poller
from src.api.server import app
from src.config import API_HOST, API_PORT

logger = logging.getLogger(__name__)


def main():
    init_db()
    logger.info("Database ready")

    poller_thread = threading.Thread(target=run_poller, daemon=True, name="email-poller")
    poller_thread.start()
    logger.info("Email poller started in background thread")

    logger.info(f"Starting FastAPI server at http://{API_HOST}:{API_PORT}")
    uvicorn.run(
        app,
        host=API_HOST,
        port=API_PORT,
        log_level="info",
        log_config=None,
    )


if __name__ == "__main__":
    main()
