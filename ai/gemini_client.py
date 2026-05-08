"""
Thin wrapper around the Google Gemini API.

Adds two pieces of operational hardening over the raw SDK:

  1. Inter-call rate limiting — minimum spacing between calls so we
     stay well under the free-tier 15 RPM cap on `gemini-2.0-flash`.
  2. Quota-exhausted detection — if the API ever returns a 429 with
     `limit: 0` (the signature of a Workspace-account key with no
     free-tier allotment), we raise a non-retryable error and pause
     ALL subsequent Gemini calls for `QUOTA_PAUSE_SECONDS`. This stops
     the system from looping retries through 295 emails when there is
     literally nothing to retry against.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from config import settings
from utils.logger import get_logger
from utils.retry import retry_call

from .errors import QuotaExhaustedError

logger = get_logger(__name__)

# Stay safely under the free tier's 15 RPM ceiling. This is also a soft
# safeguard for paid tier users — keeps log noise / cost down.
_MIN_INTERVAL_SECONDS = 4.0

# When quota is genuinely exhausted (limit:0), back off for this long
# before letting any thread try again. Long pause = quiet logs.
_QUOTA_PAUSE_SECONDS = 60 * 60  # 1 hour

# Match the "limit: 0" pattern in the 429 body — signature of a
# Workspace-account key with no free tier.
_QUOTA_ZERO_RE = re.compile(r"limit:\s*0\b", re.IGNORECASE)


def _is_transient_genai_error(exc: BaseException) -> bool:
    """Don't retry quota=0 errors (permanent) or our own QuotaExhaustedError."""
    if isinstance(exc, QuotaExhaustedError):
        return False
    if isinstance(exc, genai_errors.ClientError):
        msg = str(exc)
        if "RESOURCE_EXHAUSTED" in msg and _QUOTA_ZERO_RE.search(msg):
            return False
    return True


class GeminiClient:
    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or settings.llm_model
        self._client = genai.Client(api_key=settings.llm_api_key)
        self._gate_lock = threading.Lock()
        self._last_call_at: float = 0.0
        self._paused_until: float = 0.0
        logger.info("Gemini client initialised (model=%s)", self.model)

    # --- gating helpers ------------------------------------------------------

    def _wait_turn(self) -> None:
        """Enforce min interval + global pause; serialises all callers."""
        with self._gate_lock:
            now = time.monotonic()
            if now < self._paused_until:
                pause_remaining = self._paused_until - now
                logger.warning(
                    "Gemini calls paused for another %.0fs due to quota exhaustion.",
                    pause_remaining,
                )
                raise QuotaExhaustedError(
                    f"Gemini quota exhausted; paused for {pause_remaining:.0f}s."
                )
            elapsed = now - self._last_call_at
            if elapsed < _MIN_INTERVAL_SECONDS:
                time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
            self._last_call_at = time.monotonic()

    def _arm_quota_pause(self) -> None:
        with self._gate_lock:
            self._paused_until = time.monotonic() + _QUOTA_PAUSE_SECONDS
        logger.error(
            "Gemini key has zero quota (limit: 0). This usually means the API "
            "key was generated under a Google Workspace account, which does NOT "
            "get free-tier Gemini quota. Pausing all extraction for %d minutes. "
            "Fix: regenerate the key from a personal Google account at "
            "https://aistudio.google.com/apikey, OR enable billing on the "
            "Cloud project tied to the current key.",
            _QUOTA_PAUSE_SECONDS // 60,
        )

    # --- main entry point ----------------------------------------------------

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        config = genai_types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
        )

        def _call() -> str:
            self._wait_turn()
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=user,
                    config=config,
                )
            except genai_errors.ClientError as exc:
                msg = str(exc)
                if "RESOURCE_EXHAUSTED" in msg and _QUOTA_ZERO_RE.search(msg):
                    self._arm_quota_pause()
                    raise QuotaExhaustedError(
                        "Gemini quota is 0 (Workspace-account key). "
                        "See log for fix instructions."
                    ) from exc
                raise

            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("Gemini returned empty response")
            return text

        return retry_call(
            _call,
            attempts=4,
            base=2.0,
            max_wait=30.0,
            exceptions=(
                genai_errors.APIError,
                TimeoutError,
                ConnectionError,
                RuntimeError,
            ),
            should_retry=_is_transient_genai_error,
        )


_singleton: Optional[GeminiClient] = None
_singleton_lock = threading.Lock()


def get_gemini_client() -> GeminiClient:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = GeminiClient()
    return _singleton
