"""
Reverse sync: pull human edits from Google Sheets back into the DB.

The checklist tab is the canonical human-edit surface. This worker reads
its visible columns, matches rows by hidden task id, and pulls back:
checkbox state, status, heading/description/rationale, SPOC fields,
priority, deadline, update log, and remarks.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from googleapiclient.errors import HttpError

from config import settings
from database import get_db
from database.models import normalise_growth_pillar, normalise_urgency
from services.task_service import normalize_heading
from utils.identifiers import clean_identifier
from utils.logger import get_logger
from utils.retry import retry_call

from .client import (
    SheetsClient,
    TAB_ALL_TASKS,
    TASK_ID_COL_INDEX,
    USER_VISIBLE_COLS,
    get_sheets_client,
)

logger = get_logger(__name__)

REVERSE_SYNC_INTERVAL_SECONDS = 60

_COL_DONE = 0
_COL_HEADING = 1
_COL_DESCRIPTION = 2
_COL_STATUS = 4
_COL_RATIONALE = 7
_COL_GROWTH = 8
_COL_SPOC = 9
_COL_SPOC_CONTACT = 10
_COL_PRIORITY = 11
_COL_DEADLINE = 12
_COL_UPDATES = 13
_COL_REMARKS = 14

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


def _normalise_status(raw: Optional[str], *, checkbox_value: Optional[str]) -> Optional[str]:
    checked = str(checkbox_value or "").strip().lower() in {"true", "1", "yes"}
    if checked:
        return "done"
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return "open"
    if s in _VALID_STATUSES:
        return s
    return _STATUS_ALIASES.get(s)


def _col_letter(n: int) -> str:
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord("A") + rem) + result
    return result


class ReverseSyncWorker(threading.Thread):
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
        end_col = _col_letter(TASK_ID_COL_INDEX)
        rng = f"'{TAB_ALL_TASKS}'!A2:{end_col}"

        def _read() -> list:
            resp = (
                self._client._svc.spreadsheets()  # noqa: SLF001
                .values()
                .get(spreadsheetId=settings.google_sheet_id, range=rng)
                .execute()
            )
            return resp.get("values") or []

        try:
            sheet_rows = retry_call(_read, attempts=3, exceptions=(HttpError, TimeoutError))
        except Exception:
            logger.exception("Reverse sync: could not read checklist tab.")
            return 0

        if not sheet_rows:
            return 0

        updates = 0
        for row in sheet_rows:
            if len(row) < TASK_ID_COL_INDEX:
                continue
            raw_task_id = str(row[TASK_ID_COL_INDEX - 1] or "").strip()
            if not raw_task_id.isdigit():
                continue

            task_id = int(raw_task_id)
            db_row = self._db.get_task(task_id)
            if db_row is None:
                continue

            heading = (row[_COL_HEADING] if len(row) > _COL_HEADING else "") or ""
            description = (row[_COL_DESCRIPTION] if len(row) > _COL_DESCRIPTION else "") or ""
            rationale = (row[_COL_RATIONALE] if len(row) > _COL_RATIONALE else "") or ""
            growth = (row[_COL_GROWTH] if len(row) > _COL_GROWTH else "") or ""
            spoc = clean_identifier((row[_COL_SPOC] if len(row) > _COL_SPOC else "") or "")
            spoc_contact = clean_identifier((row[_COL_SPOC_CONTACT] if len(row) > _COL_SPOC_CONTACT else "") or "")
            priority = (row[_COL_PRIORITY] if len(row) > _COL_PRIORITY else "") or ""
            deadline = (row[_COL_DEADLINE] if len(row) > _COL_DEADLINE else "") or ""
            all_updates = (row[_COL_UPDATES] if len(row) > _COL_UPDATES else "") or ""
            remarks = (row[_COL_REMARKS] if len(row) > _COL_REMARKS else "") or ""
            status = _normalise_status(
                row[_COL_STATUS] if len(row) > _COL_STATUS else "",
                checkbox_value=row[_COL_DONE] if len(row) > _COL_DONE else "",
            )
            if status is None:
                continue

            new_values = {
                "task": heading.strip() or db_row["task"],
                "task_description": description.strip(),
                "rationale": rationale.strip(),
                "growth_pillar": normalise_growth_pillar(growth),
                "sender_or_speaker": spoc,
                "spoc_contact": spoc_contact,
                "urgency": normalise_urgency(priority),
                "deadline": deadline.strip(),
                "all_updates": all_updates.strip(),
                "user_remarks": remarks.strip(),
                "status": status,
            }
            new_values["normalized_heading"] = normalize_heading(new_values["task"])

            changed = any(
                (db_row[key] or "") != (value or "")
                for key, value in new_values.items()
                if key != "normalized_heading"
            ) or (db_row["normalized_heading"] or "") != (new_values["normalized_heading"] or "")

            if not changed:
                continue

            self._db.update_task_tracker_fields(
                task_id,
                task=new_values["task"],
                task_description=new_values["task_description"],
                rationale=new_values["rationale"],
                growth_pillar=new_values["growth_pillar"],
                sender_or_speaker=new_values["sender_or_speaker"],
                spoc_contact=new_values["spoc_contact"],
                urgency=new_values["urgency"],
                deadline=new_values["deadline"],
                all_updates=new_values["all_updates"],
                user_remarks=new_values["user_remarks"],
                status=new_values["status"],
                normalized_heading=new_values["normalized_heading"],
                touched_by_user=True,
            )
            updates += 1

        if updates:
            logger.info("Reverse sync: %d task update(s) pulled from checklist.", updates)
        return updates
