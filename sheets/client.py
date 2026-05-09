"""
Google Sheets API client.

Maintains three tabs in a fixed order:
    1. All Tasks               (every task, both sources)
    2. Tasks From Discussions  (meetings / voice memos)
    3. Tasks From Mails        (email-derived)

Every task gets dual-written: one row in its source-specific tab and
one row in "All Tasks". The local DB stores both row numbers so future
status updates can patch both rows.

Column layout (same in all three tabs):
    A  Task Heading
    B  Task Description     (always includes context: project / topic / customer)
    C  Status               (open | done | dropped)
    D  Source               (e.g. "Email | from Aman <aman@vahdam.com>")
    E  Why We're Doing This (the rationale / business reason)
    F  Growth Pillar        (Operations | Retention | Acquisition | ... | Other)
    G  SPOC                 (the person responsible — sender or speaker)
    H  Priority             (Low | Medium | High | Critical)
    I  Go Live              (deadline / when this should ship)
    J  Remarks              (left blank — for human use)
"""
from __future__ import annotations

import threading
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings
from gmail.auth import get_credentials
from utils.logger import get_logger
from utils.retry import retry_call

logger = get_logger(__name__)


# Tab names — order matters; this is the order they're created/positioned.
TAB_ALL_TASKS = "All Tasks"
TAB_FROM_DISCUSSIONS = "Tasks From Discussions"
TAB_FROM_MAILS = "Tasks From Mails"
TAB_ORDER: tuple[str, ...] = (TAB_ALL_TASKS, TAB_FROM_DISCUSSIONS, TAB_FROM_MAILS)


HEADERS: list[str] = [
    "Task Heading",
    "Task Description",
    "Status",
    "Source",
    "Why We're Doing This",
    "Growth Pillar",
    "SPOC",
    "Priority",
    "Go Live",
    "Remarks",
]

# Pre-"Source" 9-column layout used by older deployments. Self-heal logic
# in SheetsClient and ExcelMirror detects this and inserts a blank Source
# column at index 3 to bring rows into the current schema.
LEGACY_HEADERS_NO_SOURCE: list[str] = [
    "Task Heading",
    "Task Description",
    "Status",
    "Why We're Doing This",
    "Growth Pillar",
    "SPOC",
    "Priority",
    "Go Live",
    "Remarks",
]


def source_tab_for(source_type: str) -> str:
    """Map a task's source_type to its dedicated tab."""
    s = (source_type or "").lower()
    if s == "email":
        return TAB_FROM_MAILS
    return TAB_FROM_DISCUSSIONS


