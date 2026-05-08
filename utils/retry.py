"""
Retry helper built on tenacity.

`retry_call` runs `fn` with exponential backoff. The optional
`should_retry` predicate gives callers fine-grained control: if it
returns False for a given exception, the retry loop bails immediately
and re-raises. This is how we avoid hammering the API with retries when
the failure is permanent (e.g. SERVICE_DISABLED, quota=0).
"""
from __future__ import annotations

from typing import Callable, Optional, TypeVar

from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .logger import get_logger

T = TypeVar("T")
logger = get_logger(__name__)


def _is_retryable_default(exc: BaseException, allowed: tuple) -> bool:
    return isinstance(exc, allowed)


def retry_call(
    fn: Callable[..., T],
    *args,
    attempts: int = 5,
    base: float = 1.0,
    max_wait: float = 60.0,
    exceptions: tuple = (Exception,),
    should_retry: Optional[Callable[[BaseException], bool]] = None,
    **kwargs,
) -> T:
    """
    Run `fn(*args, **kwargs)` with exponential backoff.

    Args:
        attempts:     max attempts before giving up
        base:         base sleep multiplier (seconds)
        max_wait:     cap on the inter-attempt sleep
        exceptions:   tuple of exception types that are candidates for retry
        should_retry: optional predicate. If supplied AND it returns False
                      for an exception that would otherwise be retryable,
                      the retry stops immediately. Use this to skip retries
                      on permanent failures (4xx errors, quota=0, etc.)
    """
    def _is_retryable(exc: BaseException) -> bool:
        if not _is_retryable_default(exc, exceptions):
            return False
        if should_retry is not None and not should_retry(exc):
            return False
        return True

    retrying = Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=base, max=max_wait),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
        before_sleep=before_sleep_log(logger, 30),  # WARNING
    )
    for attempt in retrying:
        with attempt:
            return fn(*args, **kwargs)
    raise RuntimeError("retry_call exited without producing a value")
