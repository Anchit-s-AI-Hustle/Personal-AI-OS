"""
Local Excel mirror of the Google Sheets surface.

Writes the same task rows that go to Google Sheets into a workbook at
the repo root (`tasks.xlsx`), preserving the same 3-tab layout and 10
columns (see HEADERS in sheets/client.py). The file lives in git so the task list is review-able from
GitHub directly, and a clone of the repo always has the latest
snapshot baked in.

Failure modes (and how they're handled):
  - File is open in Excel (Windows file lock) -> log warning, skip the
    write, keep going. The next successful flush will catch it up.
  - File is corrupt / unreadable -> rebuild from scratch.

The mirror is intentionally append-only. Status updates that come in
from the sheet edit-back loop (a future feature) will need a separate
update method.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils.exceptions import InvalidFileException

from config import settings
from utils.logger import get_logger

from .client import HEADERS, LEGACY_HEADERS_NO_SOURCE, TAB_ORDER

logger = get_logger(__name__)

DEFAULT_PATH = settings.project_root / "tasks.xlsx"

_HEADER_FONT = Font(bold=True)
_HEADER_FILL = PatternFill(start_color="EAEAEA", end_color="EAEAEA", fill_type="solid")
_HEADER_ALIGN = Alignment(vertical="center")


def _column_widths() -> dict[str, int]:
    """Reasonable default widths so the Excel doesn't open as a postage stamp."""
    return {
        "A": 36,  # Task Heading
        "B": 60,  # Task Description
        "C": 12,  # Status
        "D": 12,  # Source (Email | Chat | Meeting)
        "E": 50,  # Why We're Doing This
        "F": 18,  # Growth Pillar
        "G": 22,  # SPOC
        "H": 12,  # Priority
        "I": 18,  # Go Live
        "J": 30,  # Remarks
    }


class ExcelMirror:
    """
    Thread-safe append-only writer for `tasks.xlsx`.

    Concurrency model: a single in-process lock serialises every read +
    mutation cycle. The Sheets sync worker is the only producer in
    practice, so this is plenty.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path: Path = (path or DEFAULT_PATH).resolve()
        self._lock = threading.Lock()
        self._initialised = False

    # --- public API ----------------------------------------------------------

    def append_rows(self, tab: str, rows: list[list[str]]) -> None:
        """Append rows to the given tab in the workbook. Idempotent on lock errors."""
        if not rows:
            return
        with self._lock:
            self._ensure_workbook()
            try:
                wb = load_workbook(self._path)
            except (FileNotFoundError, InvalidFileException):
                wb = self._build_empty_workbook()

            if tab not in wb.sheetnames:
                self._add_tab(wb, tab)
            ws = wb[tab]
            self._heal_legacy_schema(ws)
            for row in rows:
                ws.append(row)

            try:
                wb.save(self._path)
                logger.info(
                    "Excel mirror: appended %d row(s) to %r in %s",
                    len(rows),
                    tab,
                    self._path.name,
                )
            except PermissionError:
                # Most common cause: the file is open in Excel and Windows
                # has a write lock on it. Skip silently — the Google Sheet
                # is the source of truth and the next flush will catch us up.
                logger.warning(
                    "Excel mirror: %s is locked (probably open in Excel). "
                    "Close the file to resume mirroring; Google Sheets keeps working.",
                    self._path,
                )

    # --- bootstrap -----------------------------------------------------------

    def _ensure_workbook(self) -> None:
        if self._initialised and self._path.exists():
            return
        if not self._path.exists():
            wb = self._build_empty_workbook()
            try:
                wb.save(self._path)
                logger.info("Created Excel mirror at %s", self._path)
            except PermissionError:
                logger.warning(
                    "Could not create Excel mirror at %s (locked).", self._path
                )
                return
        else:
            # Existing file — make sure it has all expected tabs in order.
            try:
                wb = load_workbook(self._path)
            except InvalidFileException:
                logger.warning(
                    "Existing %s is unreadable; rebuilding from scratch.",
                    self._path.name,
                )
                wb = self._build_empty_workbook()
                try:
                    wb.save(self._path)
                except PermissionError:
                    return

            # Add any missing managed tabs.
            mutated = False
            for tab in TAB_ORDER:
                if tab not in wb.sheetnames:
                    self._add_tab(wb, tab)
                    mutated = True

            # Drop the default empty "Sheet" tab Excel injects on creation
            # if it's still there alongside our managed tabs.
            if "Sheet" in wb.sheetnames and len(wb.sheetnames) > len(TAB_ORDER):
                del wb["Sheet"]
                mutated = True

            if mutated:
                try:
                    wb.save(self._path)
                except PermissionError:
                    pass

        self._initialised = True

    def _build_empty_workbook(self) -> Workbook:
        wb = Workbook()
        # Workbook() creates a default "Sheet". Replace it with the first
        # managed tab and add the rest.
        default = wb.active
        default.title = TAB_ORDER[0]
        self._write_header(default)
        for tab in TAB_ORDER[1:]:
            ws = wb.create_sheet(title=tab)
            self._write_header(ws)
        # Reorder to canonical order (create_sheet appends, which is fine,
        # but be explicit for safety).
        wb._sheets = [wb[t] for t in TAB_ORDER]  # type: ignore[attr-defined]
        return wb

    def _add_tab(self, wb: Workbook, tab: str) -> None:
        ws = wb.create_sheet(title=tab)
        self._write_header(ws)
        # Sort all managed tabs to the front in canonical order.
        managed_in_order = [wb[t] for t in TAB_ORDER if t in wb.sheetnames]
        leftovers = [s for s in wb.worksheets if s not in managed_in_order]
        wb._sheets = managed_in_order + leftovers  # type: ignore[attr-defined]

    def _heal_legacy_schema(self, ws) -> None:
        """
        Bring an existing worksheet onto the current 10-col HEADERS layout.

        Two known drift cases are handled:
          1. Legacy 9-col schema (pre-"Source"): every existing row gets a
             blank cell inserted at column D, then the header is rewritten.
          2. Header row has trailing junk (e.g. an extra `None` cell that
             came from an off-by-one append): truncate to len(HEADERS).
        """
        header = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]

        # Case 1: legacy 9-col layout — shift data right and rewrite header.
        if header[: len(LEGACY_HEADERS_NO_SOURCE)] == LEGACY_HEADERS_NO_SOURCE:
            logger.info(
                "Excel mirror: tab %r is on legacy 9-col schema; inserting blank Source column.",
                ws.title,
            )
            ws.insert_cols(4)  # 1-indexed: insert before column D
            self._write_header(ws)
            return

        # Case 2: header is correct prefix but has trailing extras.
        if header[: len(HEADERS)] == HEADERS and ws.max_column > len(HEADERS):
            logger.info(
                "Excel mirror: tab %r has trailing columns past %d; trimming.",
                ws.title,
                len(HEADERS),
            )
            for extra_col in range(ws.max_column, len(HEADERS), -1):
                ws.cell(row=1, column=extra_col).value = None

    def _write_header(self, ws) -> None:
        for col, value in enumerate(HEADERS, start=1):
            cell = ws.cell(row=1, column=col, value=value)
            cell.font = _HEADER_FONT
            cell.fill = _HEADER_FILL
            cell.alignment = _HEADER_ALIGN
        for letter, width in _column_widths().items():
            ws.column_dimensions[letter].width = width
        ws.freeze_panes = "A2"


_singleton: Optional[ExcelMirror] = None
_singleton_lock = threading.Lock()


def get_excel_mirror() -> ExcelMirror:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = ExcelMirror()
    return _singleton
