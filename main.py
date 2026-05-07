"""Entry point for the Personal AI Email Intelligence system.

Usage:
    python main.py                 # Start the 5-minute scheduler loop.
    python main.py --setup-gmail   # Run interactive Gmail OAuth and exit.
    python main.py --once          # Run a single processing cycle and exit.
"""
from __future__ import annotations

import argparse
import signal
import sys

# pyrefly: ignore [missing-import]
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import get_settings
from database import ProcessedEmailStore
from services.gmail_service import GmailService
from services.pipeline import EmailIntelligencePipeline
from services.sheets_service import SheetsService
from services.task_extractor import TaskExtractor
from utils.logger import get_logger, setup_logging


def _build_pipeline() -> EmailIntelligencePipeline:
    s = get_settings()
    gmail = GmailService()
    gmail.authenticate(interactive=False)  # fail fast if token missing
    sheets = SheetsService()
    extractor = TaskExtractor()
    store = ProcessedEmailStore(s.sqlite_path)
    return EmailIntelligencePipeline(gmail, sheets, extractor, store)


def cmd_setup_gmail() -> int:
    log = get_logger("setup")
    log.info("Starting Gmail OAuth flow...")
    GmailService().authenticate(interactive=True)
    log.info("Gmail authentication complete. Token saved.")
    return 0


def cmd_once() -> int:
    log = get_logger("main")
    pipeline = _build_pipeline()
    stats = pipeline.run_once()
    log.info("One-shot run finished: %s", stats)
    return 0


def cmd_loop() -> int:
    log = get_logger("main")
    s = get_settings()
    pipeline = _build_pipeline()

    # Run immediately so the operator gets feedback on startup,
    # then every POLL_INTERVAL_MINUTES thereafter.
    log.info("Initial run before scheduling...")
    try:
        pipeline.run_once()
    except Exception as e:
        log.exception("Initial run failed: %s", e)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        pipeline.run_once,
        trigger=IntervalTrigger(minutes=s.poll_interval_minutes),
        id="email_poll",
        name="Email intelligence poll",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Graceful shutdown on Ctrl+C / SIGTERM (Windows: Ctrl+C only).
    def _stop(signum, _frame):
        log.info("Received signal %s, shutting down scheduler", signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _stop)
        except (ValueError, OSError):
            # SIGTERM handler can't be set on the main thread on some Windows setups.
            pass

    log.info("Scheduler started: every %d minute(s)", s.poll_interval_minutes)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()

    parser = argparse.ArgumentParser(description="Personal AI Email Intelligence")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--setup-gmail",
        action="store_true",
        help="Run the interactive Gmail OAuth flow and exit.",
    )
    group.add_argument(
        "--once",
        action="store_true",
        help="Run a single processing cycle and exit (useful for testing / cron).",
    )
    args = parser.parse_args(argv)

    if args.setup_gmail:
        return cmd_setup_gmail()
    if args.once:
        return cmd_once()
    return cmd_loop()


if __name__ == "__main__":
    sys.exit(main())
