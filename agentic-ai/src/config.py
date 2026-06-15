import os
from pathlib import Path
from dotenv import load_dotenv

# Resolve project root (where .env lives) regardless of working directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # src/config.py → agentic-ai/
load_dotenv(PROJECT_ROOT / ".env")

# Google
GOOGLE_CREDENTIALS_FILE = str(PROJECT_ROOT / os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"))
GOOGLE_TOKEN_FILE = str(PROJECT_ROOT / os.getenv("GOOGLE_TOKEN_FILE", "token.json"))
COMPANY_EMAIL = os.getenv("COMPANY_EMAIL", "project.agent.demo@gmail.com")

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]

# Jira
JIRA_URL = os.getenv("JIRA_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

# Ollama
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:latest")

# App settings
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.75"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
DATABASE_URL = "sqlite:///" + str(PROJECT_ROOT / os.getenv("DATABASE_URL", "agentic_ai.db").replace("sqlite:///", ""))
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))

# Retry settings
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 3

# Timezone — set to your local timezone for calendar events
# Examples: "Asia/Kolkata", "America/New_York", "Europe/London", "UTC"
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "Asia/Kolkata")
