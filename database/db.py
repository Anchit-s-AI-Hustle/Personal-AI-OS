"""
SQLite wrapper.

Thread-safe via a per-call connection (SQLite is fine for our concurrency
profile — short bursts of writes from a few threads, dominated by I/O
elsewhere). WAL mode is set in `schema.sql`.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_init_lock = threading.Lock()
_initialised = False


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.database_path,
        timeout=30.0,
        isolation_level=None,  # autocommit; use explicit BEGIN/COMMIT in transactions()
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """
    Idempotent column-level migration.

    SQLite's CREATE TABLE IF NOT EXISTS won't add columns to a pre-existing
    table, so we explicitly inspect the schema and ALTER TABLE for any new
    columns the rest of the code now expects.
    """
    cur = conn.execute("PRAGMA table_info(extracted_tasks)")
    existing = {row[1] for row in cur.fetchall()}

    additions = [
        ("task_description", "TEXT"),
        ("rationale",        "TEXT"),
        ("growth_pillar",    "TEXT"),
        ("sheet_row_source", "INTEGER"),
        ("sheet_row_all",    "INTEGER"),
        ("source_detail",    "TEXT"),    # human-readable origin label
        ("source_link",      "TEXT"),    # direct URL to the original message
        ("date_given",       "TEXT"),    # ISO 8601 — when the source was created
        ("spoc_contact",     "TEXT"),    # email / phone if known, otherwise null
    ]
    for col, col_type in additions:
        if col not in existing:
            conn.execute(f"ALTER TABLE extracted_tasks ADD COLUMN {col} {col_type}")
            logger.info("Migrated extracted_tasks: added column %s", col)


def _init_schema() -> None:
    global _initialised
    with _init_lock:
        if _initialised:
            return
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        conn = _connect()
        try:
            conn.executescript(sql)
            _migrate(conn)
        finally:
            conn.close()
        logger.info("Database schema ready at %s", settings.database_path)
        _initialised = True


class Database:
    """Thin convenience layer around sqlite3."""

    def __init__(self) -> None:
        _init_schema()

    # --- low level -----------------------------------------------------------

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = _connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.connection() as conn:
            conn.execute(sql, tuple(params))

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self.connection() as conn:
            cur = conn.execute(sql, tuple(params))
            return cur.fetchone()

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.connection() as conn:
            cur = conn.execute(sql, tuple(params))
            return list(cur.fetchall())

    # --- emails --------------------------------------------------------------

    def email_already_processed(self, gmail_message_id: str) -> bool:
        row = self.fetchone(
            "SELECT 1 FROM processed_emails WHERE gmail_message_id = ?",
            (gmail_message_id,),
        )
        return row is not None

    def record_processed_email(
        self,
        *,
        gmail_message_id: str,
        thread_id: Optional[str],
        subject: Optional[str],
        sender: Optional[str],
        received_at: Optional[str],
        summary: Optional[str],
        status: str = "processed",
    ) -> None:
        self.execute(
            """
            INSERT INTO processed_emails(
                gmail_message_id, thread_id, subject, sender,
                received_at, processed_at, summary, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gmail_message_id) DO UPDATE SET
                processed_at = excluded.processed_at,
                summary      = excluded.summary,
                status       = excluded.status
            """,
            (
                gmail_message_id,
                thread_id,
                subject,
                sender,
                received_at,
                _utcnow(),
                summary,
                status,
            ),
        )

    # --- meetings ------------------------------------------------------------

    def start_meeting_session(self, session_id: str) -> None:
        self.execute(
            """
            INSERT OR IGNORE INTO meeting_sessions(session_id, started_at, status)
            VALUES (?, ?, 'active')
            """,
            (session_id, _utcnow()),
        )

    def finalize_meeting_session(
        self,
        session_id: str,
        full_summary: Optional[str],
        insights: Optional[dict],
    ) -> None:
        self.execute(
            """
            UPDATE meeting_sessions
               SET ended_at      = ?,
                   full_summary  = ?,
                   insights_json = ?,
                   status        = 'finalized'
             WHERE session_id    = ?
            """,
            (
                _utcnow(),
                full_summary,
                json.dumps(insights) if insights is not None else None,
                session_id,
            ),
        )

    def insert_transcript_chunk(
        self,
        *,
        session_id: str,
        chunk_index: int,
        started_at: str,
        ended_at: str,
        transcript: str,
        language: Optional[str],
        audio_path: Optional[str],
        summary: Optional[str] = None,
        insights: Optional[dict] = None,
    ) -> int:
        with self.connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO transcript_chunks(
                    session_id, chunk_index, started_at, ended_at,
                    transcript, language, audio_path, summary, insights_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, chunk_index) DO UPDATE SET
                    transcript    = excluded.transcript,
                    language      = excluded.language,
                    audio_path    = excluded.audio_path,
                    summary       = excluded.summary,
                    insights_json = excluded.insights_json
                """,
                (
                    session_id,
                    chunk_index,
                    started_at,
                    ended_at,
                    transcript,
                    language,
                    audio_path,
                    summary,
                    json.dumps(insights) if insights else None,
                    _utcnow(),
                ),
            )
            return int(cur.lastrowid)

    def search_transcripts(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        if not query.strip():
            return []
        return self.fetchall(
            """
            SELECT c.session_id, c.chunk_index, c.started_at, c.transcript, c.summary
              FROM transcript_search s
              JOIN transcript_chunks c ON c.id = s.rowid
             WHERE transcript_search MATCH ?
             ORDER BY c.started_at DESC
             LIMIT ?
            """,
            (query, limit),
        )

    # --- tasks ---------------------------------------------------------------

    @staticmethod
    def make_task_dedupe_hash(source_type: str, source_ref_id: str, task: str) -> str:
        normalised = f"{source_type.lower().strip()}|{source_ref_id.strip()}|{task.lower().strip()}"
        return hashlib.sha256(normalised.encode("utf-8")).hexdigest()

    def insert_task(
        self,
        *,
        source_type: str,
        source_ref_id: str,
        task: str,
        deadline: Optional[str],
        urgency: str,
        sender_or_speaker: Optional[str],
        summary: Optional[str],
        task_description: Optional[str] = None,
        rationale: Optional[str] = None,
        growth_pillar: Optional[str] = None,
        source_detail: Optional[str] = None,
        source_link: Optional[str] = None,
        date_given: Optional[str] = None,
        spoc_contact: Optional[str] = None,
    ) -> Optional[int]:
        """Returns the inserted row id, or None if it was a duplicate."""
        dedupe_hash = self.make_task_dedupe_hash(source_type, source_ref_id, task)
        with self.connection() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO extracted_tasks(
                    source_type, source_ref_id, task, task_description, rationale,
                    growth_pillar, deadline, urgency, sender_or_speaker, summary,
                    source_detail, source_link, date_given, spoc_contact,
                    status, created_at, dedupe_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    source_type,
                    source_ref_id,
                    task,
                    task_description,
                    rationale,
                    growth_pillar,
                    deadline,
                    urgency,
                    sender_or_speaker,
                    summary,
                    source_detail,
                    source_link,
                    date_given,
                    spoc_contact,
                    _utcnow(),
                    dedupe_hash,
                ),
            )
            return int(cur.lastrowid) if cur.rowcount > 0 else None

    def unsynced_tasks(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.fetchall(
            """
            SELECT id, source_type, source_ref_id, source_detail, source_link,
                   date_given, task, task_description, rationale, growth_pillar,
                   deadline, urgency, sender_or_speaker, spoc_contact, summary,
                   status, created_at
              FROM extracted_tasks
             WHERE synced_to_sheets = 0
             ORDER BY created_at ASC
             LIMIT ?
            """,
            (limit,),
        )

    def mark_tasks_synced(
        self,
        task_ids: Iterable[int],
        *,
        all_tasks_starting_row: Optional[int] = None,
        source_starting_row: Optional[int] = None,
    ) -> None:
        """
        Mark tasks as synced. If row numbers are provided, persist them so we
        can later update status across both tabs without re-finding rows.

        `task_ids` is iterated IN ORDER — both starting_row args, if given,
        are interpreted as the row of the FIRST id, and incremented for each
        subsequent id. Pass None to skip storing that mapping.
        """
        ids = list(task_ids)
        if not ids:
            return
        with self.transaction() as conn:
            for idx, task_id in enumerate(ids):
                sets = ["synced_to_sheets = 1"]
                params: list[Any] = []
                if all_tasks_starting_row is not None:
                    sets.append("sheet_row_all = ?")
                    params.append(all_tasks_starting_row + idx)
                if source_starting_row is not None:
                    sets.append("sheet_row_source = ?")
                    params.append(source_starting_row + idx)
                params.append(task_id)
                conn.execute(
                    f"UPDATE extracted_tasks SET {', '.join(sets)} WHERE id = ?",
                    tuple(params),
                )

    def update_task_status(self, task_id: int, status: str) -> None:
        if status not in {"open", "done", "dropped"}:
            raise ValueError(f"Invalid status {status!r}")
        self.execute(
            "UPDATE extracted_tasks SET status = ? WHERE id = ?",
            (status, task_id),
        )

    def recent_tasks(self, since_iso: str) -> list[sqlite3.Row]:
        return self.fetchall(
            """
            SELECT id, source_type, task, deadline, urgency, sender_or_speaker,
                   summary, status, created_at
              FROM extracted_tasks
             WHERE created_at >= ?
             ORDER BY created_at DESC
            """,
            (since_iso,),
        )

    # --- daily summaries -----------------------------------------------------

    def upsert_daily_summary(self, date_str: str, summary: str, insights: Optional[dict]) -> None:
        self.execute(
            """
            INSERT INTO daily_summaries(date, summary, insights_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                summary       = excluded.summary,
                insights_json = excluded.insights_json,
                created_at    = excluded.created_at
            """,
            (
                date_str,
                summary,
                json.dumps(insights) if insights else None,
                _utcnow(),
            ),
        )

    # --- processing logs -----------------------------------------------------

    def log_event(
        self,
        level: str,
        component: str,
        message: str,
        context: Optional[dict] = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO processing_logs(level, component, message, context_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                level.upper(),
                component,
                message,
                json.dumps(context) if context else None,
                _utcnow(),
            ),
        )


_db_singleton: Optional[Database] = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db_singleton
    if _db_singleton is None:
        with _db_lock:
            if _db_singleton is None:
                _db_singleton = Database()
    return _db_singleton
