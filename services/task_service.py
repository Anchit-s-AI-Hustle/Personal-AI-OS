"""
Task persistence service.

Acts as the single doorway through which extracted tasks enter the DB.
Centralising it here means the dedup hash, status defaults, and source-id
formatting can't drift between the email and meeting flows.
"""
from __future__ import annotations

from typing import Iterable, Optional

from database import get_db
from database.models import ExtractedTask
from utils.logger import get_logger

logger = get_logger(__name__)


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
        return self._save(
            source_type="Email",
            source_ref_id=gmail_message_id,
            summary=email_summary,
            default_speaker=sender,
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
        return self._save(
            source_type="Meeting",
            source_ref_id=ref,
            summary=chunk_summary,
            default_speaker=None,
            tasks=tasks,
        )

    def _save(
        self,
        *,
        source_type: str,
        source_ref_id: str,
        summary: Optional[str],
        default_speaker: Optional[str],
        tasks: Iterable[ExtractedTask],
    ) -> int:
        inserted = 0
        for task in tasks:
            heading = (task.task_heading or "").strip()
            if not heading:
                continue
            row_id = self._db.insert_task(
                source_type=source_type,
                source_ref_id=source_ref_id,
                task=heading,
                task_description=task.task_description or None,
                rationale=task.rationale or None,
                growth_pillar=task.growth_pillar or None,
                deadline=task.deadline,
                urgency=task.urgency,
                sender_or_speaker=task.sender_or_speaker or default_speaker,
                summary=summary,
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
