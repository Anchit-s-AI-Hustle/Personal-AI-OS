"""
One-shot live Google Sheet rebuild.

Connects to the configured Google Sheet via the project's existing OAuth
token and, in a single batchUpdate, brings it onto the canonical state:

  - **Renames legacy tabs** (so existing `sheet_row_*` mappings stay
    valid for any task already pushed). Old name -> new name, in place.
  - **Deletes any tab that isn't one of the four managed tabs** in
    `TAB_ORDER`. Manually-created stragglers (e.g. "Tasks From In Person
    Discussions" if it appeared) get removed.
  - **Reorders the four managed tabs** to canonical 0..3 indexes.
  - **Resizes** each managed tab to exactly len(HEADERS) columns.
  - **Writes the canonical 14-column header row** to each managed tab.
  - **Wipes every data row** (rows 2..end), so the next forward-sync
    push from `SheetsSyncWorker` repopulates the Sheet with the cleaned
    DB state in the correct columns/tabs.

After this script: the live Sheet has 4 tabs, header rows only, ready
to receive the freshly-cleaned 137 task rows from the DB on next sync.

Run:
    python -m scripts.rebuild_live_sheet
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from sheets.client import (  # noqa: E402
    HEADERS,
    LEGACY_TAB_RENAMES,
    SORT_KEY_COL_INDEX,
    TAB_ORDER,
    get_sheets_client,
)
from utils.logger import get_logger  # noqa: E402

logger = get_logger("rebuild_live_sheet")


def _cell_letter(n: int) -> str:
    """1-based column index -> A1 letter."""
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord("A") + rem) + result
    return result


def main() -> int:
    print("=== Rebuilding live Google Sheet ===")
    client = get_sheets_client()
    svc = client._svc  # noqa: SLF001
    sheet_id = settings.google_sheet_id
    print(f"Sheet URL: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")

    # 1. Fetch current state.
    meta = client._fetch_meta()  # noqa: SLF001
    by_title = {
        s["properties"]["title"]: s["properties"]
        for s in meta.get("sheets", [])
    }
    print()
    print("Current tabs:")
    for s in meta.get("sheets", []):
        p = s["properties"]
        print(f"  - {p['title']!r} (rows={p.get('gridProperties',{}).get('rowCount')})")
    print()

    requests: list[dict] = []

    # 2. Rename legacy tabs. (Skip when the new name already exists —
    #    that means an earlier run renamed it.)
    for old, new in LEGACY_TAB_RENAMES.items():
        if old == new:
            continue
        if old in by_title and new not in by_title:
            print(f"Rename: {old!r} -> {new!r}")
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": by_title[old]["sheetId"],
                            "title": new,
                        },
                        "fields": "title",
                    }
                }
            )

    # Apply renames first so post-rename names are visible to the next steps.
    if requests:
        client._batch_update(requests)  # noqa: SLF001
        meta = client._fetch_meta()  # noqa: SLF001
        by_title = {
            s["properties"]["title"]: s["properties"]
            for s in meta.get("sheets", [])
        }
        requests = []

    # 3. Delete any tab that isn't in TAB_ORDER. (A spreadsheet must keep
    #    at least one sheet, so if we'd delete every tab, ensure_tabs()
    #    will create the missing managed ones in step 4.)
    managed = set(TAB_ORDER)
    for title, props in list(by_title.items()):
        if title not in managed:
            print(f"Delete: {title!r}")
            requests.append({"deleteSheet": {"sheetId": props["sheetId"]}})

    # 4. Create any managed tabs that don't exist yet.
    for desired_index, tab in enumerate(TAB_ORDER):
        if tab not in by_title:
            print(f"Create: {tab!r}")
            requests.append(
                {"addSheet": {"properties": {"title": tab, "index": desired_index}}}
            )

    if requests:
        client._batch_update(requests)  # noqa: SLF001
        meta = client._fetch_meta()  # noqa: SLF001
        by_title = {
            s["properties"]["title"]: s["properties"]
            for s in meta.get("sheets", [])
        }
        requests = []

    # 5. Reorder, resize columns, write headers.
    for desired_index, tab in enumerate(TAB_ORDER):
        props = by_title.get(tab)
        if props is None:
            continue
        sheet_gid = props["sheetId"]
        cur_index = props.get("index")
        cur_cols = props.get("gridProperties", {}).get("columnCount", 0)

        # 5a. Move into canonical position.
        if cur_index != desired_index:
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet_gid, "index": desired_index},
                        "fields": "index",
                    }
                }
            )
        # 5b. Resize to exactly len(HEADERS) columns. If the tab has more,
        #     trim them; if fewer, expand. (Lots of stale tabs had 26-29
        #     columns; we only want 14.)
        target_cols = len(HEADERS)
        if cur_cols != target_cols:
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_gid,
                            "gridProperties": {"columnCount": target_cols},
                        },
                        "fields": "gridProperties.columnCount",
                    }
                }
            )

    if requests:
        client._batch_update(requests)  # noqa: SLF001
        requests = []

    # 6. Wipe every data row in each managed tab AND write canonical headers.
    end_col = _cell_letter(len(HEADERS))
    for tab in TAB_ORDER:
        rng_clear = f"'{tab}'!A2:{end_col}"
        rng_header = f"'{tab}'!A1:{end_col}1"

        # Clear data rows.
        svc.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range=rng_clear, body={}
        ).execute()
        # Write headers.
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=rng_header,
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()
        print(f"Cleaned + headered: {tab!r}")

    # 7. Bold + freeze + light-grey-fill row 1 across all 4 tabs.
    #    Also force "Task Given On" (col F = index 5) and "Task Deadline"
    #    (col L = index 11) to PLAIN TEXT format so Google Sheets never
    #    auto-parses our pretty date strings ("9th May 2026, 3:49 AM")
    #    or — worse — interprets "2026-05-08 22:19" as a date serial.
    meta = client._fetch_meta()  # noqa: SLF001
    title_to_id = {
        s["properties"]["title"]: s["properties"]["sheetId"]
        for s in meta.get("sheets", [])
    }
    style_requests: list[dict] = []
    for tab in TAB_ORDER:
        gid = title_to_id.get(tab)
        if gid is None:
            continue
        style_requests.extend(
            [
                # Header styling.
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": gid,
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
                # Freeze header row.
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": gid,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
                # Plain-text format for Task Given On (col F, idx 5).
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": gid,
                            "startRowIndex": 1,
                            "startColumnIndex": 5,
                            "endColumnIndex": 6,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "TEXT"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                # Plain-text format for Task Deadline (col L, idx 11).
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": gid,
                            "startRowIndex": 1,
                            "startColumnIndex": 11,
                            "endColumnIndex": 12,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "TEXT"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                # HIDE the _iso_sort_key column (col O, idx 14).
                # It exists purely so Sheets can chronologically sort
                # the tab. The user should never see it.
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": gid,
                            "dimension": "COLUMNS",
                            "startIndex": SORT_KEY_COL_INDEX - 1,
                            "endIndex": SORT_KEY_COL_INDEX,
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                },
            ]
        )
    if style_requests:
        client._batch_update(style_requests)  # noqa: SLF001

    # 8. Conditional formatting + column widths + frozen heading column.
    #    The goal is "scan and understand at one glance":
    #      - Status=done   -> light green row + strike-through
    #      - Status=dropped-> light grey  row + strike-through
    #      - Priority=Critical -> red cell
    #      - Priority=High     -> orange cell
    #      - Priority=Medium   -> yellow cell
    #      - Priority=Low      -> grey cell
    #      - Column widths set generously for the wordy columns
    #      - Column A (Task Heading) frozen so it stays visible on horizontal scroll
    cf_requests: list[dict] = []
    # Pixel widths per column, indexed 0..14.
    col_pixels = [
        260,  # A Task Heading
        420,  # B Task Description
        90,   # C Status
        220,  # D Source
        220,  # E Source Link
        180,  # F Task Given On
        320,  # G Why We're Doing This
        140,  # H Growth Pillar
        160,  # I SPOC
        220,  # J SPOC Contact
        100,  # K Priority
        160,  # L Task Deadline
        460,  # M All Updates
        220,  # N Remarks
        # O hidden — skip
    ]
    for tab in TAB_ORDER:
        gid = title_to_id.get(tab)
        if gid is None:
            continue

        # Column widths.
        for idx, px in enumerate(col_pixels):
            cf_requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": gid,
                            "dimension": "COLUMNS",
                            "startIndex": idx,
                            "endIndex": idx + 1,
                        },
                        "properties": {"pixelSize": px},
                        "fields": "pixelSize",
                    }
                }
            )

        # Freeze column A (heading) so it stays visible when scrolling right.
        cf_requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": gid,
                        "gridProperties": {
                            "frozenRowCount": 1,
                            "frozenColumnCount": 1,
                        },
                    },
                    "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
                }
            }
        )

        # Row-level rules driven by Status column (C, idx 2).
        # "done"   -> light green row + strike-through
        # "dropped"-> light grey row + strike-through
        for status_val, fill, strike in [
            ("done",    {"red": 0.86, "green": 0.96, "blue": 0.85}, True),   # soft green
            ("dropped", {"red": 0.92, "green": 0.92, "blue": 0.92}, True),   # soft grey
        ]:
            cf_requests.append(
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{
                                "sheetId": gid,
                                "startRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": 14,  # don't bleed onto col O
                            }],
                            "booleanRule": {
                                "condition": {
                                    "type": "CUSTOM_FORMULA",
                                    "values": [
                                        {"userEnteredValue": f'=LOWER($C2)="{status_val}"'}
                                    ],
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

        # Priority cell rules on col K (idx 10).
        # Critical -> red, High -> orange, Medium -> yellow, Low -> grey.
        for prio_val, fill in [
            ("Critical", {"red": 0.96, "green": 0.80, "blue": 0.80}),
            ("High",     {"red": 0.99, "green": 0.89, "blue": 0.78}),
            ("Medium",   {"red": 1.00, "green": 0.97, "blue": 0.82}),
            ("Low",      {"red": 0.93, "green": 0.93, "blue": 0.93}),
        ]:
            cf_requests.append(
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{
                                "sheetId": gid,
                                "startRowIndex": 1,
                                "startColumnIndex": 10,
                                "endColumnIndex": 11,
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

        # Wrap text in the wordy columns so long entries stay readable
        # without forcing the user to expand row heights.
        for wide_col_idx in (1, 6, 12):  # Description, Why, All Updates
            cf_requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": gid,
                            "startRowIndex": 1,
                            "startColumnIndex": wide_col_idx,
                            "endColumnIndex": wide_col_idx + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {"wrapStrategy": "WRAP"}
                        },
                        "fields": "userEnteredFormat.wrapStrategy",
                    }
                }
            )

    if cf_requests:
        client._batch_update(cf_requests)  # noqa: SLF001

    print()
    print("Live Sheet rebuilt. Final state:")
    for tab in TAB_ORDER:
        print(f"  - {tab!r}")
    print()
    print("Next: run forward-sync push (this repo's SheetsSyncWorker.flush_once)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
