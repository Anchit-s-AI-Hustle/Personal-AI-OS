"""
Google Chat API client.

Reads spaces (DMs, group chats, named spaces) and their messages on
behalf of the authenticated user. Re-uses the same OAuth credentials
as Gmail/Sheets — Chat-related scopes are added in config/settings.

Workspace admin policy may block this API for end-user OAuth apps.
The poller catches HTTP 403 / 503 and degrades silently in that case.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gmail.auth import get_credentials
from utils.logger import get_logger
from utils.retry import retry_call

logger = get_logger(__name__)


def _is_transient_http_error(exc: BaseException) -> bool:
    """Don't retry permanent failures (4xx). Retry transient (5xx, network)."""
    if isinstance(exc, HttpError):
        status = exc.resp.status if exc.resp else 0
        # 401 / 403 / 404 / 410 are permanent for our purposes — don't retry.
        if 400 <= status < 500:
            return False
    return True


@dataclass
class ChatMessage:
    name: str               # full resource name: spaces/AAAA/messages/BBBB.CCCC
    space_name: str         # spaces/AAAA
    space_type: str         # DIRECT_MESSAGE | GROUP_CHAT | SPACE
    space_display: str      # human-readable space title (or "" for DMs)
    sender_display: str     # who sent it
    sender_email: str       # email if available
    create_time: str        # ISO 8601
    text: str
    thread_name: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def message_id(self) -> str:
        # The trailing component after the last `/`, used as a stable id.
        return self.name.split("/")[-1] if self.name else ""


class ChatClient:
    def __init__(self) -> None:
        creds = get_credentials()
        self._svc = build("chat", "v1", credentials=creds, cache_discovery=False)

    # --- spaces --------------------------------------------------------------

    def list_spaces(self) -> list[dict]:
        """Return all spaces the user belongs to (incl. DMs and group chats)."""

        def _call() -> list[dict]:
            spaces: list[dict] = []
            page_token: Optional[str] = None
            while True:
                resp = (
                    self._svc.spaces()
                    .list(pageSize=100, pageToken=page_token)
                    .execute()
                )
                spaces.extend(resp.get("spaces", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    return spaces

        return retry_call(
            _call,
            attempts=3,
            exceptions=(HttpError, TimeoutError),
            should_retry=_is_transient_http_error,
        )

    # --- messages ------------------------------------------------------------

    def iter_messages(
        self,
        space_name: str,
        *,
        since_iso: Optional[str] = None,
        max_messages: int = 200,
    ) -> Iterator[dict]:
        """
        Yield messages in `space_name` newest-first, optionally filtered to
        items created at or after `since_iso` (RFC 3339).
        """
        page_token: Optional[str] = None
        yielded = 0
        # The Chat API supports `filter="createTime > <RFC3339>"`.
        msg_filter = None
        if since_iso:
            msg_filter = f'createTime > "{since_iso}"'

        while yielded < max_messages:
            kwargs = {
                "parent": space_name,
                "pageSize": min(100, max_messages - yielded),
                "orderBy": "createTime desc",
            }
            if page_token:
                kwargs["pageToken"] = page_token
            if msg_filter:
                kwargs["filter"] = msg_filter

            def _call() -> dict:
                return self._svc.spaces().messages().list(**kwargs).execute()

            resp = retry_call(_call, attempts=3, exceptions=(HttpError, TimeoutError))
            messages = resp.get("messages", [])
            for m in messages:
                yield m
                yielded += 1
                if yielded >= max_messages:
                    return
            page_token = resp.get("nextPageToken")
            if not page_token:
                return

    # --- mapping -------------------------------------------------------------

    def to_chat_message(self, raw: dict, space_meta: dict) -> ChatMessage:
        sender = raw.get("sender") or {}
        # `sender.name` looks like 'users/USER_ID' — that's an internal
        # resource path, NOT a name. Use ONLY `displayName` as the human
        # label; if it's missing, leave sender_display empty so downstream
        # code knows we don't have a real name. Never leak the raw
        # resource path into the sheet.
        display = (sender.get("displayName") or "").strip()
        sender_display = display  # may be empty by design
        # Email is rarely populated in Chat responses; only keep it if
        # it actually looks like an email.
        raw_email = (sender.get("email") or "").strip()
        sender_email = raw_email if "@" in raw_email else ""

        return ChatMessage(
            name=raw.get("name", ""),
            space_name=space_meta.get("name", ""),
            space_type=(space_meta.get("spaceType") or space_meta.get("type") or "").upper(),
            space_display=(
                space_meta.get("displayName")
                or space_meta.get("spaceDetails", {}).get("name", "")
                or ""
            ),
            sender_display=sender_display,
            sender_email=sender_email,
            create_time=raw.get("createTime", ""),
            text=raw.get("text", "") or raw.get("formattedText", ""),
            thread_name=(raw.get("thread") or {}).get("name", ""),
            raw=raw,
        )


_singleton: Optional[ChatClient] = None
_singleton_lock = threading.Lock()


def get_chat_client() -> ChatClient:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = ChatClient()
    return _singleton
