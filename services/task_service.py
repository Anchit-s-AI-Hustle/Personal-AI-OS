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
# Strip non-alphanumeric except inner whitespace, so "Mother's-Day"
# matches "Mothers Day".
_PUNCT_RE = re.compile(r"[^\w\s]+")

# Verbs that mean essentially the same thing for our purposes. We
# collapse all of these to a single canonical form when normalizing.
# Example: "send", "share", "deliver", "forward", "provide", "submit"
# all become "send" — so headings that differ only by which verb the
# LLM chose still match.
_VERB_SYNONYMS: dict[str, tuple[str, ...]] = {
    "send":     ("send", "share", "deliver", "forward", "provide", "submit",
                 "give", "hand over", "pass"),
    "review":   ("review", "audit", "go through", "look at", "examine",
                 "verify", "validate", "confirm"),
    "create":   ("create", "build", "make", "produce", "draft", "design",
                 "set up", "setup", "establish"),
    "update":   ("update", "refresh", "edit", "modify", "revise", "adjust",
                 "tweak", "change"),
    "follow up": ("follow up", "chase", "ping", "remind", "circle back",
                  "check on", "check in"),
    "finalize": ("finalize", "finalise", "lock", "close", "wrap up", "sign off"),
    "fix":      ("fix", "resolve", "repair", "address", "troubleshoot"),
    "schedule": ("schedule", "book", "arrange", "set up a meeting", "plan"),
}

# Stop words dropped from normalized headings. Helps "the Q3 budget"
# match "Q3 budget", and "report for Aman" match "Aman's report".
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "for", "to", "of", "on", "with", "in", "at", "from",
    "by", "and", "or", "is", "are", "be", "this", "that", "these", "those",
    "into", "onto", "via", "about", "regarding", "re", "around",
})

# Words that genuinely end in "s" but are NOT plural (singular forms in
# their own right). Naive depluralisation must not chop them.
_PLURAL_EXCEPTIONS: frozenset[str] = frozenset({
    "ads", "us", "ios", "pwa", "css", "rss", "as", "is", "was", "has",
    "kpis", "sms", "aws", "cms", "ops", "yes", "status", "bus", "plus",
    "focus", "this", "less", "press", "process", "address", "access",
    "business", "analysis", "boss", "gas", "miss",
})


def _depluralise(word: str) -> str:
    """
    Strip a trailing 's' to merge naive plurals. Conservative:
      - Words shorter than 4 chars: untouched.
      - Known singular-s words (status, bus, focus, ...): untouched.
      - Words ending in 'us', 'is', 'os', 'as': untouched (Greek/Latin).
      - Words ending in 'ss' (process, address): untouched.
      - 'ies' -> 'y' for proper de-pluralisation of "queries", "stories".
    """
    if word in _PLURAL_EXCEPTIONS or len(word) < 4:
        return word
    # Words ending in vowel+'s' are usually singular: status, focus, bus.
    if word[-2:] in ("us", "is", "os", "as"):
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"     # "queries" -> "query"
    if word.endswith("ses") and len(word) > 4:
        return word[:-2]           # "campuses" -> "campus" (returns "campus")
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]           # "banners" -> "banner"
    return word


def _apply_verb_synonyms(words: list[str]) -> list[str]:
    """
    If the first 1-2 words match a verb-synonym phrase, replace them
    with the canonical verb. Single-word verbs are checked first, then
    two-word phrases ("follow up", "set up").
    """
    if not words:
        return words
    # Two-word verb phrase?
    if len(words) >= 2:
        bigram = f"{words[0]} {words[1]}"
        for canonical, aliases in _VERB_SYNONYMS.items():
            if bigram in aliases:
                return [canonical] + words[2:]
    # Single-word verb?
    for canonical, aliases in _VERB_SYNONYMS.items():
        if words[0] in aliases:
            return [canonical] + words[1:]
    return words


