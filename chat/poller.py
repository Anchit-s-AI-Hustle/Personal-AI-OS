"""
Google Chat polling loop.

Walks every space the user belongs to (DMs, group chats, named spaces)
and forwards new messages to a callback. Tracks per-space "high water
mark" timestamp in SQLite so each subsequent poll only fetches what's
new.

Fails soft: if Workspace policy blocks the Chat API, the thread logs
once and exits cleanly. The rest of the system keeps working.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from googleapiclient.errors import HttpError

from ai.gemini_client import QuotaExhaustedError
from config import settings
from database import get_db
from utils.logger import get_logger

from .client import ChatClient, ChatMessage, get_chat_client

logger = get_logger(__name__)

OnChatMessage = Callable[[ChatMessage], None]


class ChatPoller(threading.Thread):
    def __init__(
        self,
        on_message: OnChatMessage,
        stop_event: threading.Event,
        client: Optional[ChatClient] = None,
        interval: Optional[int] = None,
    ) -> None:
        super().__init__(name="ChatPoller", daemon=True)
        self._on_message = on_message
        self._stop = stop_event
        self._client = client or get_chat_client()
        self._interval = interval or settings.chat_polling_interval
        self._db = get_db()
        self._disabled_reason: Optional[str] = None

    # --- thread entrypoint ---------------------------------------------------

    def run(self) -> None:  # pragma: no cover
        if not settings.enable_chat_poller:
            logger.info("ChatPoller disabled by config; exiting.")
            return

        logger.info("ChatPoller started (interval=%ss)", self._interval)
        # Probe once at startup. If the API isn't accessible, the poller
        # disables itself but doesn't kill the process.
        if not self._probe():
            return

        while not self._stop.is_set():
            try:
                self._tick()
            except HttpError as exc:
                logger.warning("Chat poll cycle failed (%s); will retry.", exc)
                self._db.log_event("WARNING", "chat.poller", f"HttpError {exc.resp.status if exc.resp else '?'}")
            except Exception:
                logger.exception("Chat poll cycle crashed; will retry.")
                self._db.log_event("ERROR", "chat.poller", "Poll cycle crashed")
            for _ in range(self._interval):
                if self._stop.is_set():
                    break
                time.sleep(1)
        logger.info("ChatPoller stopped.")

    # --- probe ---------------------------------------------------------------

    def _probe(self) -> bool:
        """Test the API with a single list_spaces call. Returns False if blocked."""
        try:
            self._client.list_spaces()
            return True
        except HttpError as exc:
            status = exc.resp.status if exc.resp else "?"
            body = (exc.content or b"").decode("utf-8", errors="replace")[:300]
            if status in (401, 403):
                logger.warning(
                    "Chat API blocked (HTTP %s). This usually means your "
                    "Workspace admin disallows third-party Chat apps for "
                    "user OAuth. Chat polling is disabled for this run. "
                    "Detail: %s",
                    status,
                    body,
                )
                self._disabled_reason = f"http {status}"
                self._db.log_event("WARNING", "chat.poller", f"Disabled: {body[:200]}")
                return False
            if status in (501, 503):
                logger.warning(
                    "Chat API not available (HTTP %s); will retry on next interval.",
                    status,
                )
                return True  # transient — let the run loop retry
            logger.exception("Chat API probe failed with HTTP %s", status)
            return False
        except Exception:
            logger.exception("Chat API probe crashed.")
            return False

    # --- one poll cycle ------------------------------------------------------

    def _tick(self) -> None:
        spaces = self._client.list_spaces()
        if not spaces:
            return

        total_new = 0
        for space in spaces:
            if self._stop.is_set():
                return
            space_name = space.get("name", "")
            if not space_name:
                continue

            since = self._high_water_mark(space_name)
            try:
                messages = list(
                    self._client.iter_messages(
                        space_name, since_iso=since, max_messages=100
                    )
                )
            except HttpError as exc:
                # E.g. 403 on a single locked space — keep going with others.
                logger.debug(
                    "Skipping %s (%s): %s",
                    space.get("displayName") or space_name,
                    space.get("spaceType"),
                    exc,
                )
                continue

            if not messages:
                continue

            # Iterate oldest -> newest so high-water-mark advances monotonically.
            messages.sort(key=lambda m: m.get("createTime", ""))
            for raw in messages:
                if self._stop.is_set():
                    return
                msg = self._client.to_chat_message(raw, space)
                # Skip ones we've already seen (defensive — the filter
                # should have already excluded them).
                if self._db.email_already_processed(msg.message_id):
                    continue
                if not msg.text or not msg.text.strip():
                    # Skip pure-attachment / sticker messages with no text.
                    continue
                try:
                    self._on_message(msg)
                    total_new += 1
                except QuotaExhaustedError:
                    logger.warning(
                        "Chat poll halted: Gemini quota exhausted (processed %d so far).",
                        total_new,
                    )
                    return
                except Exception:
                    logger.exception("Failed to process chat message %s", msg.name)
                    continue
                # Advance high-water mark after each successful processing
                # so a crash mid-batch doesn't reprocess what we've done.
                self._set_high_water_mark(space_name, msg.create_time)

        if total_new:
            logger.info("Chat poll: %d new message(s) processed.", total_new)

    # --- per-space high-water-mark store -------------------------------------

    def _high_water_mark(self, space_name: str) -> Optional[str]:
        row = self._db.fetchone(
            "SELECT message FROM processing_logs WHERE component = ? AND level = 'WATERMARK' "
            "ORDER BY created_at DESC LIMIT 1",
            (f"chat.{space_name}",),
        )
        return row["message"] if row else None

    def _set_high_water_mark(self, space_name: str, iso_ts: str) -> None:
        if not iso_ts:
            return
        self._db.log_event(
            "WATERMARK",
            f"chat.{space_name}",
            iso_ts,
        )
