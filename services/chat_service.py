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
from utils.identifiers import clean_identifier, is_placeholder_identifier
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
    For a DM, figure out the other person's display name. Falls back to
    None when we genuinely don't know — we never surface raw `users/...`
    resource IDs in the Source column.
    """
    if not space_name:
        return None
    with _partner_lock:
        cached = _partner_cache.get(space_name)
    if cached:
        return cached

    # If THIS message is from the partner (not self), we may have just
    # learned their displayName.
    sender = msg.raw.get("sender") or {} if isinstance(msg.raw, dict) else {}
    sender_id = sender.get("name") or ""
    if sender_id and not _is_self(sender_id):
        partner_label = clean_identifier(sender.get("displayName"))
        if partner_label:
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


def _compute_source_link(msg: ChatMessage) -> Optional[str]:
    """
    Build a clickable Google Chat URL for the originating space.

    Format (works in any logged-in Google Chrome profile):
      https://mail.google.com/chat/u/0/#chat/space/<bare-space-id>
      https://mail.google.com/chat/u/0/#chat/dm/<bare-space-id>

    Google Chat doesn't expose stable per-message deep-links via the API,
    so we fall back to linking to the space itself. Clicking opens the
    conversation; finding the message is one scroll.
    """
    if not msg.space_name:
        return None
    # space_name looks like "spaces/AAAA1234" — strip the prefix.
    bare = msg.space_name.split("/", 1)[-1] if "/" in msg.space_name else msg.space_name
    if msg.space_type == "DIRECT_MESSAGE":
        path = "dm"
    else:
        path = "space"
    return f"https://mail.google.com/chat/u/0/#chat/{path}/{bare}"


def _resolve_sender_label(msg: ChatMessage) -> Optional[str]:
    """
    Display label for the message sender ('Anchit (Self)' for self).
    Returns None when the sender is genuinely unknown — we never fall
    back to the raw `users/...` resource path or "(unknown)" since
    those would leak into the SPOC column as junk identifiers.
    """
    sender_block = msg.raw.get("sender") if isinstance(msg.raw, dict) else None
    sender_id = ""
    if isinstance(sender_block, dict):
        sender_id = sender_block.get("name") or ""
    if _is_self(sender_id):
        return settings.self_display_name
    return clean_identifier(msg.sender_display)


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
        source_link = _compute_source_link(msg)
        is_from_self = sender_label == settings.self_display_name
        # For prompt framing (LLM sees this), keep a human-readable string
        # but never expose raw resource IDs. For DB persistence, we still
        # store None when unknown so the sheet doesn't show junk.
        sender_label_for_prompt = sender_label or "an unidentified colleague"

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
                f"This message is in a {source_detail}, sent by {sender_label_for_prompt}. "
                f"Anything they're asking Anchit (the user) to do is a TASK for "
                f"Anchit (set owner to {settings.self_display_name!r}). Anything "
                f"they're committing to is a TASK for them (owner = sender). "
                f"If you don't know the sender's real name, set owner to null — "
                f"DO NOT make up a placeholder name or identifier."
            )
        wrapped = (
            f"{framing}\n\n"
            f"--- Chat message ({msg.create_time}) ---\n"
            f"From: {sender_label_for_prompt}\n"
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
                sender=sender_label,  # may be None if unknown — TaskService tolerates
                chat_summary=extraction.summary,
                tasks=extraction.tasks,
                source_detail=source_detail,
                source_link=source_link,
                sent_at=msg.create_time,
                sender_contact=clean_identifier(msg.sender_email),
            )

        self._db.record_processed_email(
            gmail_message_id=msg.message_id,
            thread_id=msg.thread_name,
            subject=f"Chat / {source_detail}",
            sender=sender_label or "(sender unknown)",  # log only — not the sheet
            received_at=msg.create_time,
            summary=extraction.summary,
            status="processed" if n_tasks else "skipped",
        )
        logger.info(
            "Chat msg %s [%s, sender=%s]: %d task(s).",
            msg.message_id,
            source_detail,
            sender_label or "<unknown>",
            n_tasks,
        )
