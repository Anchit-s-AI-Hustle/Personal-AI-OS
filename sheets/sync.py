"""
Background worker that flushes locally-stored tasks to Google Sheets.

The DB is the source of truth, the sheet is the surface. Each task is
dual-written: once into its source-specific tab and once into the
interactive "Checklist Tracker" tab. Existing rows are patched in place
by hidden task id so reminder replies and manual edits do not create
duplicates.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from database import get_db
from utils.logger import get_logger

from .client import (
    SheetsClient,
    TAB_ALL_TASKS,
    TAB_ALL_TASKS_DETAIL,
    get_sheets_client,
    source_tab_for,
)
from .excel_mirror import ExcelMirror, get_excel_mirror

logger = get_logger(__name__)

SYNC_INTERVAL_SECONDS = 30
BATCH_SIZE = 50


def _ordinal_suffix(day: int) -> str:
    if 11 <= (day % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _format_iso_timestamp(value: Optional[str]) -> str:
    from datetime import datetime as _datetime

    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""

    date_only = len(s) == 10 and s[4] == "-" and s[7] == "-"
    try:
        dt = _datetime.fromisoformat(s)
    except ValueError:
        return s

    if dt.tzinfo is not None:
        dt = dt.astimezone()

    day = dt.day
    suffix = _ordinal_suffix(day)
    month = dt.strftime("%B")
    year = dt.year

    if date_only:
        return f"{day}{suffix} {month} {year}"

    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{day}{suffix} {month} {year}, {hour12}:{dt.minute:02d} {ampm}"


def _format_source_label(*, source_type: str, source_detail: str) -> str:
    st = (source_type or "").strip()
    sd = (source_detail or "").strip()
    sl = sd.lower()

    if st.lower() == "email":
        if sl.startswith("from "):
            return f"Email from {sd[5:].strip()}"
        return "Email"

    if st.lower() == "chat":
        if sl.startswith("dm with "):
            return f"Google Chat with {sd[8:].strip()}"
        if sl == "dm":
            return "Google Chat (DM)"
        if sl.startswith("group:"):
            return f"Google Chat group: {sd.split(':', 1)[1].strip()}"
        if sl.startswith("space:"):
            return f"Google Space: {sd.split(':', 1)[1].strip()}"
        if sl in ("google chat", "chat") or not sd:
            return "Google Chat"
        return f"Google Chat - {sd}"

    if st.lower() == "meeting":
        return "Voice memo"

    if sd:
        return f"{st or 'Source'} - {sd}"
    return st or "Source"


def _row_for_task(task) -> list[object]:
    keys = set(task.keys())

    def get(k: str) -> str:
        if k in keys:
            v = task[k]
            return v if v is not None else ""
        return ""

    source_label = _format_source_label(
        source_type=get("source_type"),
        source_detail=get("source_detail"),
    )
    date_given = get("date_given") or get("created_at")
    sort_key = date_given or get("created_at") or ""
    status = (get("status") or "open").strip().lower()

    # Accuracy rating: 0-100 for audio-sourced rows, empty for text rows.
    # Text sources (email / chat) are not "transcribed" — leave the
    # rating cell blank so the gradient doesn't paint them.
    raw_acc = get("transcription_accuracy")
    if raw_acc == "" or raw_acc is None:
        accuracy_cell: object = ""
    else:
        try:
            accuracy_cell = int(raw_acc)
        except (TypeError, ValueError):
            accuracy_cell = ""

    # Transcript: the raw source text. For email -> body; chat -> message
    # text; meeting -> Whisper transcript. Stored in source_text by the
    # respective service at insertion time. Older rows may not have it.
    transcript = get("source_text") or ""

    # Accuracy Rating Explanation + how-to-improve. Populated by the
    # post-transcription LLM rating step (services/meeting_service.py).
    accuracy_explanation = get("accuracy_explanation") or ""

    return [
        # A..E (frozen): Done?, Heading, Description, Date, Accuracy Rating
        status == "done",
        get("task"),
        get("task_description"),
        _format_iso_timestamp(date_given),
        accuracy_cell,
        # F (first unfrozen): Transcript
        transcript,
        # G..Q: rest of the existing data
        status or "open",
        source_label,
        get("source_link"),
        get("rationale"),
        get("growth_pillar") or "Other",
        get("sender_or_speaker"),
        get("spoc_contact"),
        get("urgency") or "Medium",
        _format_iso_timestamp(get("deadline")),
        get("all_updates"),
        get("user_remarks"),
        # R: Accuracy Rating Explanation
        accuracy_explanation,
        # S, T: hidden sort key + task id
        sort_key,
        str(get("id")),
    ]


class SheetsSyncWorker(threading.Thread):
    def __init__(
        self,
        stop_event: threading.Event,
        client: Optional[SheetsClient] = None,
        interval: int = SYNC_INTERVAL_SECONDS,
        batch_size: int = BATCH_SIZE,
        excel_mirror: Optional[ExcelMirror] = None,
    ) -> None:
        super().__init__(name="SheetsSyncWorker", daemon=True)
        self._stop = stop_event
        self._client = client or get_sheets_client()
        self._interval = interval
        self._batch_size = batch_size
        self._db = get_db()
        self._excel = excel_mirror or get_excel_mirror()

    def run(self) -> None:  # pragma: no cover
        logger.info("SheetsSyncWorker started (interval=%ss)", self._interval)
        try:
            self._client.ensure_tabs()
        except Exception:
            logger.exception("Could not bootstrap sheet tabs/headers; will retry on next flush.")

        while not self._stop.is_set():
            try:
                self.flush_once()
            except Exception:
                logger.exception("Sheets sync cycle crashed; will retry.")
                self._db.log_event("ERROR", "sheets.sync", "Sync cycle crashed")
            for _ in range(self._interval):
                if self._stop.is_set():
                    break
                time.sleep(1)
        logger.info("SheetsSyncWorker stopped.")

    def flush_once(self) -> int:
        rows_pushed = 0
        touched_tabs: set[str] = set()

        while not self._stop.is_set():
            tasks = self._db.unsynced_tasks(limit=self._batch_size)
            if not tasks:
                break

            for task in tasks:
                source_tab = source_tab_for(task["source_type"])
                if self._flush_task(task):
                    rows_pushed += 1
                    touched_tabs.add(source_tab)
                    touched_tabs.add(TAB_ALL_TASKS)
                    touched_tabs.add(TAB_ALL_TASKS_DETAIL)

            if len(tasks) < self._batch_size:
                break

        if rows_pushed:
            for tab in touched_tabs:
                self._client.sort_tab_desc_by_sort_key(tab)
            logger.info("Sheets sync: pushed %d task row(s).", rows_pushed)
        return rows_pushed

    def _flush_task(self, task) -> bool:
        tab = source_tab_for(task["source_type"])
        row = _row_for_task(task)
        task_id = int(task["id"])

        try:
            source_row = self._client.upsert_task_row(tab, task_id, row)
            all_row = self._client.upsert_task_row(TAB_ALL_TASKS, task_id, row)
            # Also write to the dedicated all-details tab. Failure here
            # is logged but does NOT roll back the other two — the user
            # can rebuild the All Tasks tab from scratch if needed.
            try:
                self._client.upsert_task_row(TAB_ALL_TASKS_DETAIL, task_id, row)
            except Exception:
                logger.exception(
                    "All Tasks (detail) upsert failed for task id=%s "
                    "(Checklist + source tab IS up to date).",
                    task_id,
                )
        except Exception:
            logger.exception("Could not upsert task id=%s to tab(s).", task_id)
            return False

        self._db.mark_tasks_synced(
            [task_id],
            all_tasks_starting_row=all_row,
            source_starting_row=source_row,
        )

        try:
            self._excel.upsert_task_row(tab, row)
            self._excel.upsert_task_row(TAB_ALL_TASKS, row)
            self._excel.upsert_task_row(TAB_ALL_TASKS_DETAIL, row)
        except Exception:
            logger.exception("Excel mirror upsert failed (Google Sheet IS up to date).")
        return True
