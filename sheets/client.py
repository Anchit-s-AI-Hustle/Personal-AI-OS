"""
Google Sheets API client.

Maintains three tabs in a fixed order:
    1. Checklist Tracker              (interactive checklist surface)
    2. Tasks from Gmail               (email + Google Chat source rows)
    3. Tasks from In-Person Meetings  (meeting / voice memo source rows)

Every task gets dual-written: one row in its source-specific tab and
one row in "Checklist Tracker". Hidden columns carry the raw ISO sort
key and the stable DB task id so rows can be updated in place even
after sorts or manual reordering.
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

TAB_ALL_TASKS = "Checklist Tracker"
TAB_ALL_TASKS_DETAIL = "All Tasks"
TAB_FROM_GMAIL = "Tasks from Gmail"
TAB_FROM_DISCUSSIONS = "Tasks from In-Person Meetings"

# Order matters: Checklist Tracker (daily-use condensed view) appears
# first, then the new All Tasks (full-detail view), then per-source
# condensed checklists.
TAB_ORDER: tuple[str, ...] = (
    TAB_ALL_TASKS,
    TAB_ALL_TASKS_DETAIL,
    TAB_FROM_GMAIL,
    TAB_FROM_DISCUSSIONS,
)

# Tabs that are CONDENSED — they hide cols H-N (Why / Growth Pillar /
# SPOC / SPOC Contact / Priority / Task Deadline / All Updates) so the
# user only sees A-G: the seven top critical fields.
# Sort key (P) and task id (Q) are hidden on ALL tabs unconditionally.
CONDENSED_TABS: frozenset[str] = frozenset({
    TAB_ALL_TASKS,
    TAB_FROM_GMAIL,
    TAB_FROM_DISCUSSIONS,
})

TAB_FROM_MAILS = TAB_FROM_GMAIL

LEGACY_TAB_RENAMES: dict[str, str] = {
    "All Tasks": TAB_ALL_TASKS,
    "Master Task List": TAB_ALL_TASKS,
    "Tasks From Mails": TAB_FROM_GMAIL,
    "Tasks From Discussions": TAB_FROM_DISCUSSIONS,
    "Tasks from In-Person Discussions": TAB_FROM_DISCUSSIONS,
}

HEADERS: list[str] = [
    "Done?",                # A
    "Task Heading",         # B
    "Task Description",     # C
    "Task Given On",        # D
    "Status",               # E
    "Source",               # F
    "Source Link",          # G
    "Why We're Doing This", # H
    "Growth Pillar",        # I
    "SPOC",                 # J
    "SPOC Contact",         # K
    "Priority",             # L
    "Task Deadline",        # M
    "All Updates",          # N
    "Remarks",              # O
    "_iso_sort_key",        # P hidden
    "_task_id",             # Q hidden
]

USER_VISIBLE_COLS = 15
CHECKBOX_COL_LETTER = "A"
STATUS_COL_LETTER = "E"
SORT_KEY_COL_INDEX = 16
SORT_KEY_COL_LETTER = "P"
TASK_ID_COL_INDEX = 17
TASK_ID_COL_LETTER = "Q"

LEGACY_SCHEMAS: list[tuple[list[str], list[int]]] = [
    (
        [
            "Task Heading", "Task Description", "Status",
            "Why We're Doing This", "Growth Pillar", "SPOC",
            "Priority", "Go Live", "Remarks",
        ],
        [0, 3, 4, 5, 9, 10, 13, 14, 15, 16],
    ),
    (
        [
            "Task Heading", "Task Description", "Status", "Source",
            "Why We're Doing This", "Growth Pillar", "SPOC",
            "Priority", "Go Live", "Remarks",
        ],
        [0, 4, 5, 6, 10, 11, 14, 15, 16],
    ),
    (
        [
            "Task Heading", "Task Description", "Status", "Source", "Source Link",
            "Date Given", "Why We're Doing This", "Growth Pillar", "SPOC",
            "SPOC Contact", "Priority", "Go Live", "Remarks",
        ],
        [0, 13, 14, 15, 16],
    ),
    (
        [
            "Task Heading", "Task Description", "Status", "Source", "Source Link",
            "Task Given At", "Why We're Doing This", "Growth Pillar", "SPOC",
            "SPOC Contact", "Priority", "Task Deadline", "Remarks",
        ],
        [0, 13, 14, 15, 16],
    ),
    (
        [
            "Task Heading", "Task Description", "Status", "Source", "Source Link",
            "Task Given On", "Why We're Doing This", "Growth Pillar", "SPOC",
            "SPOC Contact", "Priority", "Task Deadline", "Remarks",
        ],
        [0, 13, 14, 15, 16],
    ),
    (
        [
            "Task Heading", "Task Description", "Status", "Source", "Source Link",
            "Task Given On", "Why We're Doing This", "Growth Pillar", "SPOC",
            "SPOC Contact", "Priority", "Task Deadline", "All Updates", "Remarks",
        ],
        [0, 15, 16],
    ),
]

LEGACY_HEADERS_NO_SOURCE: list[str] = LEGACY_SCHEMAS[0][0]


def _col_letter(n: int) -> str:
    if n < 1:
        raise ValueError("column index must be >= 1")
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord("A") + rem) + result
    return result


def source_tab_for(source_type: str) -> str:
    s = (source_type or "").lower()
    if s in ("email", "chat"):
        return TAB_FROM_GMAIL
    return TAB_FROM_DISCUSSIONS


class SheetsClient:
    def __init__(self) -> None:
        creds = get_credentials()
        self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._sheet_id = settings.google_sheet_id
        self._tabs_ready = False
        self._lock = threading.Lock()

    def ensure_tabs(self) -> None:
        with self._lock:
            if self._tabs_ready:
                return

            meta = self._fetch_meta()
            existing = {
                s["properties"]["title"]: s["properties"]
                for s in meta.get("sheets", [])
            }

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
                try:
                    self._batch_update(rename_requests)
                except Exception:
                    logger.exception("Could not rename legacy tabs; will retry next boot.")
                meta = self._fetch_meta()
                existing = {
                    s["properties"]["title"]: s["properties"]
                    for s in meta.get("sheets", [])
                }

            create_requests: list[dict] = []
            for i, tab in enumerate(TAB_ORDER):
                if tab not in existing:
                    create_requests.append(
                        {"addSheet": {"properties": {"title": tab, "index": i}}}
                    )
            if create_requests:
                self._batch_update(create_requests)
                meta = self._fetch_meta()
                existing = {
                    s["properties"]["title"]: s["properties"]
                    for s in meta.get("sheets", [])
                }

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
                try:
                    self._batch_update(move_requests)
                except Exception:
                    logger.debug("Tab reorder failed; will retry next boot.", exc_info=True)

            for tab in TAB_ORDER:
                self._ensure_header_row(tab)

            self._style_tabs()
            self._tabs_ready = True

    def _fetch_meta(self) -> dict:
        def _call() -> dict:
            return self._svc.spreadsheets().get(spreadsheetId=self._sheet_id).execute()

        return retry_call(_call, attempts=3, exceptions=(HttpError, TimeoutError))

    def _batch_update(self, requests: list[dict]) -> None:
        def _call() -> None:
            self._svc.spreadsheets().batchUpdate(
                spreadsheetId=self._sheet_id,
                body={"requests": requests},
            ).execute()

        retry_call(_call, attempts=3, exceptions=(HttpError, TimeoutError))

    def _ensure_header_row(self, tab: str) -> None:
        end_col = _col_letter(len(HEADERS))
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
            return

        for legacy_header, insert_at in LEGACY_SCHEMAS:
            if current_header[: len(legacy_header)] == legacy_header:
                self._insert_blank_columns(tab, insert_at)
                break

        def _write() -> None:
            self._svc.spreadsheets().values().update(
                spreadsheetId=self._sheet_id,
                range=f"'{tab}'!A1:{end_col}1",
                valueInputOption="RAW",
                body={"values": [HEADERS]},
            ).execute()

        retry_call(_write, attempts=3, exceptions=(HttpError, TimeoutError))

    def _insert_blank_columns(self, tab: str, indices: list[int]) -> None:
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

    def _style_tabs(self) -> None:
        meta = self._fetch_meta()
        title_to_id = {
            s["properties"]["title"]: s["properties"]["sheetId"]
            for s in meta.get("sheets", [])
        }
        requests: list[dict] = []
        col_pixels = [
            70, 260, 420, 180, 110, 220, 220, 320, 140, 160, 220, 100, 160, 460, 220
        ]

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
                                    "backgroundColor": {"red": 0.92, "green": 0.92, "blue": 0.92},
                                }
                            },
                            "fields": "userEnteredFormat(textFormat,backgroundColor)",
                        }
                    },
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet_id,
                                "gridProperties": {
                                    "frozenRowCount": 1,
                                    "frozenColumnCount": 5,
                                },
                            },
                            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
                        }
                    },
                ]
            )

            for idx, px in enumerate(col_pixels):
                requests.append(
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": idx,
                                "endIndex": idx + 1,
                            },
                            "properties": {"pixelSize": px},
                            "fields": "pixelSize",
                        }
                    }
                )

            for hidden_idx in (SORT_KEY_COL_INDEX - 1, TASK_ID_COL_INDEX - 1):
                requests.append(
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": hidden_idx,
                                "endIndex": hidden_idx + 1,
                            },
                            "properties": {"hiddenByUser": True},
                            "fields": "hiddenByUser",
                        }
                    }
                )

            for text_col_idx in (3, 12, 13):
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "startColumnIndex": text_col_idx,
                                "endColumnIndex": text_col_idx + 1,
                            },
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                            "fields": "userEnteredFormat.numberFormat",
                        }
                    }
                )

            for wide_col_idx in (2, 7, 13, 14):
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "startColumnIndex": wide_col_idx,
                                "endColumnIndex": wide_col_idx + 1,
                            },
                            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                            "fields": "userEnteredFormat.wrapStrategy",
                        }
                    }
                )

            for status_val, fill, strike in [
                ("done", {"red": 0.86, "green": 0.96, "blue": 0.85}, True),
                ("dropped", {"red": 0.92, "green": 0.92, "blue": 0.92}, True),
            ]:
                requests.append(
                    {
                        "addConditionalFormatRule": {
                            "rule": {
                                "ranges": [{
                                    "sheetId": sheet_id,
                                    "startRowIndex": 1,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": USER_VISIBLE_COLS,
                                }],
                                "booleanRule": {
                                    "condition": {
                                        "type": "CUSTOM_FORMULA",
                                        "values": [{"userEnteredValue": f'=LOWER($E2)="{status_val}"'}],
                                    },
                                    "format": {
                                        "backgroundColor": fill,
                                        "textFormat": {"strikethrough": strike},
                                    },
                                },
                            },
                            "index": 0,
                        }
                    }
                )

            for prio_val, fill in [
                ("Critical", {"red": 0.96, "green": 0.80, "blue": 0.80}),
                ("High", {"red": 0.99, "green": 0.89, "blue": 0.78}),
                ("Medium", {"red": 1.00, "green": 0.97, "blue": 0.82}),
                ("Low", {"red": 0.93, "green": 0.93, "blue": 0.93}),
            ]:
                requests.append(
                    {
                        "addConditionalFormatRule": {
                            "rule": {
                                "ranges": [{
                                    "sheetId": sheet_id,
                                    "startRowIndex": 1,
                                    "startColumnIndex": 11,
                                    "endColumnIndex": 12,
                                }],
                                "booleanRule": {
                                    "condition": {
                                        "type": "TEXT_EQ",
                                        "values": [{"userEnteredValue": prio_val}],
                                    },
                                    "format": {
                                        "backgroundColor": fill,
                                        "textFormat": {"bold": True},
                                    },
                                },
                            },
                            "index": 0,
                        }
                    }
                )

        checklist_gid = title_to_id.get(TAB_ALL_TASKS)
        if checklist_gid is not None:
            requests.append(
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": checklist_gid,
                            "startRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1,
                        },
                        "rule": {
                            "condition": {"type": "BOOLEAN"},
                            "strict": True,
                            "showCustomUi": True,
                        },
                    }
                }
            )

        if requests:
            try:
                self._batch_update(requests)
            except Exception:
                logger.debug("Could not apply sheet styling.", exc_info=True)

    def append_rows(self, tab: str, rows: list[list[object]]) -> Optional[int]:
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
        return first_row

    def _find_row_by_task_id(self, tab: str, task_id: int) -> Optional[int]:
        rng = f"'{tab}'!{TASK_ID_COL_LETTER}2:{TASK_ID_COL_LETTER}"

        def _read() -> list:
            resp = (
                self._svc.spreadsheets()
                .values()
                .get(spreadsheetId=self._sheet_id, range=rng)
                .execute()
            )
            return resp.get("values") or []

        values = retry_call(_read, attempts=3, exceptions=(HttpError, TimeoutError))
        for idx, row in enumerate(values, start=2):
            if not row:
                continue
            if str(row[0]).strip() == str(task_id):
                return idx
        return None

    def upsert_task_row(self, tab: str, task_id: int, row: list[object]) -> int:
        self.ensure_tabs()
        existing_row = self._find_row_by_task_id(tab, task_id)
        end_col = _col_letter(len(HEADERS))

        if existing_row is None:
            first_row = self.append_rows(tab, [row])
            return int(first_row or 2)

        def _call() -> None:
            self._svc.spreadsheets().values().update(
                spreadsheetId=self._sheet_id,
                range=f"'{tab}'!A{existing_row}:{end_col}{existing_row}",
                valueInputOption="USER_ENTERED",
                body={"values": [row]},
            ).execute()

        retry_call(_call, attempts=3, exceptions=(HttpError, TimeoutError))
        return existing_row

    def sort_tab_desc_by_sort_key(self, tab: str) -> None:
        meta = self._fetch_meta()
        sheet_gid: Optional[int] = None
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == tab:
                sheet_gid = s["properties"]["sheetId"]
                break
        if sheet_gid is None:
            return

        request = {
            "sortRange": {
                "range": {
                    "sheetId": sheet_gid,
                    "startRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(HEADERS),
                },
                "sortSpecs": [
                    {
                        "dimensionIndex": SORT_KEY_COL_INDEX - 1,
                        "sortOrder": "DESCENDING",
                    }
                ],
            }
        }
        try:
            self._batch_update([request])
        except Exception:
            logger.exception("Failed to sort tab %r by _iso_sort_key.", tab)

    def update_status(self, tab: str, row_number: int, status: str) -> None:
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
