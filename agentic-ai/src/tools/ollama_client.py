"""
ollama_client.py
----------------
LLM client — backed by Groq in production, Ollama locally if GROQ_API_KEY is absent.

Public API is UNCHANGED — all agents call:
    from src.tools.ollama_client import call_ollama, OllamaRetryExhausted

Retry policy (identical to previous Ollama implementation):
  - Max LLM_MAX_RETRIES attempts (default 3)
  - Exponential delays: 1 s → 2 s → 4 s
  - Retries on: timeout, connection error, HTTP error, empty/malformed response
  - Raises OllamaRetryExhausted on final failure so callers route to review_queue

Provider selection (automatic, no code changes required):
  - GROQ_API_KEY is set  →  use Groq  (production / Railway)
  - GROQ_API_KEY is absent →  use Ollama  (local development)
"""

import json
import logging
import time

logger = logging.getLogger(__name__)

# ── Exception (name kept for backwards compatibility with all agent imports) ──
class OllamaRetryExhausted(RuntimeError):
    """Raised when all LLM retry attempts are exhausted."""


# ── Provider-level call functions ─────────────────────────────────────────────

def _call_groq(prompt: str, temperature: float, model: str, api_key: str) -> str:
    """
    Call Groq chat completions endpoint.
    Returns the assistant message text.
    Raises requests.HTTPError, Timeout, ConnectionError, or ValueError on failure.
    """
    import requests  # imported here so the module loads even if requests is absent

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "stream": False,
    }
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    text = data["choices"][0]["message"]["content"].strip()
    if not text:
        raise ValueError("Groq returned an empty response")
    return text


def _call_ollama(prompt: str, temperature: float, base_url: str, model: str) -> str:
    """
    Call local Ollama /api/generate endpoint.
    Returns the response text.
    Raises on any failure.
    """
    import requests

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }
    response = requests.post(
        f"{base_url}/api/generate",
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    text = data.get("response", "").strip()
    if not text:
        raise ValueError("Ollama returned an empty response")
    return text


# ── Public entry point ────────────────────────────────────────────────────────

def call_ollama(prompt: str, temperature: float = 0.1) -> str:
    """
    Send a prompt to the configured LLM backend and return the response text.

    Automatically chooses Groq (if GROQ_API_KEY is set) or Ollama (local dev).
    Wraps the call with exponential backoff retry.
    Raises OllamaRetryExhausted if all attempts fail.
    """
    # Import config lazily to avoid circular imports at module load
    from src.config import (
        GROQ_API_KEY, GROQ_MODEL,
        OLLAMA_BASE_URL, OLLAMA_MODEL,
        OLLAMA_MAX_RETRIES, OLLAMA_RETRY_BASE_DELAY,
    )

    use_groq = bool(GROQ_API_KEY)
    provider = "Groq" if use_groq else "Ollama"

    last_exception: Exception | None = None
    delay = OLLAMA_RETRY_BASE_DELAY

    for attempt in range(1, OLLAMA_MAX_RETRIES + 1):
        try:
            logger.debug(f"LLMClient ({provider}): attempt {attempt}/{OLLAMA_MAX_RETRIES}")

            if use_groq:
                text = _call_groq(prompt, temperature, GROQ_MODEL, GROQ_API_KEY)
            else:
                text = _call_ollama(prompt, temperature, OLLAMA_BASE_URL, OLLAMA_MODEL)

            if attempt > 1:
                logger.info(f"LLMClient ({provider}): succeeded on attempt {attempt}")
            return text

        except Exception as e:
            import requests as _req
            last_exception = e

            if isinstance(e, _req.exceptions.ConnectionError):
                hint = "is Groq reachable?" if use_groq else "is Ollama running? ('ollama serve')"
                logger.warning(f"LLMClient ({provider}): connection error on attempt {attempt} — {hint}")
            elif isinstance(e, _req.exceptions.Timeout):
                logger.warning(f"LLMClient ({provider}): timeout on attempt {attempt}")
            else:
                logger.warning(f"LLMClient ({provider}): error on attempt {attempt} — {e}")

        if attempt < OLLAMA_MAX_RETRIES:
            logger.info(f"LLMClient ({provider}): retrying in {delay}s…")
            time.sleep(delay)
            delay *= 2   # 1 → 2 → 4

    error_msg = f"LLM call failed after {OLLAMA_MAX_RETRIES} attempts: {last_exception}"
    logger.error(error_msg)
    raise OllamaRetryExhausted(error_msg)
