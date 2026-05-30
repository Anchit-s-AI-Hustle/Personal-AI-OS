# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Personal AI OS — a single-user Windows background service that ingests work from Gmail, Google Chat, and the laptop microphone, runs every input through an LLM to extract actionable tasks, and dual-writes them to a Google Sheet and `tasks.xlsx`. Tuned for D2C growth work at Vahdam India (the LLM prompts hard-gate on Vahdam business context).

`ARCHITECTURE.md` is the authoritative deep dive — read it before non-trivial changes. `README.md` covers user-facing setup.

## Common commands

All commands assume the project venv is active (`.\.venv\Scripts\Activate.ps1`). `main.py` has a preflight that exits with instructions if the venv isn't active.

```powershell
# Run the full service (all workers)
python main.py

# Disable specific producers
python main.py --no-email
python main.py --no-meetings
python main.py --no-chat

# Re-run the historical Gmail sweep on next boot
python main.py --reset-initial-scan

# One-shot maintenance scripts
python -m scripts.migrate_v2          # reshape DB after schema changes
python -m scripts.rebuild_live_sheet  # wipe + recreate the 3 sheet tabs, reapply formatting
python -m scripts.backfill_columns    # recover missing date_given/source_link/spoc_contact

# Sheets/DB resync (after rebuild_live_sheet)
python -c "from database import get_db; get_db().execute('UPDATE extracted_tasks SET synced_to_sheets=0, sheet_row_all=NULL, sheet_row_source=NULL')"

# List available mic devices
python -m sounddevice
```

There is no test suite, linter, or formatter configured.

## Architecture in one screen

Single Python process, multiple daemon threads, one SQLite file, one live Google Sheet, one local `tasks.xlsx` mirror.

```
Gmail poll (30s) ─┐
Chat poll (60s)  ─┼─► ai/extractor.py ─► services/task_service.py ─► SQLite ─► sheets/sync.py ─► Google Sheet + tasks.xlsx
Mic capture     ─┘                                                          ◄─ sheets/reverse_sync.py (status edits flow back)
```

Key invariants worth knowing before editing:

- **All producers share one OAuth token** (`token.json`, scopes for Gmail + Sheets + Chat). Adding a scope invalidates the token; first boot re-prompts. See `gmail/auth.py`.
- **LLM provider is a chain**, not a single client. `LLM_PROVIDER=gemini,groq,ollama` builds a `RoutedClient` in `ai/routed_client.py` that falls through on `QuotaExhaustedError` only — other errors propagate. Per-provider cooldowns are tracked in-memory.
- **Two-line dedup** for tasks:
  1. `extracted_tasks.dedupe_hash` UNIQUE on `(source_type, source_ref_id, task_heading)` — catches reprocessing.
  2. `services/task_service.py::normalize_heading` + SPOC equality — catches cross-source duplicates and merges them by appending to the `all_updates` column instead of inserting a new row. Edit `normalize_heading` carefully: verb-synonym maps and stopword list are load-bearing.
- **Sheet column O is the hidden ISO sort key.** Google Sheets can't chronologically sort our pretty-formatted "12th May 2026" strings, so forward sync writes raw ISO into O and calls `sort_tab_desc_by_sort_key` after every push. Don't repurpose column O.
- **Reverse sync is content-keyed**, not row-keyed. It looks up DB tasks by `(normalize_heading(A), lower(I))` because forward sync re-sorts the sheet every push, making row indices unstable. Renaming a task's heading in the sheet breaks the link.
- **DB migrations live in `database/db.py::_migrate`** and run every boot — they ALTER TABLE to add any missing columns. New columns get added there, not in `schema.sql` alone.
- **Vahdam-only gate** is enforced inside the LLM prompts in `ai/prompts.py` (personal banking, food delivery, etc. must produce empty task lists). If extraction is misclassifying something, the fix is usually a prompt edit, not a code edit.
- **Identifier hygiene**: `utils/identifiers.py::clean_identifier` rejects `users/12345`, `(unknown)`, opaque IDs as SPOC values; `transcription/lexicon.py::CANONICAL_DISPLAY` folds full names to short forms (`"Anchit Tandon"` → `"Anchit"`).
- **Silence skip**: mic chunks carry `is_silent`/`peak_amplitude`; silent chunks bypass Whisper entirely to avoid hallucinated "Thank you" transcripts. Threshold in `transcription/audio_capture.py::_SILENCE_PEAK_THRESHOLD`.

## When to restart `main.py`

Module-level constants (e.g. `TAB_ORDER`, `HEADERS` in `sheets/client.py`) and `.env` values are cached in the running interpreter. Any change to those, or to a code path inside a worker, requires Ctrl+C + restart. Wait for `Shutdown complete.` before relaunching.

## Where common decisions live

| Decision | File |
|---|---|
| Vahdam-only filter, prompt language, name canonicalisation | `ai/prompts.py` |
| Provider chain + quota fallback | `ai/routed_client.py` |
| "Same task" rule for dedup/merge | `services/task_service.py::normalize_heading` |
| Sheet tab names + column order | `sheets/client.py::HEADERS`, `TAB_ORDER` |
| Conditional formatting + frozen panes | `scripts/rebuild_live_sheet.py` |
| Source label wording ("Email from X" / "Voice memo") | `sheets/sync.py::_format_source_label` |
| `[date · person · source] body` tag format | `services/task_service.py::_format_update_line` |
| Silence threshold | `transcription/audio_capture.py::_SILENCE_PEAK_THRESHOLD` |
