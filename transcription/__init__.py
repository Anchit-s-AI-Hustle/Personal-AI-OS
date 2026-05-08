"""
Audio capture + speech-to-text.

`get_whisper_engine()` is a factory that returns either:
  - The local Faster-Whisper engine (default) — runs offline on CPU/GPU
  - The Groq-hosted Whisper engine — when STT_BACKEND=groq, or when the
    local backend can't load on the current Python (Python 3.14 broke
    ctranslate2 wheels at the time of writing).

Either way the meeting service gets the same `TranscriptionResult`
shape from `transcription.types`.
"""
from __future__ import annotations

import threading

from config import settings

from .audio_capture import AudioCapture, AudioChunk
from .types import TranscriptionResult

_lock = threading.Lock()
_cached_engine = None


def get_whisper_engine():
    """Return the configured STT backend (singleton)."""
    global _cached_engine
    if _cached_engine is not None:
        return _cached_engine
    with _lock:
        if _cached_engine is not None:
            return _cached_engine

        backend = (settings.stt_backend or "local").strip().lower()
        if backend == "groq":
            from .groq_whisper import get_groq_whisper_engine
            _cached_engine = get_groq_whisper_engine()
        elif backend == "local":
            from .whisper_engine import get_whisper_engine as _local
            _cached_engine = _local()
        else:
            raise RuntimeError(
                f"Unknown STT_BACKEND={backend!r}. Use 'local' or 'groq'."
            )
        return _cached_engine


__all__ = [
    "AudioCapture",
    "AudioChunk",
    "TranscriptionResult",
    "get_whisper_engine",
]
