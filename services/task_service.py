"""
Task persistence service.

Acts as the single doorway through which extracted tasks enter the DB.
Centralising it here means the dedup hash, status defaults, and source-id
formatting can't drift between the email and meeting flows.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from database import get_db
from database.models import ExtractedTask
from transcription.lexicon import correct_names
from utils.logger import get_logger

logger = get_logger(__name__)

# "Foo Bar <foo@bar.com>" -> "Foo Bar". Falls back to the email if there's
# no display-name part.
_DISPLAY_NAME_RE = re.compile(r'^\s*"?([^"<]+?)"?\s*<')


def clean_sender_name(raw: Optional[str]) -> str:
    """Pull a human-readable display name out of an RFC 5322 'From' value."""
    if not raw:
        return ""
    raw = raw.strip()
    m = _DISPLAY_NAME_RE.match(raw)
    if m:
        return m.group(1).strip()
    # No display name — return the email itself if it looks like one.
    if "@" in raw:
        return raw
    return raw


class TaskService:
    def __init__(self) -> None:
        self._db = get_db()

    def save_email_tasks(
        self,
        *,
        gmail_message_id: str,
        sender: str,
        email_summary: Optional[str],
        tasks: Iterable[ExtractedTask],
    ) -> int:
        # Source detail: human-readable name of who sent the email.
        detail = f"from {clean_sender_name(sender)}".strip()
        return self._save(
            source_type="Email",
            source_ref_id=gmail_message_id,
            source_detail=detail or None,
            summary=email_summary,
            default_speaker=clean_sender_name(sender) or sender,
            tasks=tasks,
        )

    def save_meeting_tasks(
        self,
        *,
        session_id: str,
        chunk_index: int,
        chunk_summary: Optional[str],
        tasks: Iterable[ExtractedTask],
    ) -> int:
        ref = f"{session_id}:{chunk_index:04d}"
        # We're recording the user alone (no diarisation) — call it a voice memo.
        from config import settings as _settings
        detail = f"voice memo by {_settings.self_display_name}"
        return self._save(
            source_type="Meeting",
            source_ref_id=ref,
            source_detail=detail,
            summary=chunk_summary,
            default_speaker=None,
            tasks=tasks,
        )

    def save_chat_tasks(
        self,
        *,
        chat_message_id: str,
        sender: Optional[str],
        chat_summary: Optional[str],
        tasks: Iterable[ExtractedTask],
        source_detail: Optional[str] = None,
    ) -> int:
        return self._save(
            source_type="Chat",
            source_ref_id=f"chat:{chat_message_id}",
            source_detail=source_detail,
            summary=chat_summary,
            default_speaker=sender,
            tasks=tasks,
        )

    def _save(
        self,
        *,
        source_type: str,
        source_ref_id: str,
        source_detail: Optional[str],
        summary: Optional[str],
        default_speaker: Optional[str],
        tasks: Iterable[ExtractedTask],
    ) -> int:
        inserted = 0
        for task in tasks:
            # Defensive name-canonicalisation on every text field that
            # could carry a misheard name through from Whisper or the LLM.
            heading = correct_names((task.task_heading or "").strip())
            if not heading:
                continue
            description = correct_names(task.task_description or "")
            rationale = correct_names(task.rationale or "")
            spoc = correct_names(task.sender_or_speaker or default_speaker or "") or None
            row_id = self._db.insert_task(
                source_type=source_type,
                source_ref_id=source_ref_id,
                task=heading,
                task_description=description or None,
                rationale=rationale or None,
                growth_pillar=task.growth_pillar or None,
                deadline=task.deadline,
                urgency=task.urgency,
                sender_or_speaker=spoc,
                summary=correct_names(summary or "") or None,
                source_detail=source_detail,
            )
            if row_id is not None:
                inserted += 1
            else:
                logger.debug("Duplicate task ignored: %r (ref=%s)", heading, source_ref_id)

        if inserted:
            logger.info(
                "Saved %d new task(s) from %s/%s", inserted, source_type, source_ref_id
            )
        return inserted
