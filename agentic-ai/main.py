"""
main.py
-------
Application entry point.
Starts:
  1. FastAPI server (serves dashboard + API)
  2. Email polling loop in a background thread
"""

import os
import threading
import logging
from pathlib import Path
import uvicorn

# ── Always run from the agentic-ai directory ─────────────────────────────────
# This fixes the issue when VS Code runs main.py from a parent folder
PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)

from src.database.db import init_db
from src.poller import run_poller
from src.api.server import app
from src.config import API_HOST, API_PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    # Initialize DB tables
    init_db()
    logger.info("Database ready")

    # Start polling loop in background thread
    poller_thread = threading.Thread(target=run_poller, daemon=True)
    poller_thread.start()
    logger.info("Email poller started in background")

    # Start FastAPI
    logger.info(f"Starting server at http://{API_HOST}:{API_PORT}")
    uvicorn.run(app, host=API_HOST, port=API_PORT, log_level="info")


if __name__ == "__main__":
    main()
