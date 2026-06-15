"""
ollama_client.py
----------------
Direct HTTP client for Ollama API.
Replaces langchain-ollama + langchain-core entirely.
No LangChain dependency needed just to call a local LLM.
"""

import json
import logging
import requests
from src.config import OLLAMA_BASE_URL, OLLAMA_MODEL

logger = logging.getLogger(__name__)


def call_ollama(prompt: str, temperature: float = 0.1) -> str:
    """
    Send a prompt to Ollama and return the response text.
    Uses /api/generate endpoint — simple, no LangChain needed.
    """
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
        }
    }
    try:
        response = requests.post(url, json=payload, timeout=120)
        response.raise_for_status()
        return response.json().get("response", "").strip()
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            "Cannot connect to Ollama. Make sure Ollama is running: 'ollama serve'"
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("Ollama request timed out. Model may still be loading.")
    except Exception as e:
        raise RuntimeError(f"Ollama call failed: {e}")
