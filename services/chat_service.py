"""
Chat → Gemini → tasks pipeline.

Each Chat message that arrives is fed to the same meeting-style
extractor (chat is conversation, just typed). Extracted tasks land in
the "Tasks From Discussions" tab via the dual-write logic.
"""
from __future__ import annotations

from typing import Optional

from ai import get_extractor
from chat.client import ChatMessage
from database import get_db
from utils.logger import get_logger

from .task_service import TaskService

logger = get_logger(__name__)


class ChatService:
    def __init__(self, task_service: Optional[TaskService] = None) -> None:
        self._extractor = get_extractor()
        self._db = get_db()
        self._tasks = task_service or TaskService()

    def process_message(self, msg: ChatMessage) -> None:
        if self._db.email_already_processed(msg.message_id):
            return

        space_label = msg.space_display or msg.space_type or "Chat"
        sender = msg.sender_display or "(unknown)"
        # Reuse the meeting-chunk prompt — the contract is the same
        # (transcript-like text, return tasks/ideas/etc).
        wrapped = (
            f"Chat in {space_label} ({msg.space_type})\n"
            f"From {sender} at {msg.create_time}:\n\n"
            f"{msg.text}"
        )

        try:
            extraction = self._extractor.extract_from_meeting_chunk(
                started_at=msg.create_time,
                transcript=wrapped,
            )
        except Exception:
            logger.exception("Gemini extraction failed for chat msg %s", msg.message_id)
            self._db.record_processed_email(
                gmail_message_id=msg.message_id,
                thread_id=msg.thread_name,
                subject=f"Chat / {space_label}",
                sender=sender,
                received_at=msg.create_time,
                summary=None,
                status="failed",
            )
            return

        # Save tasks. Use the chat message id as source_ref_id so we can
        # always find the original message later.
        n_tasks = 0
        if extraction.tasks:
            n_tasks = self._tasks.save_chat_tasks(
                chat_message_id=msg.message_id,
                sender=sender,
                chat_summary=extraction.summary,
                tasks=extraction.tasks,
            )

        # Re-use the processed_emails table as a generic "we've seen this id"
        # gate so the dedup logic stays uniform across email + chat.
        self._db.record_processed_email(
            gmail_message_id=msg.message_id,
            thread_id=msg.thread_name,
            subject=f"Chat / {space_label}",
            sender=sender,
            received_at=msg.create_time,
            summary=extraction.summary,
            status="processed" if n_tasks else "skipped",
        )
        logger.info(
            "Chat msg %s in %r: %d task(s) extracted.",
            msg.message_id,
            space_label,
            n_tasks,
        )
