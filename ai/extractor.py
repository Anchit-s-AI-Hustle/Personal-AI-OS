"""
High-level extraction API.

Combines the Claude client with the prompts and JSON parsing into typed
domain objects.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Optional

from database.models import (
    EmailExtraction,
    ExtractedTask,
    MeetingChunkExtraction,
    normalise_growth_pillar,
    normalise_urgency,
)
from utils.logger import get_logger

from . import prompts

logger = get_logger(__name__)

_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_BARE_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_block(text: str) -> dict[str, Any]:
    if not text:
        raise ValueError("Empty model response")

    m = _FENCE_RE.search(text)
    candidate = m.group(1) if m else None

    if candidate is None:
        m2 = _BARE_OBJ_RE.search(text)
        if m2:
            candidate = m2.group(0)

    if candidate is None:
        raise ValueError(f"No JSON object found in model response: {text[:200]!r}")

    return json.loads(candidate)


def _safe_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return str(v)


def _coerce_task(raw: dict[str, Any], default_speaker: Optional[str] = None) -> Optional[ExtractedTask]:
    # Prefer the new structured fields. Fall back to the legacy "task"
    # field if the model used the old shape.
    heading = _safe_str(raw.get("task_heading")) or _safe_str(raw.get("task"))
    if not heading:
        return None

    description = (
        _safe_str(raw.get("task_description"))
        or _safe_str(raw.get("description"))
        or ""
    )
    rationale = (
        _safe_str(raw.get("rationale"))
        or _safe_str(raw.get("why"))
        or _safe_str(raw.get("why_we_are_doing_this"))
        or ""
    )
    pillar = (
        _safe_str(raw.get("growth_pillar"))
        or _safe_str(raw.get("pillar"))
        or _safe_str(raw.get("category"))
        or "Other"
    )

    speaker = _safe_str(raw.get("owner")) or _safe_str(raw.get("spoc")) or default_speaker
    owner_contact = (
        _safe_str(raw.get("owner_contact"))
        or _safe_str(raw.get("spoc_contact"))
        or _safe_str(raw.get("contact"))
    )
    return ExtractedTask(
        task_heading=heading,
        task_description=description,
        rationale=rationale,
        growth_pillar=normalise_growth_pillar(pillar),
        urgency=normalise_urgency(_safe_str(raw.get("urgency"))),
        deadline=_safe_str(raw.get("deadline")),
        sender_or_speaker=speaker,
        owner_contact=owner_contact,
    )


class Extractor:
    def __init__(self, client=None) -> None:
        # Lazy import to avoid a circular: ai.__init__ imports Extractor,
        # but get_llm_client() is defined in ai.__init__.
        if client is None:
            from . import get_llm_client
            client = get_llm_client()
        self.client = client

    # --- email ---------------------------------------------------------------

    def extract_from_email(
        self,
        *,
        sender: str,
        subject: str,
        received_at: str,
        body: str,
    ) -> EmailExtraction:
        user_prompt = prompts.build_email_user_prompt(
            sender=sender, subject=subject, received_at=received_at, body=body
        )
        raw = self.client.complete(
            system=prompts.EMAIL_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=1500,
            temperature=0.1,
        )
        try:
            obj = _parse_json_block(raw)
        except Exception:
            logger.exception("Email extraction returned non-JSON; raw=%r", raw[:300])
            return EmailExtraction(summary="(unparseable AI response)", tasks=[], is_actionable=False)

        tasks_raw = obj.get("tasks") or []
        tasks: list[ExtractedTask] = []
        for t in tasks_raw:
            if not isinstance(t, dict):
                continue
            coerced = _coerce_task(t, default_speaker=sender)
            if coerced:
                tasks.append(coerced)

        return EmailExtraction(
            summary=_safe_str(obj.get("summary")) or "(no summary)",
            tasks=tasks,
            is_actionable=bool(obj.get("is_actionable")),
        )

    # --- meeting / conversation chunk ---------------------------------------

    def extract_from_meeting_chunk(
        self,
        *,
        started_at: str,
        transcript: str,
    ) -> MeetingChunkExtraction:
        user_prompt = prompts.build_meeting_user_prompt(
            started_at=started_at, transcript=transcript
        )
        raw = self.client.complete(
            system=prompts.MEETING_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=2000,
            temperature=0.1,
        )
        try:
            obj = _parse_json_block(raw)
        except Exception:
            logger.exception("Meeting extraction returned non-JSON; raw=%r", raw[:300])
            return MeetingChunkExtraction(summary="(unparseable AI response)")

        def _list_of_str(key: str) -> list[str]:
            v = obj.get(key) or []
            return [s for s in (_safe_str(x) for x in v) if s]

        tasks: list[ExtractedTask] = []
        for t in obj.get("tasks") or []:
            if not isinstance(t, dict):
                continue
            coerced = _coerce_task(t)
            if coerced:
                tasks.append(coerced)

        return MeetingChunkExtraction(
            summary=_safe_str(obj.get("summary")) or "(no summary)",
            tasks=tasks,
            ideas=_list_of_str("ideas"),
            blockers=_list_of_str("blockers"),
            opportunities=_list_of_str("opportunities"),
            decisions=_list_of_str("decisions"),
            follow_ups=_list_of_str("follow_ups"),
        )

    # --- transcription accuracy rating --------------------------------------

    def rate_transcription_accuracy(
        self,
        *,
        transcript: str,
        language: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Score how trustworthy a Whisper transcript looks on a 0-100 scale.

        Returns a dict like:
            {"accuracy": 78,
             "explanation": "Mostly clear English with some Hindi mixed in.
                             Two garbled phrases mid-sentence. To improve:
                             reduce background noise, move closer to the mic."}
        or None on parse failure (caller will leave the cells blank).

        Cheap call — short input, short JSON output, low temperature so
        the rating is reproducible across re-runs of the same transcript.
        """
        text = (transcript or "").strip()
        if not text:
            return None

        user = prompts.build_accuracy_rating_user_prompt(
            transcript=text, language=language or "auto",
        )
        try:
            raw = self.client.complete(
                system=prompts.ACCURACY_RATING_SYSTEM_PROMPT,
                user=user,
                max_tokens=400,
                temperature=0.0,
            )
        except Exception:
            logger.exception("LLM call failed during accuracy rating.")
            return None

        try:
            obj = _parse_json_block(raw)
        except Exception:
            logger.warning(
                "Accuracy rating returned non-JSON; raw=%r", raw[:200],
            )
            return None

        # Clamp to 0-100 and coerce to int.
        try:
            acc = int(round(float(obj.get("accuracy"))))
            acc = max(0, min(100, acc))
        except (TypeError, ValueError):
            return None

        explanation = _safe_str(obj.get("explanation")) or ""
        return {"accuracy": acc, "explanation": explanation}

    # --- daily summary -------------------------------------------------------

    def daily_summary(self, *, date_str: str, payload: str) -> dict[str, Any]:
        raw = self.client.complete(
            system=prompts.DAILY_SUMMARY_SYSTEM_PROMPT,
            user=prompts.build_daily_summary_user_prompt(date_str=date_str, payload=payload),
            max_tokens=2000,
            temperature=0.3,
        )
        try:
            return _parse_json_block(raw)
        except Exception:
            logger.exception("Daily summary returned non-JSON; raw=%r", raw[:300])
            return {"summary": raw.strip()}


_singleton: Optional[Extractor] = None
_lock = threading.Lock()


def get_extractor() -> Extractor:
    global _singleton
    if _singleton is None:
        with _lock:
            if _singleton is None:
                _singleton = Extractor()
    return _singleton
