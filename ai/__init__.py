"""
LLM integration.

`get_llm_client()` is a factory: it returns the Gemini or Groq client
based on the `LLM_PROVIDER` env var (defaults to `gemini` for backward
compatibility). All callers should use `get_llm_client()` — never reach
into the provider-specific modules directly.

`QuotaExhaustedError` is the shared signal that the configured key has
no usable quota right now; it lives in `ai.errors` so importers don't
care which provider is active.
"""
from __future__ import annotations

import threading
from typing import Optional

from config import settings

from .errors import QuotaExhaustedError
from .extractor import Extractor, get_extractor

_lock = threading.Lock()
_cached_client = None


def get_llm_client():
    """Return the configured LLM client (Gemini by default, or Groq)."""
    global _cached_client
    if _cached_client is not None:
        return _cached_client

    with _lock:
        if _cached_client is not None:
            return _cached_client

        provider = (settings.llm_provider or "gemini").strip().lower()
        if provider == "groq":
            from .groq_client import get_groq_client
            _cached_client = get_groq_client()
        elif provider == "gemini":
            from .gemini_client import get_gemini_client
            _cached_client = get_gemini_client()
        else:
            raise RuntimeError(
                f"Unknown LLM_PROVIDER={provider!r}. Use 'gemini' or 'groq'."
            )
        return _cached_client


__all__ = [
    "Extractor",
    "QuotaExhaustedError",
    "get_extractor",
    "get_llm_client",
]
