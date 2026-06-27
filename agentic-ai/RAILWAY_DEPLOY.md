# Railway Deployment Checklist

## What was changed automatically

| File | What changed |
|---|---|
| `src/tools/ollama_client.py` | Replaced Ollama HTTP call with Groq SDK. Public API (`call_ollama`, `OllamaRetryExhausted`) unchanged. Auto-selects Groq when `GROQ_API_KEY` is set, falls back to Ollama locally. |
| `src/config.py` | Added `GROQ_API_KEY`/`GROQ_MODEL`. Added Google credential reconstruction from env vars. Fixed `DATABASE_URL` to support absolute paths. Fixed port binding (`PORT` → `API_PORT`). |
| `requirements.txt` | Added `groq==0.9.0`. |
| `main.py` | No logic change — already correct. |
| `Procfile` | Created. Railway start command. |
| `railway.toml` | Created. Build/deploy config with health check. |
| `nixpacks.toml` | Created. Installs Python 3.11 + tesseract binary. |

---

## Step 1 — Prepare credentials (do this BEFORE deploying)

Run these commands locally in your terminal to get the values for Railway env vars:

```bash
# From the agentic-ai/ directory:
python -c "import json; print(json.dumps(json.load(open('credentials.json'))))"
python -c "import json; print(json.dumps(json.load(open('token.json'))))"
```

Copy each output — you'll paste them as env var values in Step 4.

---

## Step 2 — Create Railway project

1. Go to https://railway.app → New Project → Deploy from GitHub repo
2. Select your repository
3. Railway will detect `nixpacks.toml` and build automatically

---

## Step 3 — Attach persistent volume for SQLite

1. In your Railway service → **Volumes** tab → **Add Volume**
2. Mount path: `/data`
3. This keeps the database alive across redeploys

---

## Step 4 — Set environment variables

In Railway → your service → **Variables** tab, add ALL of these:

### Required (project will not start without these)

| Variable | Value |
|---|---|
| `GROQ_API_KEY` | Your Groq API key from https://console.groq.com |
| `GOOGLE_CREDENTIALS_JSON` | Full JSON content of `credentials.json` (from Step 1) |
| `GOOGLE_TOKEN_JSON` | Full JSON content of `token.json` (from Step 1) |
| `COMPANY_EMAIL` | The Gmail address the system monitors |
| `JWT_SECRET_KEY` | A long random string (min 32 chars) — generate with `python -c "import secrets; print(secrets.token_hex(32))"` |

### Required for Jira integration

| Variable | Value |
|---|---|
| `JIRA_URL` | e.g. `https://yourcompany.atlassian.net` |
| `JIRA_EMAIL` | Jira account email |
| `JIRA_API_TOKEN` | Jira API token from https://id.atlassian.com/manage-profile/security |

### Required for SQLite persistent volume

| Variable | Value |
|---|---|
| `DATABASE_URL` | `/data/agentic_ai.db` |

### Optional (have good defaults)

| Variable | Default | Notes |
|---|---|---|
| `GROQ_MODEL` | `llama3-8b-8192` | Or `llama-3.1-70b-versatile` for better quality |
| `CONFIDENCE_THRESHOLD` | `0.75` | LLM confidence threshold for auto-execution |
| `POLL_INTERVAL_SECONDS` | `60` | How often to check Gmail (seconds) |
| `CALENDAR_TIMEZONE` | `Asia/Kolkata` | Your local timezone |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | Dashboard session length |

> **Do NOT set** `PORT` — Railway injects it automatically.

---

## Step 5 — Deploy

Click **Deploy** in Railway.  
Watch the build logs — the first deploy takes ~2 minutes for pip install.

---

## Step 6 — Verify

After deploy succeeds:

1. Open your Railway service URL
2. You should see the login page (`/login`)
3. Login with default credentials: `admin` / `admin123`
4. **Change the password immediately** via the admin panel
5. Add your first client in the Clients tab
6. Send a test email from that client's domain
7. Watch the Live Monitor tab for processing events

---

## Local development (unchanged)

Nothing changes locally.  
As long as `GROQ_API_KEY` is NOT in your local `.env`, the system uses Ollama.  
If you want to test Groq locally, add `GROQ_API_KEY=gsk_...` to `.env`.

```bash
# Still works exactly as before:
python main.py
```

---

## Token refresh note

`token.json` contains a refresh token that auto-renews the access token.  
The renewed token is written back to disk (`/tmp/token.json` on Railway).  
On container restart, `GOOGLE_TOKEN_JSON` is re-read from the env var and  
the file is reconstructed — so a restart does NOT break Gmail access.

If you revoke the token or the refresh token expires (after 6 months idle),  
you must re-run the OAuth flow locally and update `GOOGLE_TOKEN_JSON` in Railway.
