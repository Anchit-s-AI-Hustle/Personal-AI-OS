"""
LLM integration.

`get_llm_client()` is a factory: it returns the Gemini, Groq, or Ollama
client based on the `LLM_PROVIDER` env var (defaults to `gemini` for
backward compatibility). All callers should use `get_llm_client()` —
never reach into the provider-specific modules directly.

`QuotaExhaustedError` is the shared signal that the configured key has
no usable quota right now; it lives in `ai.errors` so importers don't
care which provider is active. Ollama is local and never raises this.
"""
from __future__ import annotations

import threading

from config import settings

from .errors import QuotaExhaustedError
from .extractor import Extractor, get_extractor

_lock = threading.Lock()
_cached_client = None


def _build_single_client(provider: str):
    if provider == "groq":
        from .groq_client import get_groq_client
        return get_groq_client()
    if provider == "gemini":
        from .gemini_client import get_gemini_client
        return get_gemini_client()
    if provider == "ollama":
        from .ollama_client import get_ollama_client
        return get_ollama_client()
    raise RuntimeError(
        f"Unknown LLM provider {provider!r}. "
        f"Use 'gemini', 'groq', or 'ollama'."
    )


def get_llm_client():
    """
    Return the configured LLM client.

    `LLM_PROVIDER` may be:
      - a single provider name           e.g. "gemini"
      - a comma-separated priority chain e.g. "gemini,groq,ollama"

    For a chain, returns a `RoutedClient` that tries providers
    left-to-right, automatically falls back on quota exhaustion, and
    automatically recovers when the higher-priority provider's pause
    window elapses.
    """
    global _cached_client
    if _cached_client is not None:
        return _cached_client

    with _lock:
        if _cached_client is not None:
            return _cached_client

        raw = (settings.llm_provider or "gemini").strip().lower()
        names = [p.strip() for p in raw.split(",") if p.strip()]

        if len(names) == 1:
            _cached_client = _build_single_client(names[0])
        else:
            from .routed_client import RoutedClient
            chain = [(name, _build_single_client(name)) for name in names]
            _cached_client = RoutedClient(chain)

        return _cached_client


__all__ = [
    "Extractor",
    "QuotaExhaustedError",
    "get_extractor",
    "get_llm_client",
]
