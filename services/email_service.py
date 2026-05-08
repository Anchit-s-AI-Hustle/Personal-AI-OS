"""
Glue between the Gmail layer, the AI extractor, and the task service.

`process_message` is the callback that the Gmail poller invokes for every
new message.
"""
from __future__ import annotations

from typing import Optional

from ai import get_extractor
from ai.gemini_client import QuotaExhaustedError
from database import get_db
from gmail.client import GmailMessage, get_gmail_client
from utils.logger import get_logger

from .task_service import TaskService

logger = get_logger(__name__)


class EmailService:
    def __init__(self, task_service: Optional[TaskService] = None) -> None:
        self._extractor = get_extractor()
        self._db = get_db()
        self._tasks = task_service or TaskService()
        self._gmail = get_gmail_client()

    def process_message(self, msg: GmailMessage) -> None:
        if self._db.email_already_processed(msg.message_id):
            logger.debug("Email %s already processed; skipping.", msg.message_id)
            return

        logger.info(
            "Processing email id=%s from=%r subject=%r",
            msg.message_id,
            msg.sender,
            msg.subject,
        )

        try:
            extraction = self._extractor.extract_from_email(
                sender=msg.sender,
                subject=msg.subject,
                received_at=msg.received_at,
                body=msg.body_text,
            )
        except QuotaExhaustedError:
            # Don't mark the email as processed — we want to retry it once
            # quota recovers. Re-raise so the poller stops the current
            # batch immediately instead of looping through dozens of
            # messages with the same outcome.
            raise
        except Exception:
            logger.exception("Gemini extraction failed for email %s", msg.message_id)
            self._db.record_processed_email(
                gmail_message_id=msg.message_id,
                thread_id=msg.thread_id,
                subject=msg.subject,
                sender=msg.sender,
                received_at=msg.received_at,
                summary=None,
                status="failed",
            )
            return

        new_tasks = 0
        if extraction.is_actionable and extraction.tasks:
            new_tasks = self._tasks.save_email_tasks(
                gmail_message_id=msg.message_id,
                sender=msg.sender,
                email_summary=extraction.summary,
                tasks=extraction.tasks,
            )

        self._db.record_processed_email(
            gmail_message_id=msg.message_id,
            thread_id=msg.thread_id,
            subject=msg.subject,
            sender=msg.sender,
            received_at=msg.received_at,
            summary=extraction.summary,
            status="processed" if extraction.is_actionable else "skipped",
        )

        logger.info(
            "Email %s -> actionable=%s, %d new task(s)",
            msg.message_id,
            extraction.is_actionable,
            new_tasks,
        )
