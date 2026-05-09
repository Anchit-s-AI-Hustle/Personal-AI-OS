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
from utils.identifiers import clean_identifier
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
        received_at: Optional[str] = None,
        thread_id: Optional[str] = None,
        sender_email: Optional[str] = None,
    ) -> int:
        # Source detail: human-readable name of who sent the email.
        detail = f"from {clean_sender_name(sender)}".strip()
        # Direct Gmail link to the thread (or just the message if no thread id).
        link = (
            f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"
            if thread_id else
            f"https://mail.google.com/mail/u/0/#inbox/{gmail_message_id}"
        )
        return self._save(
            source_type="Email",
            source_ref_id=gmail_message_id,
            source_detail=detail or None,
            source_link=link,
            date_given=received_at,
            spoc_contact=sender_email,
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
        started_at: Optional[str] = None,
    ) -> int:
        ref = f"{session_id}:{chunk_index:04d}"
        # We're recording the user alone (no diarisation) — call it a voice memo.
        from config import settings as _settings
        detail = f"voice memo by {_settings.self_display_name}"
        return self._save(
            source_type="Meeting",
            source_ref_id=ref,
            source_detail=detail,
            source_link=None,
            date_given=started_at,
            spoc_contact=None,
            summary=chunk_summary,
            default_speaker=None,
            tasks=tasks,
        )

    def save_whatsapp_tasks(
        self,
        *,
        gmail_message_id: str,
        chat_partner: str,
        chat_summary: Optional[str],
        tasks: Iterable[ExtractedTask],
        exported_at: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> int:
        """
        WhatsApp chats arrive as forwarded "Export chat" emails. We use
        the Gmail message id as the dedup ref and the Gmail link as the
        source_link so the user can jump back to the export email and
        re-read the full thread if needed.
        """
        link = (
            f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"
            if thread_id else
            f"https://mail.google.com/mail/u/0/#inbox/{gmail_message_id}"
        )
        detail = f"WhatsApp: {chat_partner}" if chat_partner else "WhatsApp"
        return self._save(
            source_type="WhatsApp",
            source_ref_id=f"whatsapp:{gmail_message_id}",
            source_detail=detail,
            source_link=link,
            date_given=exported_at,
            spoc_contact=None,  # phone numbers come from the LLM if present
            summary=chat_summary,
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
        source_link: Optional[str] = None,
        sent_at: Optional[str] = None,
        sender_contact: Optional[str] = None,
    ) -> int:
        return self._save(
            source_type="Chat",
            source_ref_id=f"chat:{chat_message_id}",
            source_detail=source_detail,
            source_link=source_link,
            date_given=sent_at,
            spoc_contact=sender_contact,
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
        source_link: Optional[str],
        date_given: Optional[str],
        spoc_contact: Optional[str],
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
            # SPOC must be a real human name — never an opaque API id
            # ("users/12345"), never "(unknown)", never blank-ish junk.
            # If the LLM (or upstream chat client) couldn't find a real
            # name, leave SPOC empty rather than poisoning the column.
            raw_spoc = correct_names(task.sender_or_speaker or default_speaker or "")
            spoc = clean_identifier(raw_spoc)
            # Per-task contact (from the LLM) wins; fall back to the
            # source-level contact (e.g. email sender). Anything that
            # doesn't look like a real email/phone is dropped.
            contact = (
                clean_identifier(task.owner_contact)
                or clean_identifier(spoc_contact)
            )
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
                source_link=source_link,
                date_given=date_given,
                spoc_contact=contact,
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
