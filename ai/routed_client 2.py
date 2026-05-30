"""
Priority-routed LLM client.

Wraps multiple provider clients (Gemini / Groq / Ollama) in priority
order. Each `complete()` call:

  1. Tries the highest-priority provider that isn't currently in a
     known-paused window.
  2. If it raises `QuotaExhaustedError`, parses the pause duration from
     the error message (or uses a sane default) and **records the
     pause locally** so subsequent calls in the next ~17 min skip it
     silently instead of hammering the same exhausted endpoint.
  3. Falls through to the next-priority provider and tries again.
  4. As soon as the recorded pause window passes, the higher-priority
     provider is tried first again automatically — no manual reset.

Net behaviour: always uses the best available provider; degrades
gracefully on quota; recovers automatically.

Configuration:
    LLM_PROVIDER=gemini,groq,ollama        # in .env, comma-separated.
    The chain is left-to-right priority. Single-value (the default,
    e.g. `LLM_PROVIDER=gemini`) returns the underlying client directly
    without any routing wrapper — zero behavioural change for users
    who don't opt in.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

from utils.logger import get_logger

from .errors import QuotaExhaustedError

logger = get_logger(__name__)

# When a QuotaExhaustedError doesn't tell us how long to pause, assume
# a conservative 5 minutes. Better to under-pause and re-check than to
# over-pause and miss a recovery window.
_DEFAULT_PAUSE_SECONDS = 300

# Recheck a paused provider at least every this often, even if its
# parsed pause is longer. Prevents being stuck if the provider's
# quota is reset early (e.g. you bought more credits, or the daily
# rolling window ticked over before our parsed estimate).
_MAX_PAUSE_SECONDS = 30 * 60

# Capture "paused for 1056s." / "paused for 1056 seconds" / "paused for 5 min"
_PAUSE_RE = re.compile(
    r"paused\s+(?:for\s+)?(?:another\s+)?(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?)?",
    re.IGNORECASE,
)


def _parse_pause_seconds(message: str) -> int:
    """Pull a "paused for Xs/Xmin" duration out of an error message."""
    if not message:
        return _DEFAULT_PAUSE_SECONDS
    m = _PAUSE_RE.search(message)
    if not m:
        return _DEFAULT_PAUSE_SECONDS
    val = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit.startswith("m"):
        val *= 60
    return max(60, min(_MAX_PAUSE_SECONDS, int(val)))


class RoutedClient:
    """
    Holds an ordered chain of LLM clients. `.complete()` honours the
    same contract as the individual provider clients.
    """

    def __init__(self, providers: list[tuple[str, object]]) -> None:
        if not providers:
            raise ValueError("RoutedClient needs at least one provider.")
        self._providers = list(providers)  # [(name, client), ...]
        # Monotonic timestamp at which each provider becomes eligible
        # again. 0.0 == eligible right now.
        self._paused_until: dict[str, float] = {n: 0.0 for n, _ in providers}
        self._lock = threading.Lock()
        names = [n for n, _ in providers]
        logger.info(
            "RoutedClient initialised with priority chain: %s", " -> ".join(names)
        )

    def _eligible(self, name: str, now: float) -> bool:
        with self._lock:
            return now >= self._paused_until[name]

    def _mark_paused(self, name: str, seconds: int) -> None:
        with self._lock:
            until = time.monotonic() + seconds
            # Don't shorten an already-longer pause.
            if until > self._paused_until[name]:
                self._paused_until[name] = until
        logger.warning(
            "Provider %r paused for %ds; falling back to next in chain.",
            name, seconds,
        )

    # --- public API ----------------------------------------------------------

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        now = time.monotonic()
        last_exc: Optional[BaseException] = None

        for name, client in self._providers:
            if not self._eligible(name, now):
                logger.debug(
                    "Skipping %r — paused until %.1fs from now.",
                    name,
                    self._paused_until[name] - now,
                )
                continue

            try:
                result = client.complete(
                    system=system,
                    user=user,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                # Success — if we'd previously marked this one as paused,
                # an automatic recovery already happened (the pause window
                # elapsed). Just log a one-liner the first time we
                # successfully use a non-primary provider so the user
                # can see degradation in the logs.
                if name != self._providers[0][0]:
                    logger.info(
                        "Used fallback provider %r (primary in cooldown).", name
                    )
                return result
            except QuotaExhaustedError as exc:
                pause = _parse_pause_seconds(str(exc))
                self._mark_paused(name, pause)
                last_exc = exc
                # Re-fetch `now` so a slow primary doesn't unfairly
                # disqualify the fallback when we check eligibility.
                now = time.monotonic()
                continue
            except Exception:
                # Non-quota failures (network, model-not-pulled, malformed
                # prompt) bubble up immediately — don't silently degrade
                # to a different provider for those, since the failure is
                # likely systematic and the next provider would just hit
                # the same wall.
                raise

        # We exhausted the chain.
        raise QuotaExhaustedError(
            "All LLM providers in the routing chain are quota-exhausted. "
            "Last error: " + (str(last_exc) if last_exc else "no error captured")
        )

    # --- introspection helpers (used by daily summary / health checks) ------

    def status(self) -> list[dict]:
        """Return the current state of each provider in priority order."""
        now = time.monotonic()
        out = []
        with self._lock:
            for name, _ in self._providers:
                pu = self._paused_until[name]
                out.append(
                    {
                        "provider": name,
                        "available": now >= pu,
                        "paused_for_seconds": max(0, int(pu - now)),
                    }
                )
        return out
