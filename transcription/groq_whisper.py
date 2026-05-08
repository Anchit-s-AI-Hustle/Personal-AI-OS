"""
Groq Whisper backend.

Drop-in replacement for the local Faster-Whisper engine when the local
ctranslate2 wheel doesn't work on the current Python (e.g. 3.14). Uses
Groq's hosted Whisper Large v3 Turbo — same model family, multilingual
(handles Hindi + English code-switching), free-tier on Groq covers
~7,200 transcriptions/day which is plenty for 2-min chunks.

Uses the same TranscriptionResult shape as `whisper_engine` so the
meeting service doesn't care which backend is active.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from groq import Groq
from groq import APIConnectionError, APIError, APIStatusError, RateLimitError

from config import settings
from utils.logger import get_logger
from utils.retry import retry_call

from .lexicon import correct_names, whisper_prompt
from .types import TranscriptionResult

logger = get_logger(__name__)

# Groq's free tier on whisper-large-v3-turbo is 30 RPM, 7,200 RPD.
# 2.5s minimum interval keeps us safely under 30 RPM.
_MIN_INTERVAL_SECONDS = 2.5


def _is_transient(exc: BaseException) -> bool:
    """Don't retry permanent failures (4xx other than 429)."""
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if 400 <= status < 500 and status != 429:
            return False
    return True


class GroqWhisperEngine:
    def __init__(
        self,
        model: Optional[str] = None,
        language: Optional[str] = None,
    ) -> None:
        self._model = model or settings.groq_whisper_model
        # Allow blank/None to mean "auto-detect language".
        self._language = (language or settings.whisper_language or "").strip() or None
        self._client = Groq(api_key=settings.groq_api_key)
        self._gate_lock = threading.Lock()
        self._last_call_at: float = 0.0
        logger.info(
            "Groq Whisper engine initialised (model=%s, lang=%s)",
            self._model,
            self._language or "auto",
        )

    @property
    def available(self) -> bool:
        # Network call required for true availability check, but if we got
        # this far the key + import worked — return True so callers don't
        # gate their pipeline behind a probe.
        return True

    def _wait_turn(self) -> None:
        with self._gate_lock:
            now = time.monotonic()
            elapsed = now - self._last_call_at
            if elapsed < _MIN_INTERVAL_SECONDS:
                time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
            self._last_call_at = time.monotonic()

    def transcribe_file(self, audio_path: Path) -> TranscriptionResult:
        if not audio_path.exists():
            return TranscriptionResult(
                text="", language=None, language_probability=None, duration=None
            )

        def _call() -> TranscriptionResult:
            self._wait_turn()
            with audio_path.open("rb") as f:
                kwargs = {
                    "file": (audio_path.name, f.read()),
                    "model": self._model,
                    "response_format": "verbose_json",
                    # Vocabulary hint — this is the BIG accuracy win for
                    # names + brand jargon. Whisper biases tokenisation
                    # toward terms in this string.
                    "prompt": whisper_prompt(),
                }
                if self._language:
                    kwargs["language"] = self._language
                resp = self._client.audio.transcriptions.create(**kwargs)

            raw_text = (getattr(resp, "text", "") or "").strip()
            # Belt-and-braces: even with the prompt, Whisper sometimes
            # still mishears. correct_names() rewrites known aliases.
            text = correct_names(raw_text)
            language = getattr(resp, "language", None)
            duration = getattr(resp, "duration", None)
            segments_raw = getattr(resp, "segments", []) or []
            segments: list[dict] = []
            for seg in segments_raw:
                if isinstance(seg, dict):
                    segments.append(
                        {
                            "start": seg.get("start"),
                            "end": seg.get("end"),
                            "text": correct_names((seg.get("text") or "").strip()),
                        }
                    )

            return TranscriptionResult(
                text=text,
                language=language,
                language_probability=None,
                duration=duration,
                segments=segments,
            )

        return retry_call(
            _call,
            attempts=3,
            base=2.0,
            max_wait=20.0,
            exceptions=(
                APIConnectionError,
                RateLimitError,
                APIStatusError,
                APIError,
                TimeoutError,
                ConnectionError,
            ),
            should_retry=_is_transient,
        )


_singleton: Optional[GroqWhisperEngine] = None
_singleton_lock = threading.Lock()


def get_groq_whisper_engine() -> GroqWhisperEngine:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = GroqWhisperEngine()
    return _singleton
