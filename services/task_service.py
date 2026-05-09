"""
Task persistence service.

Acts as the single doorway through which extracted tasks enter the DB.
Centralising it here means the dedup hash, status defaults, and source-id
formatting can't drift between the email and meeting flows.

Task-merge model:
  Two extracted tasks are considered the SAME work item when they share
  a normalized heading AND a SPOC. When a new task arrives that matches
  an existing OPEN row, instead of creating a duplicate sheet row we
  append a chronological line to that row's `all_updates` column. This
  keeps the sheet a clean one-row-per-task view while preserving the
  full follow-up history.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from database import get_db
from database.models import ExtractedTask
from transcription.lexicon import canonical_spoc, correct_names
from utils.identifiers import clean_identifier
from utils.logger import get_logger

logger = get_logger(__name__)

# "Foo Bar <foo@bar.com>" -> "Foo Bar". Falls back to the email if there's
# no display-name part.
_DISPLAY_NAME_RE = re.compile(r'^\s*"?([^"<]+?)"?\s*<')

# Whitespace-collapse used when normalizing headings for dedup.
_WS_RE = re.compile(r"\s+")
# Common imperative prefixes that vary between extractions of the same
# task ("Send X" vs "Share X" vs "Provide X"). Strip them when present
# so e.g. "Send the Q3 budget" merges with "Share the Q3 budget".
_IMPERATIVE_PREFIXES: tuple[str, ...] = (
    "send the ", "send ", "share the ", "share ",
    "provide the ", "provide ", "draft the ", "draft ",
    "prepare the ", "prepare ", "review the ", "review ",
    "follow up on ", "follow up with ", "follow up ",
    "check on the ", "check the ", "check ",
    "confirm the ", "confirm ", "finalize the ", "finalize ",
    "finalise the ", "finalise ", "create the ", "create ",
    "build the ", "build ", "set up the ", "set up ",
    "setup the ", "setup ", "update the ", "update ",
)


def normalize_heading(heading: Optional[str]) -> str:
    """
    Canonical form of a task heading used for merge-by-equality.
    Lowercase, collapse whitespace, drop trailing punctuation, and
    strip common imperative prefixes that vary between extractions.
    """
    if not heading:
        return ""
    s = heading.strip().lower()
    s = _WS_RE.sub(" ", s)
    s = s.rstrip(".!?:; ")
    for pref in _IMPERATIVE_PREFIXES:
        if s.startswith(pref):
            s = s[len(pref):].lstrip()
            break
    return s


def _format_update_line(
    *,
    when: Optional[str],
    speaker: Optional[str],
    source_label: str,
    note: Optional[str],
) -> str:
    """
    Render one entry for the All Updates column. Format:
        "YYYY-MM-DD HH:MM — Speaker (source): note"
    Any field that's missing is gracefully skipped — never produces a
    line with placeholder junk like "(unknown)".
    """
    parts: list[str] = []
    if when:
        # Trim ISO 8601 to minute precision for compactness.
        ts = when[:16].replace("T", " ")
        parts.append(ts)
    who_bits: list[str] = []
    if speaker:
        who_bits.append(speaker)
    if source_label:
        who_bits.append(f"({source_label})")
    head = " ".join(who_bits).strip()
    body = (note or "").strip()
    line = ""
    if parts:
        line = parts[0]
    if head:
        line = f"{line} — {head}" if line else head
    if body:
        line = f"{line}: {body}" if line else body
    return line


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
        # Imported here to avoid a circular at module-load time.
        from sheets.sync import _format_source_label

        inserted = 0
        merged = 0
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
            raw_spoc = task.sender_or_speaker or default_speaker or ""
            spoc = canonical_spoc(clean_identifier(raw_spoc))
            # Per-task contact (from the LLM) wins; fall back to the
            # source-level contact (e.g. email sender). Anything that
            # doesn't look like a real email/phone is dropped.
            contact = (
                clean_identifier(task.owner_contact)
                or clean_identifier(spoc_contact)
            )
            normalized = normalize_heading(heading)

            # Merge: if we already track an OPEN task with the same
            # canonical heading and same SPOC, append an update entry
            # to it instead of creating a duplicate row.
            existing = self._db.find_open_task_by_heading(
                normalized_heading=normalized,
                spoc=spoc,
            )
            if existing is not None:
                update_line = _format_update_line(
                    when=date_given,
                    speaker=spoc or default_speaker,
                    source_label=_format_source_label(
                        source_type=source_type,
                        source_detail=source_detail or "",
                    ),
                    note=description or correct_names(summary or "") or rationale,
                )
                self._db.append_task_update(int(existing["id"]), update_line)
                merged += 1
                logger.info(
                    "Merged into existing task id=%s (%r) — appended update.",
                    existing["id"],
                    heading,
                )
                continue

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
                normalized_heading=normalized,
            )
            if row_id is not None:
                inserted += 1
            else:
                logger.debug("Duplicate task ignored: %r (ref=%s)", heading, source_ref_id)

        if inserted or merged:
            logger.info(
                "%s/%s: %d new task(s), %d merged update(s).",
                source_type, source_ref_id, inserted, merged,
            )
        return inserted
