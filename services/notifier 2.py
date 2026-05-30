"""
Outbound Gmail notifier.

Sends a daily digest of newly-captured Vahdam tasks to the user's own
inbox via the Gmail API. The same OAuth credentials used to read mail
are reused; the only added scope is `gmail.send`.

The digest body is plain text + an HTML version. Each task line carries
its source link so the user can jump straight to the originating
message/thread/audio session.
"""
from __future__ import annotations

import base64
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Iterable, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings
from gmail.auth import get_credentials
from utils.logger import get_logger
from utils.retry import retry_call

logger = get_logger(__name__)


def _row_get(row, key: str) -> str:
    """sqlite3.Row safe-getter that tolerates missing columns."""
    try:
        v = row[key]
    except (IndexError, KeyError):
        return ""
    return v if v is not None else ""


def _format_text_line(t) -> str:
    bits: list[str] = []
    bits.append(f"[{_row_get(t, 'urgency') or 'Medium'}] {_row_get(t, 'task')}")
    deadline = _row_get(t, "deadline")
    if deadline:
        bits.append(f"due: {deadline}")
    spoc = _row_get(t, "sender_or_speaker")
    if spoc:
        bits.append(f"SPOC: {spoc}")
    src_type = _row_get(t, "source_type")
    src_detail = _row_get(t, "source_detail")
    src = f"{src_type} | {src_detail}" if src_detail else src_type
    if src:
        bits.append(src)
    line = " — ".join(bits)
    link = _row_get(t, "source_link")
    if link:
        line += f"\n  link: {link}"
    desc = _row_get(t, "task_description")
    if desc:
        line += f"\n  context: {desc}"
    return line


def _format_html_row(t) -> str:
    urgency = escape(_row_get(t, "urgency") or "Medium")
    heading = escape(_row_get(t, "task"))
    desc = escape(_row_get(t, "task_description"))
    deadline = escape(_row_get(t, "deadline"))
    spoc = escape(_row_get(t, "sender_or_speaker"))
    src_type = _row_get(t, "source_type")
    src_detail = _row_get(t, "source_detail")
    src = escape(f"{src_type} | {src_detail}" if src_detail else src_type)
    link = _row_get(t, "source_link")
    link_html = (
        f'<a href="{escape(link, quote=True)}">open source</a>' if link else ""
    )
    meta_parts = [p for p in (deadline and f"due {deadline}", spoc and f"SPOC: {spoc}", src) if p]
    meta = " · ".join(meta_parts)
    return (
        f'<li style="margin-bottom:10px;">'
        f'<b>[{urgency}]</b> {heading}<br>'
        f'<span style="color:#555;font-size:13px;">{meta}'
        + (f' · {link_html}' if link_html else "")
        + "</span>"
        + (f'<br><span style="color:#333;font-size:13px;">{desc}</span>' if desc else "")
        + "</li>"
    )


def _build_message(
    *,
    sender: str,
    recipient: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> dict:
    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return {"raw": raw}


class GmailNotifier:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._svc = None  # lazy

    def _service(self):
        if self._svc is None:
            with self._lock:
                if self._svc is None:
                    creds = get_credentials()
                    self._svc = build(
                        "gmail", "v1", credentials=creds, cache_discovery=False
                    )
        return self._svc

    def send_task_digest(
        self,
        tasks: Iterable,
        *,
        date_str: str,
        recipient: Optional[str] = None,
    ) -> bool:
        """
        Send a digest of `tasks` (list of sqlite3.Row from extracted_tasks).

        Returns True on success, False on any handled failure (auth, quota,
        empty list). Never raises — notifier failures must not crash the
        daily summary worker.
        """
        if not settings.enable_notifications:
            logger.debug("Notifications disabled; skipping digest.")
            return False

        to_addr = (recipient or settings.notification_recipient or "").strip()
        if not to_addr:
            logger.warning(
                "No NOTIFICATION_RECIPIENT configured; skipping Gmail digest."
            )
            return False

        task_list = list(tasks)
        if not task_list:
            logger.info("No new tasks for %s; skipping digest email.", date_str)
            return False

        text_lines = [
            f"Vahdam task digest — {date_str}",
            f"{len(task_list)} task(s) captured.",
            "",
        ]
        text_lines.extend(_format_text_line(t) for t in task_list)
        text_body = "\n\n".join(text_lines)

        html_body = (
            f'<div style="font-family:-apple-system,Segoe UI,sans-serif;'
            f'max-width:720px;color:#222;">'
            f'<h2 style="margin-bottom:4px;">Vahdam task digest — {escape(date_str)}</h2>'
            f'<div style="color:#666;margin-bottom:16px;">'
            f"{len(task_list)} task(s) captured today."
            f"</div>"
            f'<ul style="padding-left:18px;">'
            + "".join(_format_html_row(t) for t in task_list)
            + "</ul></div>"
        )

        subject = f"Vahdam tasks · {date_str} · {len(task_list)} new"
        body = _build_message(
            sender="me",
            recipient=to_addr,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )

        try:
            def _call() -> dict:
                return (
                    self._service()
                    .users()
                    .messages()
                    .send(userId="me", body=body)
                    .execute()
                )

            resp = retry_call(_call, attempts=3, exceptions=(HttpError, TimeoutError))
            logger.info(
                "Sent task digest to %s (%d tasks, gmail id=%s).",
                to_addr,
                len(task_list),
                (resp or {}).get("id"),
            )
            return True
        except HttpError as exc:
            logger.warning("Gmail send failed (HTTP): %s", exc)
        except Exception:
            logger.exception("Gmail send failed unexpectedly.")
        return False


_singleton: Optional[GmailNotifier] = None
_singleton_lock = threading.Lock()


def get_notifier() -> GmailNotifier:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = GmailNotifier()
    return _singleton
