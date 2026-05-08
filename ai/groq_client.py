"""
Groq LLM client.

Same `complete(system, user, ...)` contract as the Gemini client so the
extractor doesn't need to know which provider is active. Groq's free
tier on `llama-3.3-70b-versatile` is ~14,400 requests/day per account
— way more than this single-user workload needs.

Operational concerns mirrored from gemini_client:
  - Inter-call throttle: 1.5s minimum spacing. Groq's free-tier RPM is
    30 on the 70B model, so this stays safely under.
  - 429 from Groq is genuine rate limiting (not "your account has
    zero quota") so we just back off and retry.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from groq import Groq
from groq import APIConnectionError, APIError, APIStatusError, RateLimitError

from config import settings
from utils.logger import get_logger
from utils.retry import retry_call

from .errors import QuotaExhaustedError

logger = get_logger(__name__)

_MIN_INTERVAL_SECONDS = 1.5
_QUOTA_PAUSE_SECONDS = 60 * 60  # 1 hour, mirrors Gemini client

_RETRYABLE: tuple[type[BaseException], ...] = (
    APIConnectionError,
    RateLimitError,
    APIStatusError,
    TimeoutError,
    ConnectionError,
)


class GroqClient:
    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or settings.groq_model
        self._client = Groq(api_key=settings.groq_api_key)
        self._gate_lock = threading.Lock()
        self._last_call_at: float = 0.0
        self._paused_until: float = 0.0
        logger.info("Groq client initialised (model=%s)", self.model)

    def _wait_turn(self) -> None:
        with self._gate_lock:
            now = time.monotonic()
            if now < self._paused_until:
                remaining = self._paused_until - now
                logger.warning(
                    "Groq calls paused for another %.0fs.", remaining
                )
                raise QuotaExhaustedError(
                    f"Groq calls paused for {remaining:.0f}s."
                )
            elapsed = now - self._last_call_at
            if elapsed < _MIN_INTERVAL_SECONDS:
                time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
            self._last_call_at = time.monotonic()

    def _arm_quota_pause(self, reason: str) -> None:
        with self._gate_lock:
            self._paused_until = time.monotonic() + _QUOTA_PAUSE_SECONDS
        logger.error(
            "Groq daily quota appears exhausted (%s). Pausing all extraction "
            "for %d minutes. If this is unexpected, check usage at "
            "https://console.groq.com/usage.",
            reason,
            _QUOTA_PAUSE_SECONDS // 60,
        )

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        def _call() -> str:
            self._wait_turn()
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    response_format={"type": "json_object"},
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except RateLimitError as exc:
                # Groq's daily rate limit reset is at 00:00 UTC. If we hit
                # the *daily* cap (vs. per-minute), we want to back off long.
                msg = str(exc).lower()
                if "daily" in msg or "rpd" in msg or "tpd" in msg:
                    self._arm_quota_pause("daily quota hit")
                    raise QuotaExhaustedError(
                        "Groq daily quota exhausted; paused."
                    ) from exc
                # Otherwise it's per-minute throttling — let retry_call back off.
                raise

            text = (response.choices[0].message.content or "").strip()
            if not text:
                raise RuntimeError("Groq returned empty response")
            return text

        return retry_call(
            _call,
            attempts=4,
            base=2.0,
            max_wait=30.0,
            exceptions=(*_RETRYABLE, RuntimeError),
        )


_singleton: Optional[GroqClient] = None
_singleton_lock = threading.Lock()


def get_groq_client() -> GroqClient:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = GroqClient()
    return _singleton
