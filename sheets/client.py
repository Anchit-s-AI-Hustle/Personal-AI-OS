"""
Google Sheets API client.

Maintains four tabs in a fixed order:
    1. Master Task List              (every task across every source)
    2. Tasks from Gmail              (emails + Google Chat DMs/Spaces/Groups)
    3. Tasks from WhatsApp           (WhatsApp chat exports)
    4. Tasks from In-Person Discussions  (meetings / voice memos / live calls)

Every task gets dual-written: one row in its source-specific tab and
one row in "All Tasks". The local DB stores both row numbers so future
status updates can patch both rows.

Column layout (identical in all four tabs):
    A  Task Heading
    B  Task Description     (always includes context: project / topic / customer)
    C  Status               (open | done | dropped)
    D  Source               (e.g. "Email | from Aman <aman@vahdam.com>")
    E  Source Link          (deep-link to the originating message/thread/session)
    F  Task Given On        (ISO 8601 — date+time the task was assigned/discussed)
    G  Why We're Doing This (the rationale / business reason)
    H  Growth Pillar        (Operations | Retention | Acquisition | ... | Other)
    I  SPOC                 (the person responsible — sender or speaker)
    J  SPOC Contact         (email or phone — never blank if SPOC is set)
    K  Priority             (Low | Medium | High | Critical)
    L  Task Deadline        (when this should ship / be done by)
    M  All Updates          (chronological log of follow-ups across sources)
    N  Remarks              (left blank — for human use)
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
TAB_ALL_TASKS = "Master Task List"
TAB_FROM_GMAIL = "Tasks from Gmail"
TAB_FROM_WHATSAPP = "Tasks from WhatsApp"
# Excel limits worksheet titles to 31 characters. "Tasks from In-Person
# Discussions" is 32 chars, so we use a 30-char form that still reads
# clearly. Both Sheets and Excel use the same name.
TAB_FROM_DISCUSSIONS = "Tasks from In-Person Meetings"
TAB_ORDER: tuple[str, ...] = (
    TAB_ALL_TASKS,
    TAB_FROM_GMAIL,
    TAB_FROM_WHATSAPP,
    TAB_FROM_DISCUSSIONS,
)

# Backward-compat alias so older imports (TAB_FROM_MAILS) keep working.
TAB_FROM_MAILS = TAB_FROM_GMAIL

# Legacy tab names that should be RENAMED IN PLACE on next bootstrap so
# existing rows aren't lost. Order: oldest -> newest. Applied first in
# ensure_tabs(), before the missing-tab / reorder logic runs.
LEGACY_TAB_RENAMES: dict[str, str] = {
    "All Tasks":                            TAB_ALL_TASKS,
    "Tasks From Mails":                     TAB_FROM_GMAIL,
    "Tasks From WhatsApp":                  TAB_FROM_WHATSAPP,
    "Tasks From Discussions":               TAB_FROM_DISCUSSIONS,
    # Catch the 32-char form a previous migration may have set.
    "Tasks from In-Person Discussions":     TAB_FROM_DISCUSSIONS,
}


HEADERS: list[str] = [
    "Task Heading",       # A
    "Task Description",   # B
    "Status",             # C
    "Source",             # D
    "Source Link",        # E
    "Task Given On",      # F  (renamed from "Date Given" -> "Task Given At" -> "Task Given On")
    "Why We're Doing This",  # G
    "Growth Pillar",      # H
    "SPOC",               # I
    "SPOC Contact",       # J
    "Priority",           # K
    "Task Deadline",      # L  (renamed from "Go Live")
    "All Updates",        # M  (chronological log of follow-ups across sources)
    "Remarks",            # N
]

# Status column letter — used by update_status. If HEADERS shifts, change here.
STATUS_COL_LETTER = "C"

# Known prior schemas. Self-heal logic detects these by header-row equality
# and migrates the worksheet onto the current HEADERS layout.
#
# Each entry maps {column index (0-based) -> blank to insert}. Indices are
# applied in ascending order to a row matching the legacy layout, producing
# a row matching HEADERS.
LEGACY_SCHEMAS: list[tuple[list[str], list[int]]] = [
    # 9-col pre-"Source": insert Source(D=3), Source Link(E=4), Date Given(F=5),
    # SPOC Contact(J=9).
    (
        [
            "Task Heading", "Task Description", "Status",
            "Why We're Doing This", "Growth Pillar", "SPOC",
            "Priority", "Go Live", "Remarks",
        ],
        [3, 4, 5, 9],
    ),
    # 10-col post-"Source" (the immediately previous schema): insert Source Link(E=4),
    # Date Given(F=5), SPOC Contact(J=9).
    (
        [
            "Task Heading", "Task Description", "Status", "Source",
            "Why We're Doing This", "Growth Pillar", "SPOC",
            "Priority", "Go Live", "Remarks",
        ],
        [4, 5, 9],
    ),
    # 13-col schema with the old "Date Given" / "Go Live" header text
    # (column positions identical to current HEADERS — just rewrite the
    # header row, no data shift needed).
    (
        [
            "Task Heading", "Task Description", "Status", "Source", "Source Link",
            "Date Given", "Why We're Doing This", "Growth Pillar", "SPOC",
            "SPOC Contact", "Priority", "Go Live", "Remarks",
        ],
        [],
    ),
    # 13-col schema with intermediate "Task Given At" header text. No
    # "All Updates" column yet — insert one at index 12 (M) so existing
    # data shifts right one position; "Remarks" ends up at N.
    (
        [
            "Task Heading", "Task Description", "Status", "Source", "Source Link",
            "Task Given At", "Why We're Doing This", "Growth Pillar", "SPOC",
            "SPOC Contact", "Priority", "Task Deadline", "Remarks",
        ],
        [12],
    ),
    # 13-col schema with "Task Given On" (current names) but no "All
    # Updates". Insert at index 12.
    (
        [
            "Task Heading", "Task Description", "Status", "Source", "Source Link",
            "Task Given On", "Why We're Doing This", "Growth Pillar", "SPOC",
            "SPOC Contact", "Priority", "Task Deadline", "Remarks",
        ],
        [12],
    ),
]

# Kept as an alias so older imports don't break.
LEGACY_HEADERS_NO_SOURCE: list[str] = LEGACY_SCHEMAS[0][0]


def _col_letter(n: int) -> str:
    """1-based column index -> A1 letter (A, B, ..., Z, AA, AB, ...)."""
    if n < 1:
        raise ValueError("column index must be >= 1")
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord("A") + rem) + result
    return result


def source_tab_for(source_type: str) -> str:
    """
    Map a task's source_type to its dedicated tab.

    Tab routing:
      - Email + Google Chat (DMs/Spaces/Groups) -> "Tasks from Gmail"
        (Workspace lumps them together; the user wants them in one tab.)
      - WhatsApp                                -> "Tasks from WhatsApp"
      - Meeting / Conversation / anything else  -> "Tasks from In-Person
                                                    Discussions" (live audio).
    """
    s = (source_type or "").lower()
    if s in ("email", "chat"):
        return TAB_FROM_GMAIL
    if s == "whatsapp":
        return TAB_FROM_WHATSAPP
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

        Step 0 — rename legacy tab names to current names so existing rows
        are preserved across the rename. Applied BEFORE the create/missing
        logic so we don't accidentally create a duplicate empty tab next
        to the legacy one.
        """
        with self._lock:
            if self._tabs_ready:
                return

            meta = self._fetch_meta()
            existing = {
                s["properties"]["title"]: s["properties"]
                for s in meta.get("sheets", [])
            }

            # 0. Rename legacy tabs in place. Skip a rename when the new
            #    name already exists (means a prior boot already migrated
            #    or the user manually created the new tab) — leave the
            #    legacy tab alone so manual cleanup is possible.
            rename_requests: list[dict] = []
            for old, new in LEGACY_TAB_RENAMES.items():
                if old == new:
                    continue
                if old in existing and new not in existing:
                    rename_requests.append(
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": existing[old]["sheetId"],
                                    "title": new,
                                },
                                "fields": "title",
                            }
                        }
                    )
            if rename_requests:
                logger.info(
                    "Renaming legacy tab(s): %s",
                    [r["updateSheetProperties"]["properties"]["title"]
                     for r in rename_requests],
                )
                try:
                    self._batch_update(rename_requests)
                except Exception:
                    logger.exception(
                        "Could not rename legacy tabs; will retry next boot."
                    )
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
        # Read up to twice the current width so we still detect older headers
        # that had fewer columns.
        end_col = _col_letter(max(len(HEADERS), 13))
        rng = f"'{tab}'!A1:{end_col}1"

        def _read() -> list:
            resp = (
                self._svc.spreadsheets()
                .values()
                .get(spreadsheetId=self._sheet_id, range=rng)
                .execute()
            )
            return resp.get("values") or []

        rows = retry_call(_read, attempts=3, exceptions=(HttpError, TimeoutError))
        current_header = rows[0] if rows else []
        if current_header[: len(HEADERS)] == HEADERS and len(current_header) == len(HEADERS):
            return  # already correct

        # Legacy schema migration: shift existing data right so each row's
        # cells realign with the new HEADERS, then rewrite the header.
        for legacy_header, insert_at in LEGACY_SCHEMAS:
            if current_header[: len(legacy_header)] == legacy_header:
                logger.info(
                    "Tab %r is on a legacy %d-col schema; inserting %d blank column(s) to migrate.",
                    tab, len(legacy_header), len(insert_at),
                )
                self._insert_blank_columns(tab, insert_at)
                break

        logger.info("Writing header row to tab %r", tab)

        def _write() -> None:
            self._svc.spreadsheets().values().update(
                spreadsheetId=self._sheet_id,
                range=f"'{tab}'!A1:{_col_letter(len(HEADERS))}1",
                valueInputOption="RAW",
                body={"values": [HEADERS]},
            ).execute()

        retry_call(_write, attempts=3, exceptions=(HttpError, TimeoutError))

    def _insert_blank_columns(self, tab: str, indices: list[int]) -> None:
        """
        Insert blank columns at the given 0-based indices on `tab`.

        Indices must reference positions in the FINAL (post-insert) layout,
        applied in ascending order. Each insertion shifts columns at and after
        that position one to the right.
        """
        if not indices:
            return
        meta = self._fetch_meta()
        sheet_id = None
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == tab:
                sheet_id = s["properties"]["sheetId"]
                break
        if sheet_id is None:
            return

        # Apply ascending so each subsequent index is correct in the
        # post-insert coordinate space.
        requests = [
            {
                "insertDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": idx,
                        "endIndex": idx + 1,
                    },
                    "inheritFromBefore": False,
                }
            }
            for idx in sorted(indices)
        ]
        try:
            self._batch_update(requests)
        except Exception:
            logger.exception("Could not insert blank columns into %r", tab)

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
        """Update the Status column of the given 1-based row."""
        if row_number is None or row_number < 2:
            return

        def _call() -> None:
            self._svc.spreadsheets().values().update(
                spreadsheetId=self._sheet_id,
                range=f"'{tab}'!{STATUS_COL_LETTER}{row_number}",
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
