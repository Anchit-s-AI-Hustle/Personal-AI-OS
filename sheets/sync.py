"""
Background worker that flushes locally-stored tasks to Google Sheets.

The DB is the source of truth, the sheet is the surface. Each task is
DUAL-WRITTEN: once into the source-specific tab (Tasks From Mails or
Tasks From Discussions) and once into the consolidated "All Tasks" tab.
If either append fails, the tasks stay marked unsynced and we retry on
the next cycle.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from database import get_db
from utils.logger import get_logger

from .client import (
    HEADERS,
    SheetsClient,
    TAB_ALL_TASKS,
    TAB_FROM_DISCUSSIONS,
    TAB_FROM_MAILS,
    get_sheets_client,
    source_tab_for,
)
from .excel_mirror import ExcelMirror, get_excel_mirror

logger = get_logger(__name__)

SYNC_INTERVAL_SECONDS = 30
BATCH_SIZE = 50


def _row_for_task(task) -> list[str]:
    """
    Map a DB row to the 10-column sheet shape.

    Column order (matches HEADERS in sheets/client.py):
      Task Heading | Task Description | Status | Source |
      Why We're Doing This | Growth Pillar | SPOC | Priority |
      Go Live | Remarks
    """
    # `task` is a sqlite3.Row; .keys() lets us tolerate older rows that
    # predate the migration columns.
    keys = set(task.keys())

    def get(k: str) -> str:
        if k in keys:
            v = task[k]
            return v if v is not None else ""
        return ""

    # Source column = "<source_type> | <human-readable detail>" if we have
    # the detail (sender name, DM partner, space name, etc.), otherwise
    # just the type. Examples:
    #   "Email | from Aman Kumar"
    #   "Chat | DM with Manisha Kushwaha"
    #   "Chat | Space: D2C - Content + Conversion - ROAS"
    #   "Meeting | voice memo by Anchit (Self)"
    src_type = get("source_type") or "Unknown"
    src_detail = get("source_detail")
    source_label = f"{src_type} | {src_detail}" if src_detail else src_type

    return [
        get("task"),                          # Task Heading
        get("task_description"),              # Task Description
        get("status") or "open",              # Status
        source_label,                         # Source
        get("rationale"),                     # Why We're Doing This
        get("growth_pillar") or "Other",      # Growth Pillar
        get("sender_or_speaker"),             # SPOC
        get("urgency") or "Medium",           # Priority
        get("deadline"),                      # Go Live
        "",                                   # Remarks (left blank for human use)
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
        # Bootstrap tabs + headers before any append.
        try:
            self._client.ensure_tabs()
        except Exception:
            logger.exception(
                "Could not bootstrap sheet tabs/headers; will retry on next flush."
            )

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
        while not self._stop.is_set():
            tasks = self._db.unsynced_tasks(limit=self._batch_size)
            if not tasks:
                break

            # Group by source tab so we get one append per tab per source-type.
            from_mails: list = []
            from_discussions: list = []
            for t in tasks:
                tab = source_tab_for(t["source_type"])
                if tab == TAB_FROM_MAILS:
                    from_mails.append(t)
                else:
                    from_discussions.append(t)

            # Append source-specific tabs first, then "All Tasks".
            self._flush_to_tab(from_mails)
            self._flush_to_tab(from_discussions)

            rows_pushed += len(tasks)

            if len(tasks) < self._batch_size:
                break

        if rows_pushed:
            logger.info("Sheets sync: pushed %d task row(s).", rows_pushed)
        return rows_pushed

    # --- helpers -------------------------------------------------------------

    def _flush_to_tab(self, tasks: list) -> None:
        """
        Append the given tasks to their source-specific tab AND to "All Tasks",
        then mark synced with both row numbers persisted.
        """
        if not tasks:
            return

        # All tasks in `tasks` share the same source tab by construction.
        tab = source_tab_for(tasks[0]["source_type"])
        rows = [_row_for_task(t) for t in tasks]

        # 1. Append to source-specific tab.
        try:
            source_first_row = self._client.append_rows(tab, rows)
        except Exception:
            logger.exception("Could not append %d row(s) to tab %r", len(rows), tab)
            return

        # 2. Append to "All Tasks".
        try:
            all_first_row = self._client.append_rows(TAB_ALL_TASKS, rows)
        except Exception:
            logger.exception(
                "Appended to %r but failed appending to %r; retrying next flush.",
                tab,
                TAB_ALL_TASKS,
            )
            # Don't mark synced — we want a retry. But the source tab now has
            # rows that the next retry will duplicate. The dedupe_hash in
            # extracted_tasks prevents us from emitting the same task twice
            # locally; on the sheet side, we accept this rare double-row as
            # an acceptable trade for not losing data.
            return

        # 3. Persist both row numbers + mark synced.
        self._db.mark_tasks_synced(
            (t["id"] for t in tasks),
            all_tasks_starting_row=all_first_row,
            source_starting_row=source_first_row,
        )

        # 4. Mirror to local Excel. Do this AFTER marking synced so a Google
        # Sheets push success isn't blocked by an Excel write failure (e.g.
        # the file is open in Excel locally). The mirror handles its own
        # locking and only logs warnings on failure.
        try:
            self._excel.append_rows(tab, rows)
            self._excel.append_rows(TAB_ALL_TASKS, rows)
        except Exception:
            logger.exception(
                "Excel mirror append failed (Google Sheet IS up to date)."
            )
