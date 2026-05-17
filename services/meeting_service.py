"""
Glue between audio chunks, Whisper, the AI extractor, and the task service.
"""
from __future__ import annotations

import json
from typing import Optional

from ai import get_extractor
from database import get_db
from storage import write_transcript_file
from transcription import AudioChunk, get_whisper_engine
from utils.logger import get_logger

from .task_service import TaskService

logger = get_logger(__name__)


# Phrases Whisper emits on near-silence or on noise that has no real speech.
# If a transcript is dominated by these, the LLM should never see it.
_WHISPER_HALLUCINATION_TOKENS = {
    "thank you",
    "thanks for watching",
    "thanks for watching!",
    "you",
    ".",
    "bye",
    "okay",
    "ok",
}


def _looks_like_noise(transcript: str) -> bool:
    """
    True if the transcript looks like Whisper hallucination on near-silence
    or otherwise too fragmentary to attribute tasks from.

    Conservative — we'd rather skip extraction than fabricate a task. A real
    meeting will easily clear this bar; a 5-second clip of "Thank you. Thank
    you. [Hindi noise]. 45 questions." will not.
    """
    import re

    text = (transcript or "").strip()
    if not text:
        return True

    lowered = text.lower()
    # Strip non-letters (handles Devanagari + punctuation) so we count
    # actual Latin words. Hindi-only speech with no Latin words is a
    # judgement call — we still let it through (the prompt itself now
    # demands explicit assignment evidence).
    words = re.findall(r"[a-zA-Z][a-zA-Z']{1,}", text)
    if len(words) < 6 and not re.search(r"[ऀ-ॿ]{20,}", text):
        # Few English words AND not a substantive Devanagari passage.
        return True

    # If the only English content is one of the canned hallucination
    # phrases, treat it as noise even if Hindi gibberish surrounds it.
    english_chunk = " ".join(words).lower().strip()
    if english_chunk in _WHISPER_HALLUCINATION_TOKENS:
        return True

    # Heavy repetition signature: same short phrase repeated >=3x.
    for phrase in _WHISPER_HALLUCINATION_TOKENS:
        if phrase and lowered.count(phrase) >= 3 and len(words) < 25:
            return True

    return False


