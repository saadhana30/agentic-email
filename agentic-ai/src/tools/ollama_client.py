"""
ollama_client.py
----------------
Direct HTTP client for Ollama API with exponential backoff retry.

Retry policy:
  - Max 3 attempts (configurable via OLLAMA_MAX_RETRIES)
  - Delays: 1s, 2s, 4s  (base doubles each time)
  - Retries on: timeout, connection error, malformed JSON, HTTP errors
  - If all retries fail: raises OllamaRetryExhausted so callers can
    send the email to review_queue without crashing the graph.
"""

import json
import logging
import time
import requests
from src.config import (
    OLLAMA_BASE_URL, OLLAMA_MODEL,
    OLLAMA_MAX_RETRIES, OLLAMA_RETRY_BASE_DELAY,
)

logger = logging.getLogger(__name__)


class OllamaRetryExhausted(RuntimeError):
    """Raised when all Ollama retry attempts are exhausted."""


def call_ollama(prompt: str, temperature: float = 0.1) -> str:
    """
    Send a prompt to Ollama and return the response text.

    Wraps the call with exponential backoff retry logic.
    Raises OllamaRetryExhausted if all attempts fail.
    """
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }

    last_exception: Exception | None = None
    delay = OLLAMA_RETRY_BASE_DELAY

    for attempt in range(1, OLLAMA_MAX_RETRIES + 1):
        try:
            logger.debug(f"OllamaClient: attempt {attempt}/{OLLAMA_MAX_RETRIES}")
            response = requests.post(url, json=payload, timeout=120)
            response.raise_for_status()

            data = response.json()
            text = data.get("response", "").strip()

            if not text:
                raise ValueError("Ollama returned an empty response")

            if attempt > 1:
                logger.info(f"OllamaClient: succeeded on attempt {attempt}")

            return text

        except requests.exceptions.ConnectionError as e:
            last_exception = e
            logger.warning(
                f"OllamaClient: connection error on attempt {attempt} — "
                "is Ollama running? ('ollama serve')"
            )

        except requests.exceptions.Timeout as e:
            last_exception = e
            logger.warning(
                f"OllamaClient: timeout on attempt {attempt} — "
                "model may still be loading"
            )

        except (requests.exceptions.HTTPError, ValueError, json.JSONDecodeError) as e:
            last_exception = e
            logger.warning(f"OllamaClient: bad response on attempt {attempt} — {e}")

        except Exception as e:
            last_exception = e
            logger.warning(f"OllamaClient: unexpected error on attempt {attempt} — {e}")

        # Don't sleep after the last attempt
        if attempt < OLLAMA_MAX_RETRIES:
            logger.info(f"OllamaClient: retrying in {delay}s...")
            time.sleep(delay)
            delay *= 2   # exponential backoff: 1 → 2 → 4

    error_msg = f"Ollama call failed after {OLLAMA_MAX_RETRIES} attempts: {last_exception}"
    logger.error(error_msg)
    raise OllamaRetryExhausted(error_msg)
