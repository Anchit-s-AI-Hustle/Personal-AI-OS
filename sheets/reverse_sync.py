"""
Reverse sync: pull human edits from Google Sheets back into the DB.

The forward sync worker (sheets/sync.py) is the source of truth for the
DB -> Sheet direction. This worker handles the reverse: when a human
manually edits the Status column on the sheet, that change needs to
flow back into `extracted_tasks.status` so the daily digest, the chat
poller, and any future surfaces see the updated state.

Scope (Phase 2):
  - Status column only. Other columns (description, deadline, etc.) are
    intentionally NOT round-tripped — the sheet is the user's working
    surface for those, the DB doesn't need them.
  - "All Tasks" tab is the canonical surface to read from. Each task
    row's `sheet_row_all` is the index we look up by.

Failure modes:
  - Sheet API outage: log warning, retry next cycle.
  - Status value the sheet contains isn't one of the allowed enum values:
    snap to the closest match, log if we couldn't.
  - Task not found in DB for a given row: skip silently. (Probably a
    row the user added by hand — those don't have a DB counterpart.)
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from googleapiclient.errors import HttpError

from config import settings
from database import get_db
from utils.logger import get_logger
from utils.retry import retry_call

from .client import (
    SheetsClient,
    STATUS_COL_LETTER,
    TAB_ALL_TASKS,
    get_sheets_client,
)

logger = get_logger(__name__)

REVERSE_SYNC_INTERVAL_SECONDS = 60

_VALID_STATUSES = {"open", "done", "dropped"}
_STATUS_ALIASES = {
    "open": "open",
    "todo": "open",
    "to do": "open",
    "pending": "open",
    "in progress": "open",
    "wip": "open",
    "blocked": "open",
    "done": "done",
    "complete": "done",
    "completed": "done",
    "closed": "done",
    "shipped": "done",
    "dropped": "dropped",
    "cancelled": "dropped",
    "canceled": "dropped",
    "wontfix": "dropped",
    "won't fix": "dropped",
    "skip": "dropped",
}


def _normalise_status(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if s in _VALID_STATUSES:
        return s
    return _STATUS_ALIASES.get(s)


class ReverseSyncWorker(threading.Thread):
    """
    Periodically reads the Status column of the "All Tasks" tab and
    updates the DB for any row whose status was edited by the user.

    Idempotent: if the sheet status already matches the DB, no write.
    """

    def __init__(
        self,
        stop_event: threading.Event,
        client: Optional[SheetsClient] = None,
        interval: int = REVERSE_SYNC_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(name="ReverseSyncWorker", daemon=True)
        self._stop = stop_event
        self._client = client or get_sheets_client()
        self._interval = interval
        self._db = get_db()

    def run(self) -> None:  # pragma: no cover
        logger.info("ReverseSyncWorker started (interval=%ss)", self._interval)
        # Wait one interval before the first poll so the forward sync has
        # a chance to bootstrap tabs/headers on a fresh boot.
        for _ in range(self._interval):
            if self._stop.is_set():
                return
            time.sleep(1)

        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("Reverse sync cycle crashed; will retry.")
            for _ in range(self._interval):
                if self._stop.is_set():
                    break
                time.sleep(1)
        logger.info("ReverseSyncWorker stopped.")

    def poll_once(self) -> int:
        """
        Read the Status column for every synced task, compare to DB,
        update DB rows whose sheet status has changed. Returns the
        number of DB rows updated this cycle.
        """
        # Pull every task that has a row index on "All Tasks" — those are
        # the ones the user can edit. Unsynced / never-synced rows are
        # skipped (they have no row to read).
        rows = self._db.fetchall(
            """
            SELECT id, sheet_row_all, status
              FROM extracted_tasks
             WHERE sheet_row_all IS NOT NULL
            """
        )
        if not rows:
            return 0

        # Read the entire Status column once. Rows in `extracted_tasks`
        # may point at row N up to the current end of the sheet — pull
        # the column at maximum needed depth in a single API call.
        max_row = max(r["sheet_row_all"] for r in rows)
        rng = f"'{TAB_ALL_TASKS}'!{STATUS_COL_LETTER}2:{STATUS_COL_LETTER}{max_row}"

        def _read() -> list:
            resp = (
                self._client._svc.spreadsheets()  # noqa: SLF001
                .values()
                .get(spreadsheetId=settings.google_sheet_id, range=rng)
                .execute()
            )
            return resp.get("values") or []

        try:
            values = retry_call(
                _read, attempts=3, exceptions=(HttpError, TimeoutError)
            )
        except HttpError as exc:
            # 400 "Unable to parse range" means the tab name in our
            # constant doesn't exist on the live Sheet (e.g. the user
            # renamed/deleted it, or this Python process is still
            # holding a stale TAB_ALL_TASKS value because main.py was
            # started before a tab-rename migration). Logging "exception"
            # for this would spew a huge traceback every minute. Treat
            # it as a soft skip with a one-line warning.
            status = getattr(exc.resp, "status", None) if exc.resp else None
            if status in (400, 404):
                logger.warning(
                    "Reverse sync: cannot read %r — tab not found on the "
                    "Sheet. Skipping this cycle. If you just renamed/created "
                    "tabs, restart main.py so the new TAB_ALL_TASKS value "
                    "is loaded.",
                    f"{TAB_ALL_TASKS}!{STATUS_COL_LETTER}",
                )
                return 0
            logger.exception("Reverse sync: HTTP error reading status column.")
            return 0
        except Exception:
            logger.exception("Reverse sync: could not read status column.")
            return 0

        # values is a list of single-element lists, one per row, indexed
        # from sheet row 2 -> values[0]. Build a {row_number: cell_value}
        # so missing/empty cells are tolerated.
        status_by_row: dict[int, str] = {}
        for offset, row in enumerate(values):
            if not row:
                continue
            cell = row[0] if row else ""
            status_by_row[offset + 2] = cell  # +2 because we read from row 2

        updates = 0
        unknown_values: set[str] = set()

        for r in rows:
            row_idx = r["sheet_row_all"]
            sheet_raw = status_by_row.get(row_idx, "")
            sheet_status = _normalise_status(sheet_raw)
            if sheet_status is None:
                if sheet_raw and sheet_raw.strip():
                    unknown_values.add(sheet_raw.strip())
                continue
            if sheet_status == r["status"]:
                continue

            try:
                self._db.update_task_status(r["id"], sheet_status)
                updates += 1
                logger.info(
                    "Reverse sync: task id=%d status %r -> %r (row %d)",
                    r["id"],
                    r["status"],
                    sheet_status,
                    row_idx,
                )
            except ValueError:
                # update_task_status enforces the enum; should not happen
                # because _normalise_status already snapped to an enum value.
                logger.debug(
                    "Reverse sync: rejected status %r for task %d", sheet_status, r["id"]
                )

        if unknown_values:
            logger.info(
                "Reverse sync: ignored %d unrecognised status value(s): %s",
                len(unknown_values),
                sorted(unknown_values),
            )
        if updates:
            logger.info("Reverse sync: %d task status update(s) pulled from Sheet.", updates)
        return updates
