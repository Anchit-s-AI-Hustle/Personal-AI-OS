"""
Faster-Whisper engine wrapper.

The model is loaded lazily on first transcription. We use `int8` on CPU by
default which keeps RAM under ~1GB for the `base` model.

If `faster_whisper` / `ctranslate2` cannot load on the current Python
version (a real risk on bleeding-edge Pythons before wheels are released),
we degrade gracefully: every call to `transcribe_file` returns an empty
result and logs once. The audio chunks are still captured to disk by
`AudioCapture`, and the rest of the pipeline (email, sheets, daily
summaries) continues to work.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from config import settings
from utils.logger import get_logger

from .types import TranscriptionResult

logger = get_logger(__name__)

_PROBE_PATH = Path(__file__).parent / "whisper_probe.py"
_PROBE_TIMEOUT_S = 60.0


def _probe_whisper(model: str, device: str, compute_type: str) -> tuple[bool, str]:
    """
    Run the probe subprocess. Returns (ok, reason). On segfault/timeout
    we return ok=False with a useful reason string.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(_PROBE_PATH), model, device, compute_type],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"probe timed out after {_PROBE_TIMEOUT_S}s"
    except FileNotFoundError as exc:
        return False, f"probe could not be launched: {exc}"

    if result.returncode == 0:
        return True, "ok"

    # Negative returncodes on Windows = crash signal (e.g. -1073741819 = access violation).
    if result.returncode < 0 or result.returncode > 1:
        return False, f"native crash (exit code {result.returncode})"

    err = (result.stderr or "").strip()
    return False, f"python exception: {err.splitlines()[-1] if err else 'unknown'}"


class WhisperEngine:
    def __init__(
        self,
        model: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
        language: Optional[str] = None,
    ) -> None:
        self._model_name = model or settings.whisper_model
        self._device = device or settings.whisper_device
        self._compute_type = compute_type or settings.whisper_compute_type
        self._language = language or settings.whisper_language  # None = auto-detect
        self._model = None
        self._lock = threading.Lock()
        self._unavailable_reason: Optional[str] = None
        self._warned_unavailable = False

    def _ensure_loaded(self):
        if self._model is not None:
            return self._model
        if self._unavailable_reason is not None:
            return None

        with self._lock:
            if self._model is not None:
                return self._model
            if self._unavailable_reason is not None:
                return None

            # 1. Probe in a subprocess — if it segfaults, only the probe dies.
            logger.info(
                "Probing Faster-Whisper compatibility (model=%s device=%s compute_type=%s)...",
                self._model_name,
                self._device,
                self._compute_type,
            )
            ok, reason = _probe_whisper(self._model_name, self._device, self._compute_type)
            if not ok:
                self._unavailable_reason = reason
                logger.error(
                    "Faster-Whisper probe failed (%s). Audio will continue to "
                    "be captured to %s as WAV files, but transcription is "
                    "DISABLED for this run. Most common cause on Windows: "
                    "ctranslate2 has no working wheel for your Python "
                    "version. Try recreating the venv with Python 3.12.",
                    reason,
                    settings.audio_chunks_dir,
                )
                return None
            logger.info("Faster-Whisper probe OK; loading the model in-process.")

            # 2. Probe passed — load in-process for real use.
            try:
                from faster_whisper import WhisperModel  # noqa: WPS433
                self._model = WhisperModel(
                    self._model_name,
                    device=self._device,
                    compute_type=self._compute_type,
                )
                return self._model
            except Exception as exc:
                self._unavailable_reason = repr(exc)
                logger.error(
                    "Faster-Whisper load failed after probe succeeded (%s). "
                    "Transcription disabled for this run.",
                    exc,
                )
                return None

    @property
    def available(self) -> bool:
        # Don't trigger a load just to check — only honest after _ensure_loaded.
        return self._model is not None

    def transcribe_file(self, audio_path: Path) -> TranscriptionResult:
        model = self._ensure_loaded()

        if model is None:
            if not self._warned_unavailable:
                logger.warning(
                    "Skipping transcription for %s — Whisper unavailable.",
                    audio_path.name,
                )
                self._warned_unavailable = True
            return TranscriptionResult(
                text="",
                language=None,
                language_probability=None,
                duration=None,
            )

        # Hindi-English code-switched audio works best with `language=None`
        # (auto-detect per chunk). Translation is OFF — we keep the original
        # language so the LLM can read both Hindi and English.
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=self._language,  # None lets Whisper detect
            task="transcribe",
            beam_size=1,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )

        text_parts: list[str] = []
        seg_dicts: list[dict] = []
        for seg in segments_iter:
            text_parts.append(seg.text.strip())
            seg_dicts.append(
                {
                    "start": float(seg.start) if seg.start is not None else None,
                    "end": float(seg.end) if seg.end is not None else None,
                    "text": seg.text.strip(),
                }
            )

        full_text = " ".join(p for p in text_parts if p).strip()
        return TranscriptionResult(
            text=full_text,
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
            duration=getattr(info, "duration", None),
            segments=seg_dicts,
        )


_singleton: Optional[WhisperEngine] = None
_lock = threading.Lock()


def get_whisper_engine() -> WhisperEngine:
    global _singleton
    if _singleton is None:
        with _lock:
            if _singleton is None:
                _singleton = WhisperEngine()
    return _singleton
