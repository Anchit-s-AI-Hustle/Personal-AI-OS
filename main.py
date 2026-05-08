"""
Personal AI OS — entrypoint.

Boots every background worker and blocks until Ctrl+C / SIGTERM is
received, then shuts everything down cleanly.

    python main.py
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Optional

# Force UTF-8 stdout/stderr so non-ASCII characters in log messages
# (em-dashes, accented names, Hindi transcripts) display correctly on
# Windows PowerShell, which defaults to the OEM codepage.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Silence noisy huggingface_hub warnings BEFORE faster_whisper is imported.
# Both are cosmetic on Windows and have nothing to do with our pipeline.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "0")  # keep progress bar; kill warnings only
warnings.filterwarnings(
    "ignore",
    message=r".*sending unauthenticated requests to the HF Hub.*",
)
warnings.filterwarnings(
    "ignore",
    message=r".*cache-system uses symlinks by default.*",
)


def _preflight() -> None:
    """
    Fail loudly with actionable advice BEFORE third-party imports run.
    The most common pebble in this repo is running `python main.py` from
    a fresh terminal that hasn't activated the venv — which causes a
    ModuleNotFoundError on the very next line.
    """
    project_root = Path(__file__).resolve().parent
    venv_python = project_root / ".venv" / "Scripts" / "python.exe"
    current_python = Path(sys.executable).resolve()

    # Heuristic: if a project-local .venv exists but we're not running its
    # interpreter, the user almost certainly forgot to activate it.
    if venv_python.exists() and current_python != venv_python.resolve():
        # Try to import a representative third-party dep. If it works, we
        # assume the current interpreter has its own copy and let it
        # through — no need to nag.
        try:
            # pyrefly: ignore [missing-import]
            import tenacity  # noqa: F401
            return
        except ImportError:
            print(
                "\n"
                "================================================================\n"
                "  Personal AI OS: virtualenv not activated.\n"
                "================================================================\n"
                f"  Running with : {current_python}\n"
                f"  Expected     : {venv_python}\n"
                "\n"
                "  Fix it from this PowerShell session:\n"
                "      .\\.venv\\Scripts\\Activate.ps1\n"
                "      python main.py\n"
                "\n"
                "  ...or just run the venv python directly:\n"
                "      .\\.venv\\Scripts\\python.exe main.py\n"
                "================================================================\n",
                file=sys.stderr,
            )
            sys.exit(2)


_preflight()

from config import settings  # noqa: E402,F401  -- importing validates env vars
from chat import ChatPoller  # noqa: E402
from database import get_db  # noqa: E402
from gmail import GmailPoller  # noqa: E402
from meetings import MeetingPipeline  # noqa: E402
from services import ChatService, DailySummaryWorker, EmailService  # noqa: E402
from sheets import SheetsSyncWorker  # noqa: E402
from utils.logger import get_logger, setup_logging  # noqa: E402

setup_logging()
logger = get_logger("main")


class PersonalAIOS:
    def __init__(
        self,
        *,
        enable_email: bool,
        enable_meetings: bool,
        enable_chat: bool,
    ) -> None:
        self.stop_event = threading.Event()
        self._enable_email = enable_email
        self._enable_meetings = enable_meetings
        self._enable_chat = enable_chat
        self._gmail_poller: Optional[GmailPoller] = None
        self._chat_poller: Optional[ChatPoller] = None
        self._sheets_worker: Optional[SheetsSyncWorker] = None
        self._meeting_pipeline: Optional[MeetingPipeline] = None
        self._daily_worker: Optional[DailySummaryWorker] = None

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        # Make sure the DB exists before any worker tries to write to it.
        get_db()

        if self._enable_email:
            email_service = EmailService()
            self._gmail_poller = GmailPoller(
                on_message=email_service.process_message,
                stop_event=self.stop_event,
            )
            self._gmail_poller.start()
        else:
            logger.info("Email module disabled by flag.")

        if self._enable_chat and settings.enable_chat_poller:
            chat_service = ChatService()
            self._chat_poller = ChatPoller(
                on_message=chat_service.process_message,
                stop_event=self.stop_event,
            )
            self._chat_poller.start()
        else:
            logger.info("Chat module disabled by flag or config.")

        # Sheets sync runs whenever ANY producer is on.
        any_producer = (
            self._enable_email
            or (self._enable_meetings and settings.enable_meeting_capture)
            or (self._enable_chat and settings.enable_chat_poller)
        )
        if any_producer:
            self._sheets_worker = SheetsSyncWorker(stop_event=self.stop_event)
            self._sheets_worker.start()
        else:
            logger.info("Sheets sync skipped: nothing to push.")

        if self._enable_meetings and settings.enable_meeting_capture:
            self._meeting_pipeline = MeetingPipeline(stop_event=self.stop_event)
            self._meeting_pipeline.start()
        else:
            logger.info("Meeting module disabled by flag or config.")

        self._daily_worker = DailySummaryWorker(
            stop_event=self.stop_event, hour=settings.daily_summary_hour
        )
        self._daily_worker.start()

        logger.info("Personal AI OS is up. Press Ctrl+C to stop.")

    def wait(self) -> None:
        # Block the main thread; daemon threads keep the work going.
        try:
            while not self.stop_event.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            logger.info("Ctrl+C received.")

    def shutdown(self) -> None:
        if self.stop_event.is_set():
            return
        logger.info("Shutting down Personal AI OS...")
        self.stop_event.set()

        if self._meeting_pipeline is not None:
            self._meeting_pipeline.shutdown()

        for worker in (
            self._gmail_poller,
            self._chat_poller,
            self._sheets_worker,
            self._daily_worker,
        ):
            if worker is not None and worker.is_alive():
                worker.join(timeout=10.0)

        # Final flush of any tasks captured at the last second.
        if self._sheets_worker is not None:
            try:
                self._sheets_worker.flush_once()
            except Exception:
                logger.exception("Final sheets flush failed.")

        logger.info("Shutdown complete.")


def _install_signal_handlers(app: PersonalAIOS) -> None:
    def _handle(signum, _frame) -> None:
        logger.info("Received signal %s.", signum)
        app.stop_event.set()

    signal.signal(signal.SIGINT, _handle)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _handle)
        except (ValueError, OSError):
            # SIGTERM can't be installed in some restricted threads on Windows.
            pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Personal AI OS")
    p.add_argument("--no-email", action="store_true", help="disable Gmail polling")
    p.add_argument("--no-meetings", action="store_true", help="disable audio capture")
    p.add_argument("--no-chat", action="store_true", help="disable Google Chat polling")
    p.add_argument(
        "--reset-initial-scan",
        action="store_true",
        help="Delete the initial-scan sentinel so the historical sweep runs again on this boot.",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(list(argv if argv is not None else sys.argv[1:]))

    if args.reset_initial_scan:
        sentinel = settings.database_path.parent / ".initial_scan_done"
        if sentinel.exists():
            sentinel.unlink()
            print(f"[main] Removed sentinel {sentinel}; historical scan will run on this boot.")
        else:
            print(f"[main] No sentinel at {sentinel} — initial scan was not done yet.")

    app = PersonalAIOS(
        enable_email=not args.no_email,
        enable_meetings=not args.no_meetings,
        enable_chat=not args.no_chat,
    )
    _install_signal_handlers(app)

    try:
        app.start()
        app.wait()
    except Exception:
        logger.exception("Fatal error in main loop.")
        return 1
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
