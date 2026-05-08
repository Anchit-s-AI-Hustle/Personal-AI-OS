-- =====================================================
-- Personal AI OS — SQLite schema
-- =====================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Every Gmail message we have already inspected. Acts as the dedup gate.
CREATE TABLE IF NOT EXISTS processed_emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT NOT NULL UNIQUE,
    thread_id       TEXT,
    subject         TEXT,
    sender          TEXT,
    received_at     TEXT,           -- ISO 8601, derived from internalDate
    processed_at    TEXT NOT NULL,  -- ISO 8601, when WE finished processing
    summary         TEXT,
    status          TEXT NOT NULL DEFAULT 'processed' -- processed | failed | skipped
);

CREATE INDEX IF NOT EXISTS idx_emails_thread ON processed_emails(thread_id);
CREATE INDEX IF NOT EXISTS idx_emails_received ON processed_emails(received_at);

-- A meeting/conversation session — one logical recording window.
CREATE TABLE IF NOT EXISTS meeting_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL UNIQUE,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    full_summary    TEXT,
    insights_json   TEXT,
    status          TEXT NOT NULL DEFAULT 'active' -- active | finalized | failed
);

-- Individual transcribed chunks within a session.
CREATE TABLE IF NOT EXISTS transcript_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT NOT NULL,
    transcript      TEXT NOT NULL,
    language        TEXT,
    audio_path      TEXT,
    summary         TEXT,
    insights_json   TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(session_id, chunk_index),
    FOREIGN KEY(session_id) REFERENCES meeting_sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_chunks_session ON transcript_chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_started ON transcript_chunks(started_at);

-- Free-text search over transcripts (auto-populated by triggers below).
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_search USING fts5(
    session_id,
    chunk_index UNINDEXED,
    transcript,
    summary,
    content='transcript_chunks',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS transcript_chunks_ai AFTER INSERT ON transcript_chunks
BEGIN
    INSERT INTO transcript_search(rowid, session_id, chunk_index, transcript, summary)
    VALUES (new.id, new.session_id, new.chunk_index, new.transcript, COALESCE(new.summary, ''));
END;

CREATE TRIGGER IF NOT EXISTS transcript_chunks_ad AFTER DELETE ON transcript_chunks
BEGIN
    INSERT INTO transcript_search(transcript_search, rowid, session_id, chunk_index, transcript, summary)
    VALUES('delete', old.id, old.session_id, old.chunk_index, old.transcript, COALESCE(old.summary, ''));
END;

CREATE TRIGGER IF NOT EXISTS transcript_chunks_au AFTER UPDATE ON transcript_chunks
BEGIN
    INSERT INTO transcript_search(transcript_search, rowid, session_id, chunk_index, transcript, summary)
    VALUES('delete', old.id, old.session_id, old.chunk_index, old.transcript, COALESCE(old.summary, ''));
    INSERT INTO transcript_search(rowid, session_id, chunk_index, transcript, summary)
    VALUES (new.id, new.session_id, new.chunk_index, new.transcript, COALESCE(new.summary, ''));
END;

-- Tasks extracted from any source. dedupe_hash prevents re-pushing the same task.
-- task_description, rationale, growth_pillar, sheet_row_source and sheet_row_all
-- are added by the idempotent migration in database/db.py if missing on
-- existing installs.
CREATE TABLE IF NOT EXISTS extracted_tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type         TEXT NOT NULL,   -- Email | Meeting | Conversation
    source_ref_id       TEXT NOT NULL,   -- gmail_message_id or session_id:chunk
    task                TEXT NOT NULL,   -- task heading
    task_description    TEXT,
    rationale           TEXT,
    growth_pillar       TEXT,
    deadline            TEXT,
    urgency             TEXT NOT NULL,   -- Low | Medium | High | Critical (= "Priority")
    sender_or_speaker   TEXT,
    summary             TEXT,
    status              TEXT NOT NULL DEFAULT 'open', -- open | done | dropped
    created_at          TEXT NOT NULL,
    synced_to_sheets    INTEGER NOT NULL DEFAULT 0,
    sheet_row           INTEGER,         -- legacy single-tab row
    sheet_row_source    INTEGER,         -- row in source-specific tab
    sheet_row_all       INTEGER,         -- row in 'All Tasks' tab
    dedupe_hash         TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_tasks_synced ON extracted_tasks(synced_to_sheets);
CREATE INDEX IF NOT EXISTS idx_tasks_source ON extracted_tasks(source_type, source_ref_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON extracted_tasks(status);

-- Operational telemetry — useful for debugging when running headless.
CREATE TABLE IF NOT EXISTS processing_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    level           TEXT NOT NULL,
    component       TEXT NOT NULL,
    message         TEXT NOT NULL,
    context_json    TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proc_logs_created ON processing_logs(created_at);

-- Daily strategic summaries.
CREATE TABLE IF NOT EXISTS daily_summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL UNIQUE,   -- YYYY-MM-DD
    summary     TEXT NOT NULL,
    insights_json TEXT,
    created_at  TEXT NOT NULL
);
