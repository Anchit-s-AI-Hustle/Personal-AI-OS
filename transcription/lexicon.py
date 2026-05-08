"""
Domain lexicon for the transcription pipeline.

Two jobs:

1. `WHISPER_PROMPT` is fed to Whisper as a prompt-bias so the model
   tokenises Anchit/Aman/Vahdam/Klaviyo/etc. correctly the first time.
   This is the BIG win for name accuracy — Whisper's prompt parameter
   is essentially a vocabulary hint.

2. `KNOWN_PEOPLE` is a canonical-name → aliases mapping for cleanup
   AFTER transcription. If Whisper still hears "Anshith" or "Maneesha",
   `correct_names()` rewrites it to "Anchit" / "Manisha" before the
   LLM (or the sheet) ever sees it.

To extend: just add to KNOWN_PEOPLE / EXTRA_VOCAB / BRAND_TERMS below.
No code changes needed.
"""
from __future__ import annotations

import re
from typing import Iterable

# ---------------------------------------------------------------------------
# Edit these to keep accuracy high on YOUR vocabulary.
# Aliases must be lowercased; matching is case-insensitive with word
# boundaries.
# ---------------------------------------------------------------------------

KNOWN_PEOPLE: dict[str, list[str]] = {
    # Anchit (the user himself)
    "Anchit": [
        "anshith",
        "anshit",
        "ancheet",
        "anchet",
        "anchut",
        "anchitt",
        "onchit",
        "inchit",
        "aanchit",
        "aanchet",
        "ankit",       # common Indian name that sounds very similar
        "ankeet",
        "aamchit",
        "anchik",
        "aamchit",
        "aanshit",
    ],
    # Direct team
    "Aman": [
        "amaan",
        "aamen",
        "ahmen",
        "amann",
        "ahmaan",
        "ammon",
        "ammen",
    ],
    "Manisha": [
        "maneesha",
        "manesha",
        "maneeshaa",
        "manishaa",
        "maneeshia",
        "munisha",
        "monisha",
    ],
    "Arihant": [
        "arihaan",
        "arihaant",
        "aarihant",
        "ari haant",
        "arvint",
        "ari hand",
        "ari ant",
        "arah ant",
    ],
    # Other names that show up frequently
    "Aakash": [
        "akash",
        "aakaash",
        "akkash",
        "aakaash",
    ],
    "Shehzad": [
        "shezad",
        "shahzad",
        "sehzad",
        "shahzed",
    ],
    "Lavanya": [
        "lavania",
        "lavanyaa",
    ],
    "Akshay": [
        "akshey",
        "akshaye",
    ],
    "Plaban": [
        "plabaan",
        "palaban",
    ],
}


# Brand / product / channel vocabulary that helps Whisper not butcher
# domain terms. These also get auto-fed via the WHISPER_PROMPT.
BRAND_TERMS = [
    "Vahdam",
    "Vahdam India",
    "Vahdam Teas",
    "Anchit Tandon",
]

PRODUCT_TERMS = [
    "Black Tea",
    "Green Tea",
    "Honey Lemon",
    "Turmeric Ginger",
    "Ashwagandha",
]

CHANNEL_TERMS = [
    "Shopify",
    "Amazon",
    "Amazon US",
    "Amazon IN",
    "Klaviyo",
    "Meta Ads",
    "Google Ads",
    "Flipkart",
    "Instagram",
    "WhatsApp",
]

GROWTH_TERMS = [
    "CAC",
    "LTV",
    "AOV",
    "RTO",
    "PDP",
    "CRO",
    "ROAS",
    "D2C",
    "SKU",
    "OOS",
    "RFQ",
]


# ---------------------------------------------------------------------------
# Public helpers — used by groq_whisper / meeting_service / chat_service
# ---------------------------------------------------------------------------


def all_canonical_names() -> list[str]:
    return list(KNOWN_PEOPLE.keys())


def whisper_prompt() -> str:
    """
    Build the prompt-string fed to Whisper. ~200-300 chars max is ideal —
    too long and Whisper deprioritises later words.
    """
    parts = (
        list(KNOWN_PEOPLE.keys())
        + BRAND_TERMS
        + PRODUCT_TERMS
        + CHANNEL_TERMS
        + GROWTH_TERMS
    )
    return ", ".join(parts) + "."


# Pre-compile a single regex per canonical name that matches any alias
# (case-insensitive, word-boundary). Building it once is much cheaper
# than rebuilding per call.
def _build_alias_regex() -> list[tuple[re.Pattern, str]]:
    out: list[tuple[re.Pattern, str]] = []
    for canonical, aliases in KNOWN_PEOPLE.items():
        if not aliases:
            continue
        # Sort longest-first so multi-word aliases match before single-word.
        sorted_aliases = sorted(set(aliases), key=len, reverse=True)
        # \b word boundaries; re.escape protects punctuation.
        pattern = (
            r"\b("
            + "|".join(re.escape(a) for a in sorted_aliases)
            + r")\b"
        )
        out.append((re.compile(pattern, flags=re.IGNORECASE), canonical))
    return out


_ALIAS_REGEXES = _build_alias_regex()


def correct_names(text: str) -> str:
    """Replace every known alias with its canonical name."""
    if not text:
        return text
    for rx, canonical in _ALIAS_REGEXES:
        text = rx.sub(canonical, text)
    return text


def correct_names_in_fields(fields: Iterable[str | None]) -> list[str | None]:
    """Vectorised version for a row of strings (some may be None)."""
    return [correct_names(f) if isinstance(f, str) else f for f in fields]
