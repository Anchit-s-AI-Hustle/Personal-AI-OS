"""
Chat → Gemini/Groq → tasks pipeline.

Per chat message:
  - Resolves "self" vs "other person" using settings.self_chat_user_id.
  - Computes a human-readable source_detail like "Chat with Aman" for DMs
    or "Chat in <space name>" for spaces.
  - Builds a richer prompt context so the LLM understands when the
    sender is the user (treat tasks as work the user handed off to the
    other party) vs when it's someone else asking the user.
  - Persists tasks via TaskService with the right SPOC.
"""
from __future__ import annotations

import threading
from typing import Optional

from ai import QuotaExhaustedError, get_extractor
from chat.client import ChatMessage
from config import settings
from database import get_db
from utils.logger import get_logger

from .task_service import TaskService

logger = get_logger(__name__)


def _is_self(user_name: str) -> bool:
    """`user_name` is a Chat user resource string like 'users/<digits>'."""
    sid = (settings.self_chat_user_id or "").strip()
    return bool(sid) and user_name == sid


# Cache of "the other person" per DM space, lazily resolved by inspecting
# message senders the first time we see the space.
_partner_cache: dict[str, str] = {}
_partner_lock = threading.Lock()


def _dm_partner_label(space_name: str, msg: ChatMessage) -> Optional[str]:
    """
    For a DM, figure out the other person's display name.
    Falls back to None when we don't know yet.
    """
    if not space_name:
        return None
    with _partner_lock:
        cached = _partner_cache.get(space_name)
    if cached:
        return cached

    # If THIS message is from the partner (not self), we just learned them.
    sender = msg.raw.get("sender") or {} if isinstance(msg.raw, dict) else {}
    sender_id = sender.get("name") or ""
    if sender_id and not _is_self(sender_id):
        partner_label = (
            sender.get("displayName")
            or sender_id  # fall back to the user id; better than nothing
        )
        with _partner_lock:
            _partner_cache[space_name] = partner_label
        return partner_label
    return None


def _compute_source_detail(msg: ChatMessage) -> str:
    """Human-readable Source detail for a chat message."""
    if msg.space_type == "DIRECT_MESSAGE":
        partner = _dm_partner_label(msg.space_name, msg)
        if partner:
            return f"DM with {partner}"
        return "DM"
    if msg.space_type == "GROUP_CHAT":
        label = msg.space_display or "group chat"
        return f"Group: {label}"
    if msg.space_type == "SPACE":
        label = msg.space_display or "space"
        return f"Space: {label}"
    return msg.space_type or "Chat"


def _resolve_sender_label(msg: ChatMessage) -> str:
    """Display label for the message sender ('Anchit (Self)' for self)."""
    sender_block = msg.raw.get("sender") if isinstance(msg.raw, dict) else None
    sender_id = ""
    if isinstance(sender_block, dict):
        sender_id = sender_block.get("name") or ""
    if _is_self(sender_id):
        return settings.self_display_name
    # Otherwise prefer the displayName chat_client already extracted.
    return msg.sender_display or sender_id or "(unknown)"


class ChatService:
    def __init__(self, task_service: Optional[TaskService] = None) -> None:
        self._extractor = get_extractor()
        self._db = get_db()
        self._tasks = task_service or TaskService()

    def process_message(self, msg: ChatMessage) -> None:
        if self._db.email_already_processed(msg.message_id):
            return

        sender_label = _resolve_sender_label(msg)
        source_detail = _compute_source_detail(msg)
        is_from_self = sender_label == settings.self_display_name

        # Frame the prompt so the LLM understands the role.
        if is_from_self:
            framing = (
                f"Anchit Tandon (the user) sent the following message in a "
                f"{source_detail}. Anything Anchit asks the recipient to do, "
                f"or any feedback Anchit gives them, is a TASK for the "
                f"recipient — set 'owner' to them when known. Anything Anchit "
                f"commits to is a TASK for Anchit (set owner to "
                f"{settings.self_display_name!r})."
            )
        else:
            framing = (
                f"This message is in a {source_detail}, sent by {sender_label}. "
                f"Anything they're asking Anchit (the user) to do is a TASK for "
                f"Anchit (set owner to {settings.self_display_name!r}). Anything "
                f"they're committing to is a TASK for them (owner = sender)."
            )
        wrapped = (
            f"{framing}\n\n"
            f"--- Chat message ({msg.create_time}) ---\n"
            f"From: {sender_label}\n"
            f"Where: {source_detail}\n\n"
            f"{msg.text}"
        )

        try:
            extraction = self._extractor.extract_from_meeting_chunk(
                started_at=msg.create_time,
                transcript=wrapped,
            )
        except QuotaExhaustedError:
            raise
        except Exception:
            logger.exception("LLM extraction failed for chat msg %s", msg.message_id)
            self._db.record_processed_email(
                gmail_message_id=msg.message_id,
                thread_id=msg.thread_name,
                subject=f"Chat / {source_detail}",
                sender=sender_label,
                received_at=msg.create_time,
                summary=None,
                status="failed",
            )
            return

        n_tasks = 0
        if extraction.tasks:
            n_tasks = self._tasks.save_chat_tasks(
                chat_message_id=msg.message_id,
                sender=sender_label,
                chat_summary=extraction.summary,
                tasks=extraction.tasks,
                source_detail=source_detail,
            )

        self._db.record_processed_email(
            gmail_message_id=msg.message_id,
            thread_id=msg.thread_name,
            subject=f"Chat / {source_detail}",
            sender=sender_label,
            received_at=msg.create_time,
            summary=extraction.summary,
            status="processed" if n_tasks else "skipped",
        )
        logger.info(
            "Chat msg %s [%s, sender=%s]: %d task(s).",
            msg.message_id,
            source_detail,
            sender_label,
            n_tasks,
        )
