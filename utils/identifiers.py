"""
Identifier hygiene.

The sheet should only ever surface real-world identifiers — names,
email addresses, phone numbers — never opaque internal IDs from upstream
APIs (Google Chat's `users/12345`, generic UUIDs, etc.). This module
centralises the predicate so the chat client, chat service, and task
service all agree on what counts as a "real" identifier.
"""
from __future__ import annotations

import re
from typing import Optional

# Patterns we consider placeholders rather than real identifiers.
#
# - "users/12345..." or "users/<base64>"      — Google Chat API resource name
# - "user-XXXX" / "user_XXXX" / "userXXXX"    — generic UI placeholders
# - "(unknown)" / "unknown"                   — explicit "we don't know"
# - bare UUID-ish hex strings                 — internal IDs leaking through
# - empty / whitespace                        — obvious
_PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*users?\s*[/\-_]\s*\S+\s*$", re.IGNORECASE),
    re.compile(r"^\s*\(?\s*unknown\s*\)?\s*$", re.IGNORECASE),
    re.compile(r"^\s*n\s*/?\s*a\s*$", re.IGNORECASE),
    # 24+ hex/alphanumeric chars with no spaces and no @ — looks like an ID, not a name
    re.compile(r"^\s*[A-Za-z0-9_\-]{24,}\s*$"),
)

_REAL_NAME_HINTS = re.compile(r"[A-Za-z]{2,}")


def is_placeholder_identifier(value: Optional[str]) -> bool:
    """
    Return True if `value` looks like an opaque ID rather than a real
    name / email / phone number. Conservative: returns False on anything
    that contains an `@` (likely email) or `+` followed by digits (likely
    phone) so we don't accidentally drop genuine contacts.
    """
    if value is None:
        return True
    s = str(value).strip()
    if not s:
        return True

    # Anything resembling a real email / phone number is real.
    if "@" in s and "." in s:
        return False
    if re.match(r"^\s*\+?\d[\d\s\-().]{6,}\s*$", s):
        return False

    for pat in _PLACEHOLDER_PATTERNS:
        if pat.match(s):
            return True

    # Reject strings with no alphabetic run of length 2+ (digits-only,
    # punctuation-only) when they aren't phone-shaped per the check above.
    if not _REAL_NAME_HINTS.search(s):
        return True

    return False


def clean_identifier(value: Optional[str]) -> Optional[str]:
    """Return a stripped version of `value` if it's real; else None."""
    if is_placeholder_identifier(value):
        return None
    return str(value).strip()
