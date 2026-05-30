"""
One-shot backfill for tasks recorded before recent column additions.

What this fixes (in-place, on the SQLite DB only — does not rewrite the
Google Sheet or tasks.xlsx; just re-marks rows as unsynced where data
changed, so the next sync flush pushes the updated values):

  1. `date_given` blank          -> copy from `created_at`
  2. `spoc_contact` blank        -> attempt to recover an email/phone
                                    from `source_detail` ("from <name>
                                    <addr@x>"), else from `summary`
  3. `source_link` blank for     -> rebuild from source_ref_id (which
     Email rows                     was the gmail_message_id)

For Meeting / Chat rows we cannot reconstruct missing data — leave as is.

Run: python -m scripts.backfill_columns
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Allow `python scripts/backfill_columns.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import get_db  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger("backfill")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d[\s\-.]?){8,15}")


def _recover_contact(*sources: str | None) -> str | None:
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


def main() -> int:
    db = get_db()
    rows = db.fetchall(
        """
        SELECT id, source_type, source_ref_id, source_detail, source_link,
               date_given, summary, spoc_contact, created_at
          FROM extracted_tasks
        """
    )

    fixed_date = fixed_contact = fixed_link = 0

    for r in rows:
        sets: list[str] = []
        params: list = []

        if not (r["date_given"] or "").strip() and r["created_at"]:
            sets.append("date_given = ?")
            params.append(r["created_at"])
            fixed_date += 1

        if not (r["spoc_contact"] or "").strip():
            recovered = _recover_contact(r["source_detail"], r["summary"])
            if recovered:
                sets.append("spoc_contact = ?")
                params.append(recovered)
                fixed_contact += 1

        if (
            (r["source_type"] or "").lower() == "email"
            and not (r["source_link"] or "").strip()
            and r["source_ref_id"]
        ):
            sets.append("source_link = ?")
            params.append(_email_link(r["source_ref_id"]))
            fixed_link += 1

        if not sets:
            continue

        # Force a re-push to Sheets/Excel so the surface reflects the fix.
        sets.append("synced_to_sheets = 0")
        params.append(r["id"])
        db.execute(
            f"UPDATE extracted_tasks SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

    logger.info(
        "Backfill complete: %d date_given, %d spoc_contact, %d source_link patched.",
        fixed_date,
        fixed_contact,
        fixed_link,
    )
    print(
        f"date_given: {fixed_date}  spoc_contact: {fixed_contact}  "
        f"source_link: {fixed_link}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