def normalize_heading(heading: Optional[str]) -> str:
    """
    Canonical form of a task heading used for merge-by-equality dedup.

    Pipeline:
      1. Lowercase + collapse whitespace + strip punctuation.
      2. Tokenise into words.
      3. Collapse verb synonyms ("share" -> "send", "build" -> "create")
         on the FIRST word/bigram.
      4. Drop stop words ("the", "a", "for", ...).
      5. De-pluralise each remaining word ("banners" -> "banner").
      6. Re-join with single spaces.

    Result: "Send the revised Q3 budgets" and "Share Q3 budget" both
    normalise to "send q3 budget" and merge into one row.
    """
    if not heading:
        return ""
    s = heading.strip().lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if not s:
        return ""
    words = s.split(" ")
    words = _apply_verb_synonyms(words)
    words = [_depluralise(w) for w in words if w and w not in _STOPWORDS]
    return " ".join(words)


def _format_update_line(
    *,
    when: Optional[str],
    speaker: Optional[str],
    source_label: str,
    note: Optional[str],
) -> str:
    """
    Render one entry for the All Updates column. Tagged format:

        [YYYY-MM-DD HH:MM · Person · Source] body

    A single bracketed tag at the start carries everything the reader
    needs to identify *when, by whom, via what channel* — then a
    compact body. Easier to scan than free-form prose.

    Any tag field that's truly unknown is omitted (no "unknown" /
    "(none)" placeholders). The tag is dropped entirely if all three
    fields are blank.
    """
    # Build the tag interior.
    tag_parts: list[str] = []
    if when:
        # Trim ISO 8601 to minute precision: "2026-05-12 04:01".
        ts = when[:16].replace("T", " ").strip()
        if ts:
            tag_parts.append(ts)
    if speaker and speaker.strip():
        tag_parts.append(speaker.strip())
    if source_label and source_label.strip():
        # Compact source: "Email from Aman" -> "Email"; "Google Chat with
        # Manisha" -> "Google Chat"; etc. The full source already lives
        # in the row's Source column; All Updates only needs the channel.
        sl = source_label.strip()
        compact = (
            "Email" if sl.lower().startswith("email") else
            "Google Chat" if "google chat" in sl.lower() else
            "Google Space" if "google space" in sl.lower() else
            "In-person meeting" if "in-person" in sl.lower() else
            sl
        )
        tag_parts.append(compact)

    body = (note or "").strip()
    # Collapse internal whitespace in the body so multi-line LLM output
    # doesn't break the per-update single-line format.
    body = _WS_RE.sub(" ", body)

    if not tag_parts and not body:
        return ""
    if not tag_parts:
        return body
    tag = "[" + " · ".join(tag_parts) + "]"
    return f"{tag} {body}".rstrip() if body else tag


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


# Cap for the source_text column. Long email bodies (newsletters, quoted
# threads) blow up the sheet cell visually; 8000 chars is roughly two
# pages of plain text — enough to keep useful context without exploding
# the Sheet row height.
_SOURCE_TEXT_MAX_CHARS = 8000


def _truncate_source_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    if len(s) <= _SOURCE_TEXT_MAX_CHARS:
        return s
    return s[:_SOURCE_TEXT_MAX_CHARS].rstrip() + "\n... [truncated]"


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
        body_text: Optional[str] = None,
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
            source_text=_truncate_source_text(body_text),
            transcription_accuracy=None,           # text — no STT involved
            accuracy_explanation=None,
        )

    def save_meeting_tasks(
        self,
        *,
        session_id: str,
        chunk_index: int,
        chunk_summary: Optional[str],
        tasks: Iterable[ExtractedTask],
        started_at: Optional[str] = None,
        transcript_text: Optional[str] = None,
        transcription_accuracy: Optional[int] = None,
        accuracy_explanation: Optional[str] = None,
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
            source_text=_truncate_source_text(transcript_text),
            transcription_accuracy=transcription_accuracy,
            accuracy_explanation=accuracy_explanation,
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
        message_text: Optional[str] = None,
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
            source_text=_truncate_source_text(message_text),
            transcription_accuracy=None,           # text — no STT involved
            accuracy_explanation=None,
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
        source_text: Optional[str] = None,
        transcription_accuracy: Optional[int] = None,
        accuracy_explanation: Optional[str] = None,
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
                source_text=source_text,
                transcription_accuracy=transcription_accuracy,
                accuracy_explanation=accuracy_explanation,
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
