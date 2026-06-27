"""
config.py
---------
Central configuration.  Reads from environment variables (Railway / .env locally).

Google credential files are reconstructed from env vars at import time so
Railway never needs a browser OAuth flow.  Local dev continues to work from
the files on disk exactly as before.
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

# ── Project root ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent   # src/config.py → agentic-ai/
load_dotenv(PROJECT_ROOT / ".env")


# ── Google credential file reconstruction ────────────────────────────────────
# On Railway: set GOOGLE_CREDENTIALS_JSON and GOOGLE_TOKEN_JSON env vars
# containing the full JSON content of each file.
# Locally: the files already exist on disk — no reconstruction needed.

def _reconstruct_google_file(env_var: str, default_filename: str) -> str:
    """
    If the env var contains JSON, write it to /tmp and return that path.
    Otherwise return the local project-root path (works for local dev).
    """
    json_content = os.getenv(env_var, "")
    if json_content.strip().startswith("{"):
        # Running on Railway (or any env where file content is injected as env var)
        tmp_path = f"/tmp/{default_filename}"
        try:
            with open(tmp_path, "w") as f:
                f.write(json_content)
        except Exception:
            pass  # write failure will surface later as a clear auth error
        return tmp_path
    # Local dev — use the file in the project root
    custom_path = os.getenv(
        "GOOGLE_CREDENTIALS_FILE" if "CREDENTIALS" in env_var else "GOOGLE_TOKEN_FILE",
        default_filename,
    )
    return str(PROJECT_ROOT / custom_path)


GOOGLE_CREDENTIALS_FILE = _reconstruct_google_file(
    "GOOGLE_CREDENTIALS_JSON", "credentials.json"
)
GOOGLE_TOKEN_FILE = _reconstruct_google_file(
    "GOOGLE_TOKEN_JSON", "token.json"
)

COMPANY_EMAIL = os.getenv("COMPANY_EMAIL", "project.agent.demo@gmail.com")

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]

# ── Jira ──────────────────────────────────────────────────────────────────────
JIRA_URL        = os.getenv("JIRA_URL")
JIRA_EMAIL      = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN  = os.getenv("JIRA_API_TOKEN")

# ── LLM backend ───────────────────────────────────────────────────────────────
# Groq (production / Railway) — set GROQ_API_KEY to activate
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

# Ollama (local fallback — used when GROQ_API_KEY is not set)
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2:latest")

# ── App settings ──────────────────────────────────────────────────────────────
CONFIDENCE_THRESHOLD  = float(os.getenv("CONFIDENCE_THRESHOLD", "0.75"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# DATABASE_URL
# Local default  → sqlite:///agentic_ai.db  (resolved relative to project root)
# Railway        → set DATABASE_URL=/data/agentic_ai.db  (persistent volume)
_raw_db = os.getenv("DATABASE_URL", "agentic_ai.db").replace("sqlite:///", "")
if _raw_db.startswith("/"):
    # Absolute path supplied (Railway persistent volume e.g. /data/agentic_ai.db)
    DATABASE_URL = f"sqlite:///{_raw_db}"
else:
    # Relative path — resolve against project root (local dev)
    DATABASE_URL = "sqlite:///" + str(PROJECT_ROOT / _raw_db)

# PORT — Railway injects $PORT; fall back to API_PORT for local dev
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("PORT", os.getenv("API_PORT", "8000")))

# ── Retry settings ────────────────────────────────────────────────────────────
MAX_RETRIES        = 2
RETRY_DELAY_SECONDS = 3

# LLM retry — exponential backoff: 1 s → 2 s → 4 s
OLLAMA_MAX_RETRIES    = 3
OLLAMA_RETRY_BASE_DELAY = 1

# ── Timezone ──────────────────────────────────────────────────────────────────
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "Asia/Kolkata")

# ── Logs directory ────────────────────────────────────────────────────────────
# On Railway the project root may be read-only; fall back to /tmp/logs
_logs_candidate = PROJECT_ROOT / "logs"
try:
    _logs_candidate.mkdir(exist_ok=True)
    LOGS_DIR = _logs_candidate
except OSError:
    LOGS_DIR = Path("/tmp/logs")
    LOGS_DIR.mkdir(exist_ok=True)

# ── JWT Auth ──────────────────────────────────────────────────────────────────
JWT_SECRET_KEY                = os.getenv("JWT_SECRET_KEY", "change-this-in-production")
JWT_ALGORITHM                 = "HS256"
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
JWT_REFRESH_TOKEN_EXPIRE_DAYS   = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "7"))
