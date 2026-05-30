"""
Lightweight LLM orchestrator helper.
Provides a per-app ordered chain based on LLM_PROVIDER (comma chain) and
optional per-app overrides via environment variables (LLM_ORDER_<APP>).
This is intentionally small: this repo already has a RoutedClient that
performs quota-based fallback; this module centralises ordering rules
and personalization for downstream apps.
"""
from __future__ import annotations

import os
from typing import List


def _env_list(key: str) -> List[str]:
    raw = os.getenv(key, "")
    return [p.strip() for p in raw.split(",") if p.strip()]


class LLMOrchestrator:
    """Resolve the ordered provider chain for an application."""

    def __init__(self, default_chain: List[str]):
        # canonical default chain, e.g. ['gemini','groq','ollama']
        self.default_chain = [p.lower() for p in default_chain]

    def get_chain_for(self, app_name: str) -> List[str]:
        """Return the provider chain for `app_name`.
        Priority:
          1. LLM_ORDER_<UPPER_APP_NAME> env var (comma list)
          2. LLM_PROVIDER (global chain)
          3. fallback to the default passed at init
        """
        env_key = f"LLM_ORDER_{app_name.upper()}"
        chain = _env_list(env_key)
        if chain:
            return [p.lower() for p in chain]

        global_chain = _env_list("LLM_PROVIDER")
        if global_chain:
            return [p.lower() for p in global_chain]

        return list(self.default_chain)


# Simple singleton convenience: constructed with a sensible default.
default_orchestrator = LLMOrchestrator(["gemini", "groq", "ollama"])