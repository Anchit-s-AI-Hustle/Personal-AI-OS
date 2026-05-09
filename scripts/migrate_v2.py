"""
v2 schema migration — bring DB + local Excel into the current shape.

What this does, in order:

  1. **Backfill DB columns** for every existing row:
       - date_given      <- created_at when blank
       - source_link     <- rebuild from source_ref_id for Email rows
       - source_detail   <- "Google Chat" fallback for Chat rows that had it
                            blank (we can't reconstruct the DM/space partner;
                            this at least makes the Source column readable)

  2. **Reset sync state** on every task:
       - synced_to_sheets    = 0
       - sheet_row_all       = NULL
       - sheet_row_source    = NULL
     This forces a clean re-push of every row on the next forward sync,
     so the new 13-column / 4-tab layout is populated correctly without
     leaving orphan rows tied to old sheet positions.

  3. **Rebuild local Excel** (`tasks.xlsx`):
       - Renames legacy tabs to the current names
       - Inserts the missing columns to bring 10-col tabs onto 13-col HEADERS
       - **Wipes all data rows** (keeps only the header row in each tab)
     The next forward sync re-populates everything from the freshly-clean
     DB rows.

After this script runs, restart `python main.py`. On boot:
  - Sheets/Excel ensure_tabs() will create the WhatsApp tab if needed
    and confirm column headers
  - The forward sync worker will push every DB row in batches; each row
    lands in the correct tab per the new routing (Email + Chat -> Gmail;
    WhatsApp -> WhatsApp; Meeting -> In-Person Discussions; everything
    also goes to Master Task List)

Google Sheets caveat: this script can't reach Google Sheets without
OAuth. To avoid duplicate rows on the cloud side, manually delete every
data row (keep row 1 / headers) on each tab BEFORE restarting main.py.
The forward sync will then repopulate the Sheet from scratch. If you
skip that step, the Sheet just gets new rows appended on top of the old
ones — annoying but recoverable (delete duplicates manually later).

Run:
    python -m scripts.migrate_v2
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import load_workbook  # noqa: E402
from openpyxl.utils.exceptions import InvalidFileException  # noqa: E402

from database import get_db  # noqa: E402
from sheets.client import HEADERS, LEGACY_SCHEMAS, LEGACY_TAB_RENAMES, TAB_ORDER  # noqa: E402
from sheets.excel_mirror import DEFAULT_PATH, ExcelMirror  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger("migrate_v2")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d[\s\-.]?){8,15}")


def _recover_contact(*sources):
    for s in sources:
        if not s:
            continue
        m = _EMAIL_RE.search(s)
        if m:
            return m.group(0)
    for s in sources:
        if not s:
            continue
        m = _PHONE_RE.search(s)
        if m:
            return m.group(0).strip()
    return None


def _email_link(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#inbox/{message_id}"


def step1_backfill_db() -> dict:
    """Backfill missing columns where we can recover the value."""
    db = get_db()
    rows = db.fetchall(
        """
        SELECT id, source_type, source_ref_id, source_detail, source_link,
               date_given, summary, spoc_contact, created_at
          FROM extracted_tasks
        """
    )

    fixed = {"date_given": 0, "source_link": 0, "source_detail": 0, "spoc_contact": 0}

    for r in rows:
        sets, params = [], []

        if not (r["date_given"] or "").strip() and r["created_at"]:
            sets.append("date_given = ?")
            params.append(r["created_at"])
            fixed["date_given"] += 1

        if not (r["spoc_contact"] or "").strip():
            recovered = _recover_contact(r["source_detail"], r["summary"])
            if recovered:
                sets.append("spoc_contact = ?")
                params.append(recovered)
                fixed["spoc_contact"] += 1

        if (
            (r["source_type"] or "").lower() == "email"
            and not (r["source_link"] or "").strip()
            and r["source_ref_id"]
        ):
            sets.append("source_link = ?")
            params.append(_email_link(r["source_ref_id"]))
            fixed["source_link"] += 1

        if (
            (r["source_type"] or "").lower() == "chat"
            and not (r["source_detail"] or "").strip()
        ):
            sets.append("source_detail = ?")
            params.append("Google Chat")
            fixed["source_detail"] += 1

        if not sets:
            continue
        params.append(r["id"])
        db.execute(
            f"UPDATE extracted_tasks SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

    logger.info(
        "Step 1 backfill: date_given=%d, source_link=%d, source_detail=%d, spoc_contact=%d",
        fixed["date_given"], fixed["source_link"], fixed["source_detail"], fixed["spoc_contact"],
    )
    return fixed


def step2_reset_sync_state() -> int:
    """Force every task to re-push to Sheets/Excel cleanly on next sync."""
    db = get_db()
    n = db.fetchone("SELECT COUNT(*) c FROM extracted_tasks WHERE synced_to_sheets = 1")["c"]
    db.execute(
        """
        UPDATE extracted_tasks
           SET synced_to_sheets = 0,
               sheet_row_all    = NULL,
               sheet_row_source = NULL
        """
    )
    logger.info("Step 2 reset: %d task(s) marked unsynced.", n)
    return n


def step3_rebuild_excel() -> bool:
    """Rename legacy tabs, shift columns, wipe all data rows."""
    path = DEFAULT_PATH
    if not path.exists():
        logger.info("Step 3 skipped: %s does not exist (will be created on next boot).", path)
        return True

    try:
        wb = load_workbook(path)
    except (InvalidFileException, PermissionError) as exc:
        logger.warning("Step 3: cannot open %s (%s). Skipping Excel rebuild.", path, exc)
        return False

    mutated = False

    # 3a. Rename legacy tabs.
    for old, new in LEGACY_TAB_RENAMES.items():
        if old != new and old in wb.sheetnames and new not in wb.sheetnames:
            logger.info("Renaming Excel tab %r -> %r", old, new)
            wb[old].title = new
            mutated = True

    # 3b. Add any missing managed tabs (so all four exist before wiping).
    for tab in TAB_ORDER:
        if tab not in wb.sheetnames:
            logger.info("Adding missing Excel tab %r", tab)
            wb.create_sheet(title=tab)
            mutated = True

    # 3c. **Delete** every tab that isn't one of the four managed ones.
    #     This is the load-bearing change: previous runs left ghosts like
    #     "Tasks From WhatsApp1" and the un-renamed legacy tabs hanging
    #     around. We want exactly TAB_ORDER and nothing else.
    managed = set(TAB_ORDER)
    for sheet_name in list(wb.sheetnames):
        if sheet_name not in managed:
            logger.info("Removing non-managed Excel tab %r", sheet_name)
            del wb[sheet_name]
            mutated = True

    # 3d. For each managed tab: heal headers (insert blank cols where the
    #     legacy schema is detected) then wipe every data row, leaving only
    #     the header row.
    for tab in TAB_ORDER:
        ws = wb[tab]
        header = [ws.cell(1, c).value for c in range(1, max(ws.max_column, len(HEADERS)) + 1)]
        # Heal column shape if needed.
        for legacy_header, insert_at in LEGACY_SCHEMAS:
            if header[: len(legacy_header)] == legacy_header:
                logger.info(
                    "Tab %r on legacy %d-col schema; inserting %d blank col(s).",
                    tab, len(legacy_header), len(insert_at),
                )
                for idx in sorted(insert_at):
                    ws.insert_cols(idx + 1)
                break
        # Rewrite header row to the canonical HEADERS regardless.
        for col, value in enumerate(HEADERS, start=1):
            ws.cell(row=1, column=col, value=value)
        # Wipe trailing columns past HEADERS.
        for c in range(ws.max_column, len(HEADERS), -1):
            ws.cell(row=1, column=c).value = None
        # Wipe every data row (rows 2..end).
        if ws.max_row >= 2:
            ws.delete_rows(2, ws.max_row - 1)
        mutated = True

    # 3e. Reorder to canonical order.
    wb._sheets = [wb[t] for t in TAB_ORDER]  # type: ignore[attr-defined]

    if mutated:
        try:
            wb.save(path)
            logger.info("Excel rebuilt: %s now on canonical 4-tab / 13-col layout.", path)
        except PermissionError:
            logger.warning(
                "Could not save %s — close it in Excel and re-run the migration.", path
            )
            return False

    # Reset the singleton flag so subsequent imports re-bootstrap.
    ExcelMirror._initialised = False  # type: ignore[attr-defined]
    return True


def main() -> int:
    print("=== Personal AI OS — v2 schema migration ===")
    s1 = step1_backfill_db()
    s2 = step2_reset_sync_state()
    ok = step3_rebuild_excel()

    print()
    print("Summary:")
    print(f"  Step 1 backfill : date_given={s1['date_given']}, source_link={s1['source_link']}, "
          f"source_detail={s1['source_detail']}, spoc_contact={s1['spoc_contact']}")
    print(f"  Step 2 reset    : {s2} task(s) marked unsynced")
    print(f"  Step 3 Excel    : {'rebuilt' if ok else 'SKIPPED (see log)'}")
    print()
    print("Next steps:")
    print("  1. (Optional but recommended) In Google Sheets, manually delete every")
    print("     DATA row in each tab (keep the header row). This avoids duplicates.")
    print("  2. Restart:  python main.py")
    print("     The forward sync worker will repopulate everything from the DB.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