class SheetsClient:
    def __init__(self) -> None:
        creds = get_credentials()
        self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._sheet_id = settings.google_sheet_id
        self._tabs_ready = False
        self._lock = threading.Lock()

    # --- bootstrap -----------------------------------------------------------

    def ensure_tabs(self) -> None:
        """
        Idempotent: create any missing tab in the right order, write headers
        to anything that doesn't have them yet.
        """
        with self._lock:
            if self._tabs_ready:
                return

            meta = self._fetch_meta()
            existing = {
                s["properties"]["title"]: s["properties"]
                for s in meta.get("sheets", [])
            }

            # 1. Create any missing tabs.
            create_requests: list[dict] = []
            for i, tab in enumerate(TAB_ORDER):
                if tab not in existing:
                    create_requests.append(
                        {"addSheet": {"properties": {"title": tab, "index": i}}}
                    )
            if create_requests:
                logger.info(
                    "Creating sheet tab(s): %s",
                    [r["addSheet"]["properties"]["title"] for r in create_requests],
                )
                self._batch_update(create_requests)
                meta = self._fetch_meta()
                existing = {
                    s["properties"]["title"]: s["properties"]
                    for s in meta.get("sheets", [])
                }

            # 2. Reorder so the three managed tabs sit at indices 0, 1, 2.
            move_requests: list[dict] = []
            for desired_index, tab in enumerate(TAB_ORDER):
                props = existing.get(tab)
                if props is None:
                    continue
                if props.get("index") != desired_index:
                    move_requests.append(
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": props["sheetId"],
                                    "index": desired_index,
                                },
                                "fields": "index",
                            }
                        }
                    )
            if move_requests:
                logger.info("Reordering tabs to canonical order.")
                try:
                    self._batch_update(move_requests)
                except Exception:
                    # Reordering can race with creation timestamps; non-fatal.
                    logger.debug("Tab reorder failed; will retry next boot.", exc_info=True)

            # 3. Header row in every tab.
            for tab in TAB_ORDER:
                self._ensure_header_row(tab)

            # 4. Header styling (bold + frozen + light grey).
            self._style_headers()

            self._tabs_ready = True

    def _fetch_meta(self) -> dict:
        def _call() -> dict:
            return (
                self._svc.spreadsheets()
                .get(spreadsheetId=self._sheet_id)
                .execute()
            )

        return retry_call(_call, attempts=3, exceptions=(HttpError, TimeoutError))

    def _batch_update(self, requests: list[dict]) -> None:
        def _call() -> None:
            self._svc.spreadsheets().batchUpdate(
                spreadsheetId=self._sheet_id,
                body={"requests": requests},
            ).execute()

        retry_call(_call, attempts=3, exceptions=(HttpError, TimeoutError))

    def _ensure_header_row(self, tab: str) -> None:
        rng = f"'{tab}'!A1:J1"

        def _read() -> list:
            resp = (
                self._svc.spreadsheets()
                .values()
                .get(spreadsheetId=self._sheet_id, range=rng)
                .execute()
            )
            return resp.get("values") or []

        rows = retry_call(_read, attempts=3, exceptions=(HttpError, TimeoutError))
        if rows and rows[0] == HEADERS:
            return  # already correct

        # Legacy schema: header row is the 9-col pre-"Source" layout.
        # Insert an empty column D in every existing data row so the
        # historical data realigns with the new HEADERS before we rewrite
        # the header.
        if rows and rows[0][: len(LEGACY_HEADERS_NO_SOURCE)] == LEGACY_HEADERS_NO_SOURCE:
            logger.info(
                "Tab %r is on the legacy 9-col schema; inserting blank Source column at D.",
                tab,
            )
            self._insert_blank_source_column(tab)

        logger.info("Writing header row to tab %r", tab)

        def _write() -> None:
            self._svc.spreadsheets().values().update(
                spreadsheetId=self._sheet_id,
                range=rng,
                valueInputOption="RAW",
                body={"values": [HEADERS]},
            ).execute()

        retry_call(_write, attempts=3, exceptions=(HttpError, TimeoutError))

    def _insert_blank_source_column(self, tab: str) -> None:
        """
        Shift columns D..end one to the right, leaving an empty column D.
        Used to migrate a tab from the legacy 9-col schema (no Source) to
        the current 10-col schema. Idempotency is enforced by the caller
        (only invoked when the header row matches LEGACY_HEADERS_NO_SOURCE).
        """
        meta = self._fetch_meta()
        sheet_id = None
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == tab:
                sheet_id = s["properties"]["sheetId"]
                break
        if sheet_id is None:
            return

        request = {
            "insertDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 3,   # column D, 0-indexed
                    "endIndex": 4,
                },
                "inheritFromBefore": False,
            }
        }
        try:
            self._batch_update([request])
        except Exception:
            logger.exception("Could not insert blank Source column into %r", tab)

    def _style_headers(self) -> None:
        """Bold + frozen + light-grey-fill row 1 across all 3 tabs."""
        meta = self._fetch_meta()
        title_to_id = {
            s["properties"]["title"]: s["properties"]["sheetId"]
            for s in meta.get("sheets", [])
        }
        requests: list[dict] = []
        for tab in TAB_ORDER:
            sheet_id = title_to_id.get(tab)
            if sheet_id is None:
                continue
            requests.extend(
                [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": len(HEADERS),
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "textFormat": {"bold": True},
                                    "backgroundColor": {
                                        "red": 0.92, "green": 0.92, "blue": 0.92
                                    },
                                }
                            },
                            "fields": "userEnteredFormat(textFormat,backgroundColor)",
                        }
                    },
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet_id,
                                "gridProperties": {"frozenRowCount": 1},
                            },
                            "fields": "gridProperties.frozenRowCount",
                        }
                    },
                ]
            )
        if requests:
            try:
                self._batch_update(requests)
            except Exception:
                # Cosmetic — never fatal.
                logger.debug("Could not apply header styling.", exc_info=True)

    # --- appends -------------------------------------------------------------

    def append_rows(self, tab: str, rows: list[list[str]]) -> Optional[int]:
        """
        Append `rows` to `tab`. Returns the 1-based row number where the
        FIRST appended row landed (so the caller can map task ids back).
        """
        if not rows:
            return None
        self.ensure_tabs()

        def _call() -> dict:
            return (
                self._svc.spreadsheets()
                .values()
                .append(
                    spreadsheetId=self._sheet_id,
                    range=f"'{tab}'!A1",
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": rows},
                )
                .execute()
            )

        resp = retry_call(_call, attempts=4, exceptions=(HttpError, TimeoutError))
        updated_range = (resp.get("updates") or {}).get("updatedRange")
        first_row: Optional[int] = None
        if updated_range and "!" in updated_range:
            cell_ref = updated_range.split("!", 1)[1]
            start = cell_ref.split(":", 1)[0]
            digits = "".join(ch for ch in start if ch.isdigit())
            if digits:
                first_row = int(digits)

        logger.info(
            "Appended %d row(s) to tab %r starting at row %s",
            len(rows),
            tab,
            first_row,
        )
        return first_row

    def update_status(self, tab: str, row_number: int, status: str) -> None:
        """Update column C (Status) of the given 1-based row."""
        if row_number is None or row_number < 2:
            return

        def _call() -> None:
            self._svc.spreadsheets().values().update(
                spreadsheetId=self._sheet_id,
                range=f"'{tab}'!C{row_number}",
                valueInputOption="RAW",
                body={"values": [[status]]},
            ).execute()

        retry_call(_call, attempts=3, exceptions=(HttpError, TimeoutError))


_singleton: Optional[SheetsClient] = None
_singleton_lock = threading.Lock()


def get_sheets_client() -> SheetsClient:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = SheetsClient()
    return _singleton
