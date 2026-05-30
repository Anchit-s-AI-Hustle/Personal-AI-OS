"""
Ollama LLM client — local, free, no quota.

Talks to a locally-running Ollama daemon over its HTTP API. Same
`complete(system, user, ...)` contract as the Gemini and Groq clients
so the extractor doesn't need to know which provider is active.

Setup (one-time):
  1. Install Ollama from https://ollama.com/download
  2. Pull a model:  ollama pull llama3.1:8b
  3. Make sure the Ollama daemon is running (the desktop app starts it
     automatically; on a server: `ollama serve` in a separate terminal)
  4. Set in .env:
        LLM_PROVIDER=ollama
        OLLAMA_MODEL=llama3.1:8b      # or qwen2.5:7b, mistral:7b, etc.
        OLLAMA_HOST=http://localhost:11434  # default, override if remote

Tradeoffs vs. cloud providers:
  - No quota, no API key, no internet needed once the model is pulled.
  - Latency: 3-10s per extraction on CPU (vs ~1s for Groq/Gemini).
    If you have a GPU, pass --num-gpu to ollama and it's much faster.
  - Output quality is model-dependent. llama3.1:8b is a reasonable
    default for this kind of structured-extraction task; bump to
    llama3.1:70b on a beefy machine for better judgment.
  - Format compliance: we ask for `format=json` in the request, which
    Ollama enforces server-side. Models still occasionally produce
    malformed JSON; the extractor handles parse failures gracefully.

Operational notes mirrored from gemini_client / groq_client:
  - Inter-call throttle: not needed (no rate limit on a local server).
  - Connection error -> retry with backoff (server might be starting).
  - 404 from /api/chat -> the model isn't pulled. We surface a clear
    "run `ollama pull <model>`" message instead of a stack trace.
"""
from __future__ import annotations

import threading
from typing import Optional

import requests

from config import settings
from utils.logger import get_logger
from utils.retry import retry_call

from .errors import QuotaExhaustedError

logger = get_logger(__name__)

# Long timeout: the first call after the daemon starts can take 30s+
# while Ollama loads the model into memory. Subsequent calls are fast.
_REQUEST_TIMEOUT_SECONDS = 180.0

_RETRYABLE: tuple[type[BaseException], ...] = (
    requests.ConnectionError,
    requests.Timeout,
    ConnectionError,
    TimeoutError,
)


class OllamaModelMissingError(RuntimeError):
    """The configured model isn't pulled on this Ollama daemon."""


class OllamaClient:
    def __init__(
        self,
        model: Optional[str] = None,
        host: Optional[str] = None,
    ) -> None:
        self.model = model or settings.ollama_model
        self.host = (host or settings.ollama_host or "http://localhost:11434").rstrip("/")
        self._session = requests.Session()
        self._lock = threading.Lock()
        # Disable any user-level proxy for localhost calls — common cause
        # of "Connection refused" with corporate proxy environment vars.
        if self.host.startswith(("http://localhost", "http://127.0.0.1")):
            self._session.trust_env = False
        logger.info("Ollama client initialised (model=%s, host=%s)", self.model, self.host)

    # --- public API ----------------------------------------------------------

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        url = f"{self.host}/api/chat"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            # Ollama's "json" format constrains the model to emit valid
            # JSON. The extractor's _parse_json_block also tolerates
            # plain JSON / fenced JSON, so we're robust either way.
            "format": "json",
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        def _call() -> str:
            try:
                resp = self._session.post(
                    url, json=payload, timeout=_REQUEST_TIMEOUT_SECONDS
                )
            except requests.ConnectionError as exc:
                # Surface a clearer message — this is the #1 setup gotcha.
                raise ConnectionError(
                    f"Could not reach Ollama at {self.host}. Is the Ollama "
                    f"daemon running? Start the desktop app, or run `ollama serve`."
                ) from exc

            if resp.status_code == 404:
                # /api/chat 404 means the model isn't pulled.
                raise OllamaModelMissingError(
                    f"Ollama model {self.model!r} is not pulled on {self.host}. "
                    f"Run:  ollama pull {self.model}"
                )

            if resp.status_code >= 500:
                # Treat 5xx as transient — retry_call will back off.
                raise requests.ConnectionError(
                    f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]!r}"
                )

            if resp.status_code != 200:
                # 4xx other than 404: bad request, model parameter mismatch, etc.
                # Not retryable.
                raise RuntimeError(
                    f"Ollama HTTP {resp.status_code}: {resp.text[:300]!r}"
                )

            data = resp.json()
            text = ((data.get("message") or {}).get("content") or "").strip()
            if not text:
                raise RuntimeError(
                    f"Ollama returned empty content. Response: {data!r}"
                )
            return text

        # OllamaModelMissingError is permanent — don't retry. Same for
        # any RuntimeError other than transient HTTP-shaped ones.
        return retry_call(
            _call,
            attempts=4,
            base=2.0,
            max_wait=30.0,
            exceptions=(*_RETRYABLE, RuntimeError),
            should_retry=lambda exc: not isinstance(
                exc, (OllamaModelMissingError, QuotaExhaustedError)
            ),
        )


_singleton: Optional[OllamaClient] = None
_singleton_lock = threading.Lock()


def get_ollama_client() -> OllamaClient:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = OllamaClient()
    return _singleton
