"""
Gmail polling loop.

Runs in its own thread. Every `POLLING_INTERVAL` seconds it lists
messages matching `GMAIL_QUERY_FILTER`, skips ones we've already seen,
and forwards new ones to a callback.

On first launch (when `INITIAL_SCAN_DAYS > 0` and the sentinel file is
missing) it does a one-time historical sweep before transitioning to
the normal polling cadence.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional

from config import settings
from database import get_db
from utils.logger import get_logger

from .client import GmailClient, GmailMessage, get_gmail_client

logger = get_logger(__name__)

OnMessage = Callable[[GmailMessage], None]


class GmailPoller(threading.Thread):
    def __init__(
        self,
        on_message: OnMessage,
        stop_event: threading.Event,
        client: Optional[GmailClient] = None,
        interval: Optional[int] = None,
        query: Optional[str] = None,
    ) -> None:
        super().__init__(name="GmailPoller", daemon=True)
        self._on_message = on_message
        self._stop = stop_event
        self._client = client or get_gmail_client()
        self._interval = interval or settings.polling_interval
        self._query = query or settings.gmail_query_filter
        self._db = get_db()
        self._sentinel = settings.database_path.parent / ".initial_scan_done"

    def run(self) -> None:  # pragma: no cover
        logger.info(
            "GmailPoller started (interval=%ss, live query=%r)",
            self._interval,
            self._query,
        )

        # 1. One-time historical scan, if armed and not yet done.
        try:
            self._maybe_initial_scan()
        except Exception:
            logger.exception("Initial historical scan crashed; continuing with live poll.")

        # 2. Normal live-poll loop.
        while not self._stop.is_set():
            try:
                self._tick(self._query, max_results=100)
            except Exception:
                logger.exception("Gmail poll cycle crashed; will retry next interval.")
                self._db.log_event("ERROR", "gmail.poller", "Poll cycle crashed")
            for _ in range(self._interval):
                if self._stop.is_set():
                    break
                time.sleep(1)
        logger.info("GmailPoller stopped.")

    # --- one-time historical scan -------------------------------------------

    def _maybe_initial_scan(self) -> None:
        days = settings.initial_scan_days
        if days <= 0:
            return
        if self._sentinel.exists():
            logger.debug("Initial scan sentinel present; skipping.")
            return

        # Ignore the live filter — we want EVERYTHING in the window, including
        # already-read mail.
        scan_query = f"newer_than:{days}d in:inbox"
        max_msgs = settings.initial_scan_max_messages
        logger.info(
            "Initial historical scan: query=%r, max_messages=%d (this may take a while)",
            scan_query,
            max_msgs,
        )
        try:
            processed = self._tick(scan_query, max_results=max_msgs)
            logger.info("Initial historical scan complete: %d new message(s) processed.", processed)
            self._sentinel.parent.mkdir(parents=True, exist_ok=True)
            self._sentinel.write_text(
                f"Initial scan completed at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Query: {scan_query}\n",
                encoding="utf-8",
            )
        except Exception:
            logger.exception(
                "Initial historical scan failed mid-flight; sentinel NOT written so it will retry next boot."
            )
            raise

    # --- shared list-and-process helper -------------------------------------

    def _tick(self, query: str, max_results: int) -> int:
        ids = self._client.list_message_ids(query, max_results=max_results)
        if not ids:
            logger.debug("Gmail poll: no messages match %r.", query)
            return 0

        new_ids = [
            (mid, tid) for mid, tid in ids if not self._db.email_already_processed(mid)
        ]
        if not new_ids:
            logger.debug("Gmail poll: %d match, all already processed.", len(ids))
            return 0

        logger.info(
            "Gmail poll: %d total / %d new message(s) to process (query=%r).",
            len(ids),
            len(new_ids),
            query,
        )
        processed = 0
        for mid, _tid in new_ids:
            if self._stop.is_set():
                return processed
            try:
                msg = self._client.fetch_message(mid)
                self._on_message(msg)
                processed += 1
            except Exception:
                logger.exception("Failed to process Gmail message id=%s", mid)
                self._db.log_event(
                    "ERROR", "gmail.poller", f"Failed to process {mid}"
                )
        return processed
