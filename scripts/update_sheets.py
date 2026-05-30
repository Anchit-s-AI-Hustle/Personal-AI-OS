"""Ensure AI task tracker sheets contain the expected header columns in order.
Usage: copy .env.example to .env and set VAHDAM_SHEET_ID and PERSONAL_SHEET_ID
then run: python scripts/update_sheets.py --dry-run

This script only prepares and verifies headers; it requires valid Google
OAuth credentials (GOOGLE_CREDENTIALS_PATH / GOOGLE_TOKEN_PATH) configured
as per the README. It will not modify remote sheets unless --apply is used.
"""
from __future__ import annotations

import argparse
import os
from typing import List

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

EXPECTED_COLUMNS = [
    "Timestamp",
    "Source Type",
    "Task",
    "Deadline",
    "Urgency",
    "Sender/Speaker",
    "Summary",
    "Status",
    "Source Reference ID",
]


def _get_service():
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "./credentials.json")
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "./token.json")
    if not os.path.exists(creds_path):
        raise FileNotFoundError(f"Credentials not found: {creds_path}")
    if not os.path.exists(token_path):
        raise FileNotFoundError(f"OAuth token not found: {token_path}")
    # This helper expects the token.json to be present (user has consented).
    creds = Credentials.from_authorized_user_file(token_path)
    svc = build("sheets", "v4", credentials=creds)
    return svc


def ensure_header(sheet_id: str, tab_name: str = "Tasks", apply: bool = False):
    svc = _get_service()
    rng = f"'{tab_name}'!1:1"
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    values = resp.get("values", [])
    current = values[0] if values else []
    print("Current header:", current)
    if current == EXPECTED_COLUMNS:
        print("Header already correct.")
        return True
    print("Expected header:", EXPECTED_COLUMNS)
    if not apply:
        print("Dry-run: not applying changes.")
        return False
    # Apply the header row replacement
    body = {"range": rng, "values": [EXPECTED_COLUMNS]}
    svc.spreadsheets().values().update(spreadsheetId=sheet_id, range=rng, valueInputOption="RAW", body=body).execute()
    print("Header updated.")
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="Actually write changes")
    p.add_argument("--sheet", choices=["vahdam", "personal", "both"], default="both")
    args = p.parse_args()

    sheet_map = {
        "vahdam": os.getenv("VAHDAM_SHEET_ID"),
        "personal": os.getenv("PERSONAL_SHEET_ID"),
    }

    targets = []
    if args.sheet in ("both", "vahdam"):
        targets.append((sheet_map["vahdam"], "Vahdam - Task Tracker"))
    if args.sheet in ("both", "personal"):
        targets.append((sheet_map["personal"], "Tasks"))

    for sid, tab in targets:
        if not sid:
            print(f"No sheet id configured for tab {tab}; skipping.")
            continue
        success = ensure_header(sid, tab_name=tab, apply=args.apply)
        print(f"Sheet {sid} ({tab}) -> {'OK' if success else 'NOOP'}")


if __name__ == '__main__':
    main()
