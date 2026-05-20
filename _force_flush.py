"""Manually flush all unsynced tasks to Google Sheet + Excel."""
import io, sys, threading, traceback
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from database import get_db
from sheets.sync import SheetsSyncWorker
from utils.logger import setup_logging

setup_logging()
db = get_db()

print(f"Before: {len(db.unsynced_tasks(limit=10000))} unsynced tasks")
worker = SheetsSyncWorker(stop_event=threading.Event(), interval=999999)

try:
    pushed = worker.flush_once()
    print(f"flush_once() returned: {pushed}")
except Exception:
    print("flush_once() raised:")
    traceback.print_exc()

print(f"After: {len(db.unsynced_tasks(limit=10000))} unsynced tasks")
