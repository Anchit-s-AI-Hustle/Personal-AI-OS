"""
Shared exceptions across LLM providers.

Putting these in a provider-neutral module so the email/chat poller
imports stay stable when LLM_PROVIDER flips between gemini and groq.
"""
from __future__ import annotations


class QuotaExhaustedError(RuntimeError):
    """
    Raised when the configured LLM key has run out of (or never had)
    quota. Non-retryable — the caller should halt the current batch
    and let the client's pause window elapse.
    """