class MeetingService:
    def __init__(self, task_service: Optional[TaskService] = None) -> None:
        self._whisper = get_whisper_engine()
        self._extractor = get_extractor()
        self._db = get_db()
        self._tasks = task_service or TaskService()

    def process_chunk(self, chunk: AudioChunk) -> None:
        logger.info(
            "Processing audio chunk session=%s idx=%d (%.1fs)",
            chunk.session_id,
            chunk.chunk_index,
            chunk.duration_seconds,
        )

        # 1. Make sure the session exists.
        self._db.start_meeting_session(chunk.session_id)

        # 1b. Skip silent chunks before they hit Whisper. Whisper hallucinates
        #     "Thank you" / "Thanks for watching" on pure silence; sending
        #     dead-mic audio through it just pollutes the DB. The capture
        #     layer flags chunks whose peak amplitude is below the silence
        #     threshold (default 0.005).
        if getattr(chunk, "is_silent", False):
            logger.info(
                "Chunk %d/%s is silent (peak=%.5f); skipping transcription + AI.",
                chunk.chunk_index,
                chunk.session_id,
                getattr(chunk, "peak_amplitude", 0.0),
            )
            return

        # 2. Transcribe.
        try:
            transcription = self._whisper.transcribe_file(chunk.audio_path)
        except Exception:
            logger.exception(
                "Whisper transcription failed for %s", chunk.audio_path.name
            )
            return

        if not transcription.text or len(transcription.text.strip()) < 3:
            logger.info(
                "Chunk %d in session %s had no usable speech; skipping AI step.",
                chunk.chunk_index,
                chunk.session_id,
            )
            self._db.insert_transcript_chunk(
                session_id=chunk.session_id,
                chunk_index=chunk.chunk_index,
                started_at=chunk.started_at,
                ended_at=chunk.ended_at,
                transcript=transcription.text or "",
                language=transcription.language,
                audio_path=str(chunk.audio_path),
            )
            return

        if _looks_like_noise(transcription.text):
            logger.info(
                "Chunk %d in session %s looks like noise/Whisper hallucination "
                "(transcript=%r); persisting transcript but skipping AI extraction.",
                chunk.chunk_index,
                chunk.session_id,
                transcription.text[:120],
            )
            self._db.insert_transcript_chunk(
                session_id=chunk.session_id,
                chunk_index=chunk.chunk_index,
                started_at=chunk.started_at,
                ended_at=chunk.ended_at,
                transcript=transcription.text,
                language=transcription.language,
                audio_path=str(chunk.audio_path),
                summary="(transcript unclear — extraction skipped)",
            )
            return

        # 3. Extract insights.
        try:
            extraction = self._extractor.extract_from_meeting_chunk(
                started_at=chunk.started_at,
                transcript=transcription.text,
            )
        except Exception:
            logger.exception(
                "Claude extraction failed for chunk %s/%d",
                chunk.session_id,
                chunk.chunk_index,
            )
            extraction = None

        summary = extraction.summary if extraction else None
        insights = (
            {
                "ideas": extraction.ideas,
                "blockers": extraction.blockers,
                "opportunities": extraction.opportunities,
                "decisions": extraction.decisions,
                "follow_ups": extraction.follow_ups,
            }
            if extraction
            else None
        )

        # 4. Persist transcript + summary.
        self._db.insert_transcript_chunk(
            session_id=chunk.session_id,
            chunk_index=chunk.chunk_index,
            started_at=chunk.started_at,
            ended_at=chunk.ended_at,
            transcript=transcription.text,
            language=transcription.language,
            audio_path=str(chunk.audio_path),
            summary=summary,
            insights=insights,
        )

        try:
            write_transcript_file(
                session_id=chunk.session_id,
                chunk_index=chunk.chunk_index,
                transcript=transcription.text,
                summary=summary,
            )
        except Exception:
            logger.exception("Could not persist transcript .txt for chunk %d", chunk.chunk_index)

        # 4b. Rate the transcript's accuracy via the LLM so the user can
        #     see at a glance how trustworthy each meeting row is.
        #     This is cheap (one short LLM call) and only runs when we
        #     actually have a transcript with tasks worth saving.
        accuracy_pct: Optional[int] = None
        accuracy_explanation: Optional[str] = None
        if extraction and extraction.tasks:
            try:
                rating = self._extractor.rate_transcription_accuracy(
                    transcript=transcription.text,
                    language=transcription.language,
                )
                if rating is not None:
                    accuracy_pct = rating.get("accuracy")
                    accuracy_explanation = rating.get("explanation")
            except Exception:
                logger.exception(
                    "Transcription accuracy rating failed for %s/%d; "
                    "saving tasks without a rating.",
                    chunk.session_id, chunk.chunk_index,
                )

        # 5. Save tasks.
        if extraction and extraction.tasks:
            self._tasks.save_meeting_tasks(
                session_id=chunk.session_id,
                chunk_index=chunk.chunk_index,
                chunk_summary=summary,
                tasks=extraction.tasks,
                started_at=chunk.started_at,
                transcript_text=transcription.text,
                transcription_accuracy=accuracy_pct,
                accuracy_explanation=accuracy_explanation,
            )

        logger.info(
            "Chunk %d/%s processed: %d task(s), %d idea(s), %d blocker(s)",
            chunk.chunk_index,
            chunk.session_id,
            len(extraction.tasks) if extraction else 0,
            len(extraction.ideas) if extraction else 0,
            len(extraction.blockers) if extraction else 0,
        )

    # --- session finalisation -----------------------------------------------

    def finalize_session(self, session_id: str) -> None:
        chunks = self._db.fetchall(
            """
            SELECT chunk_index, summary, insights_json, transcript
              FROM transcript_chunks
             WHERE session_id = ?
             ORDER BY chunk_index ASC
            """,
            (session_id,),
        )
        if not chunks:
            return

        rolled_summary = "\n".join(
            f"- {c['summary']}" for c in chunks if c["summary"]
        ) or None

        merged: dict[str, list[str]] = {
            "ideas": [], "blockers": [], "opportunities": [],
            "decisions": [], "follow_ups": [],
        }
        for c in chunks:
            if not c["insights_json"]:
                continue
            try:
                obj = json.loads(c["insights_json"])
            except Exception:
                continue
            for key in merged:
                merged[key].extend(obj.get(key) or [])

        self._db.finalize_meeting_session(session_id, rolled_summary, merged)
        logger.info("Finalized meeting session %s", session_id)
