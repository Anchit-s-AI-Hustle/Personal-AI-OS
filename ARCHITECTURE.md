# Personal AI OS — Architecture

A single-user background service that ingests work from Gmail, Google
Chat, and your laptop mic, runs every input through an LLM to extract
actionable tasks, and writes them to a Google Sheet + a local Excel
mirror. Built for Anchit Tandon at Vahdam India; the prompts are
tuned for D2C growth work (Shopify, Amazon US/IN, retention, paid
media).

---

## 1. What it does, end-to-end

```
                     ┌──────────────────────────────┐
                     │   Personal AI OS (one Py    │
                     │    process on the laptop)    │
                     └──────────────────────────────┘
                                    │
        ┌───────────────────────────┼────────────────────────────┐
        │                           │                            │
   POLL EMAIL                  POLL CHAT                    LISTEN AUDIO
   (every 30s)                (every 60s)                   (continuous)
        │                           │                            │
        ▼                           ▼                            ▼
   gmail/poller.py             chat/poller.py             transcription/
   ↳ services/email_service    ↳ services/chat_service     audio_capture.py
                                                          ↳ Whisper STT
                                                          ↳ services/meeting_service
        │                           │                            │
        └───────────────┬───────────┴────────────────┬───────────┘
                        │                            │
                        ▼                            ▼
                 ai/extractor.py — LLM extracts structured tasks
                        │
                        ▼
                 services/task_service.py — dedup + persist
                        │
                        ▼
                 SQLite DB (data/personal_ai_os.db)
                        │
            ┌───────────┴────────────┐
            ▼                        ▼
   sheets/sync.py            sheets/excel_mirror.py
   (Google Sheet)            (tasks.xlsx in repo)
            │
            ▼
   sheets/reverse_sync.py
   (pulls user's status edits back to DB)
```

Three permanent tabs on the live Sheet:

1. **Master Task List** — every task, every source
2. **Tasks from Gmail** — Email + Google Chat (DMs, Spaces, Groups)
3. **Tasks from In-Person Meetings** — voice-memo audio captured live

Every task is dual-written to its source tab AND to Master.

---

## 2. Module map

| Folder | Role |
|---|---|
| `main.py` | Entry point. Boots all background workers, installs signal handlers, blocks until Ctrl+C. |
| `config/settings.py` | Loads `.env`, validates required keys, exposes typed `Settings` dataclass. |
| `database/` | SQLite schema, migrations, CRUD. Single source of truth for tasks. |
| `gmail/` | Gmail API client + polling loop. Reuses shared OAuth token. |
| `chat/` | Google Chat API client + polling loop. Reuses same OAuth token. |
| `transcription/` | Always-on mic capture + Whisper STT (local OR Groq cloud). |
| `meetings/` | Pipeline that wires mic chunks -> Whisper -> meeting_service. |
| `ai/` | LLM clients (Gemini, Groq, Ollama), a routed-client for fallback, prompts, extractor. |
| `services/` | Glue between sources, the LLM, and the DB. One service per source: `email_service`, `chat_service`, `meeting_service`. Plus `task_service` (persistence), `notifier` (Gmail digest), `daily_summary`. |
| `sheets/` | Forward sync (DB → Sheet+Excel), reverse sync (Sheet → DB), local Excel mirror. |
| `storage/` | Disk persistence helpers (transcripts, audio chunks). |
| `utils/` | Retry logic (tenacity), structured logging, identifier hygiene. |
| `scripts/` | One-shot ops: `migrate_v2`, `rebuild_live_sheet`, `backfill_columns`. |

---

## 3. The runtime

### 3.1 Boot sequence (`main.py`)

1. Preflight: confirm we're running inside `.venv`, exit with a friendly message if not.
2. Load `config.settings` (validates `.env` + required API keys).
3. Initialise the DB schema (idempotent migrations in `database/db.py:_migrate`).
4. Start workers as background daemon threads:
   - `GmailPoller` — every 30s, reads `is:unread newer_than:2d` and calls `EmailService.process_message` per message.
   - `ChatPoller` — every 60s, iterates Spaces + DMs + Groups, tracks per-space high-water-mark in `processing_logs`.
   - `MeetingPipeline` — wraps `AudioCapture` + `MeetingService`.
   - `SheetsSyncWorker` — every 30s, pushes any unsynced task to Sheets+Excel; sorts each tab DESC by the hidden ISO key after every push.
   - `ReverseSyncWorker` — every 60s, reads Master Task List, matches each visible row to a DB task by `(normalized_heading, SPOC)`, copies status changes back.
   - `DailySummaryWorker` — once a day at `DAILY_SUMMARY_HOUR` (21:00 local), generates a strategic briefing via the LLM, writes to `daily_summaries`, and emails a digest of today's tasks to the user.
5. Install SIGINT / SIGTERM handlers that set the shared `stop_event`.
6. Block on a 1-second sleep loop until `stop_event` fires; then graceful shutdown.

### 3.2 Shutdown

On Ctrl+C:
1. `stop_event.set()` — every worker's poll loop sees it within its current sleep interval.
2. Meeting pipeline drains its queue and flushes the last partial audio chunk.
3. Final `SheetsSyncWorker.flush_once()` pushes any tasks queued during shutdown.
4. Each thread joined with a 10s timeout.

---

## 4. Data sources

### 4.1 Gmail (`gmail/poller.py` + `services/email_service.py`)

- **Auth**: `gmail/auth.py` uses a single OAuth token (`token.json`) shared with Sheets + Chat. Scopes include `gmail.readonly`, `gmail.modify`, `gmail.send`, `spreadsheets`, `chat.spaces.readonly`, `chat.messages.readonly`. If a new scope is added the token gets invalidated on the next boot and a fresh consent is requested.
- **Query**: `GMAIL_QUERY_FILTER` (default `is:unread newer_than:2d`). Adjustable in `.env`.
- **Per message**:
  1. Check `processed_emails` for the message id; skip if seen.
  2. `Extractor.extract_from_email` -> JSON with `is_actionable`, `summary`, `tasks[]`, `ideas`, `opportunities`, `risks`.
  3. If `is_actionable`, route through `TaskService.save_email_tasks` (which dedup-merges or inserts).
  4. Record the message in `processed_emails` so we never reprocess.
- **Quota etiquette**: `QuotaExhaustedError` is NOT caught here — it propagates up to the poller, which halts the current batch and resumes when quota recovers.

### 4.2 Google Chat (`chat/poller.py` + `services/chat_service.py`)

- Walks every space the authenticated user belongs to.
- Per-space high-water-mark stored in `processing_logs` (level=`WATERMARK`, component=`chat.<space_name>`). On each tick we only fetch `createTime > <mark>`.
- Each new message:
  1. Resolve sender display name (rejects `users/<id>` placeholder via `utils.identifiers.clean_identifier`).
  2. Resolve DM partner / group / space label for the Source column.
  3. Build a framing prompt that tells the LLM whether the message is FROM the user (committing) or TO the user (request).
  4. Hand to the meeting-shape extraction path (Chat reuses the meeting prompt since both are "conversation chunks").
  5. Save via `TaskService.save_chat_tasks` -> `source_type="Chat"`.
- **Fails soft**: if the Chat API returns 401/403 (Workspace admin blocks third-party Chat apps for user OAuth), the poller logs once and disables itself for that boot.

### 4.3 In-person meetings (`transcription/` + `services/meeting_service.py`)

- **Capture**: `AudioCapture` thread reads from the default mic via `sounddevice`. Audio is float32, 16 kHz mono. Buffered into queues, flushed to disk as a `.wav` every `AUDIO_CHUNK_MINUTES` (default 2 min) in `data/audio_chunks/session-<TS>_chunk_NNNN.wav`.
- **Mic health probe**: on startup, records 1 second from the configured device and logs a loud warning if peak amplitude < 0.005 (mic muted / privacy-blocked / wrong device). This catches the most common failure where Whisper hallucinates "Thank you" on silence.
- **Silence skip**: each flushed chunk carries `is_silent: bool` and `peak_amplitude: float`. If silent, the meeting_service skips Whisper entirely — no garbage transcripts in the DB.
- **Transcription**: `STT_BACKEND` chooses between local `faster-whisper` (`whisper_engine.py`) or Groq cloud (`groq_whisper.py`). Both produce `Transcription{text, language, segments}`.
- **Extraction**: the transcript goes to `Extractor.extract_from_meeting_chunk` which uses `MEETING_SYSTEM_PROMPT` (with the same Vahdam-only gate + context-rich-description requirements as the email prompt).
- **Persistence**: `TaskService.save_meeting_tasks` -> `source_type="Meeting"`, `source_detail="voice memo by <SELF_DISPLAY_NAME>"`. Each chunk gets a row in `transcript_chunks` storing the raw transcript + insights JSON.

---

## 5. The LLM layer (`ai/`)

### 5.1 Routed-client (`ai/routed_client.py`)

`LLM_PROVIDER` in `.env` may be:

- a single provider: `gemini`, `groq`, or `ollama`
- a comma-separated **priority chain**: `gemini,groq,ollama`

For a chain, `get_llm_client()` returns a `RoutedClient` that:

1. Tries providers left-to-right per call.
2. On `QuotaExhaustedError`: parses the "paused for Xs" duration out of the message, marks the provider as cooling down for that window, falls through to the next provider, **the current call still returns a result**.
3. While a provider is in cooldown, subsequent calls skip it silently — no API hits.
4. The moment cooldown expires, that provider is tried first again automatically.
5. Non-quota errors (network, model-not-pulled, malformed prompt) propagate immediately and DO NOT trigger fallback — those would just hit the same wall on the next provider.

`status()` exposes per-provider availability for health checks.

### 5.2 Per-provider clients

| Client | Source | Quota handling |
|---|---|---|
| `gemini_client.py` | Google AI Studio Gemini Flash (default `gemini-2.0-flash`) | Detects `RESOURCE_EXHAUSTED` with `limit:0` (real quota=0) vs transient 429. `should_retry` predicate skips retry on permanent. |
| `groq_client.py` | Groq cloud, default `llama-3.1-8b-instant` | Detects "daily" in 429 message, arms a 1-hour local pause. `should_retry` skips retry on `QuotaExhaustedError` (prevents wasteful exponential backoff against an exhausted endpoint). |
| `ollama_client.py` | Local Ollama daemon at `OLLAMA_HOST` (default `http://localhost:11434`), `OLLAMA_MODEL` (default `llama3.1:8b`) | No quota concept. Surfaces clear "model not pulled" / "daemon not running" messages. |

All three implement the same `complete(system, user, max_tokens, temperature) -> str` interface so the extractor is provider-agnostic.

### 5.3 Prompts (`ai/prompts.py`)

- **`USER_CONTEXT`** — the Vahdam business context injected into every system prompt: team members (Aman, Manisha, Arihant), growth levers (acquisition / retention / AOV / marketplace / etc.), geographies (US, India, EU, UK), and the channel mix (Shopify, Amazon, Klaviyo, Meta Ads).
- **Name canonicalisation rules** — Whisper-misheard variants ("Anshith", "Maneesha", etc.) are explicitly listed; the prompt instructs the LLM to always emit the canonical form.
- **Placeholder identifier ban** — never emit `users/12345`, `(unknown)`, generic UUIDs as names. Null is the only acceptable "I don't know".
- **Hard Vahdam-only gate** — present in all three prompts (email, meeting, chat). Personal banking, family chat, food delivery, ride receipts MUST produce empty task lists.
- **Quality bar for descriptions** — one sentence, ≤180 chars, context-FIRST (SKU / customer / campaign / channel / amount lead), then the verb, then the deadline reason. Bad: "Send the report." Good: "Hero SKUs (turmeric ginger, ashwagandha) need revised UK Amazon Q3 PPC budget split before Tuesday agency call so Q3 creative briefs unblock."
- **Task shape** is shared via `TASK_SHAPE_HINT` and inlined into every prompt — keeps the JSON contract consistent across sources.

### 5.4 Extractor (`ai/extractor.py`)

- `extract_from_email` — returns `EmailExtraction(summary, tasks, is_actionable)`.
- `extract_from_meeting_chunk` — returns `MeetingChunkExtraction(summary, tasks, ideas, blockers, opportunities, decisions, follow_ups)`.
- `daily_summary` — returns a free-form briefing dict.
- All three wrap the LLM call, parse JSON with fallback (fenced ```json ... ``` block first, then bare `{ ... }`), and coerce each task into a typed `ExtractedTask` via `_coerce_task`.

---

## 6. Task service: dedup + persistence (`services/task_service.py`)

### 6.1 The merge model

Two extracted tasks are considered the **same work item** when they share:

1. The same canonical heading (`normalize_heading(task)`), AND
2. The same SPOC (case-insensitive); OR
3. The same canonical heading and one side has a blank/missing SPOC.

When a new task matches an existing OPEN row by this rule, we do **NOT** insert a new row. Instead:

- Build an "update line" in the tagged format `[YYYY-MM-DD HH:MM · Person · Source] body`.
- Append it to the existing row's `all_updates` column (newest-last).
- Mark the existing row `synced_to_sheets=0` so the next forward-sync push refreshes the All Updates cell in the Sheet.

### 6.2 `normalize_heading`

Pipeline that turns a free-form imperative into a canonical form for equality matching:

```
"Update Banners to Mother's Day Creatives"
   ↓ lowercase + collapse whitespace + strip punctuation
"update banners to mothers day creatives"
   ↓ verb-synonym collapse on first word/bigram
"update banners to mothers day creatives"   (no change — "update" is already canonical)
   ↓ drop stopwords ("the", "a", "for", "to", "of", "with", "and", ...)
"update banners mothers day creatives"
   ↓ depluralise each word ("banners" -> "banner", "creatives" -> "creative")
"update banner mother day creative"        (note "mothers" stripped of trailing 's')
```

Verb-synonym collapse maps families of synonyms to one canonical verb:

- `send / share / deliver / forward / provide / submit` → `send`
- `create / build / make / produce / draft / design / set up` → `create`
- `update / refresh / edit / modify / revise / adjust / tweak` → `update`
- `follow up / chase / ping / remind / circle back / check on` → `follow up`
- `review / audit / verify / validate / confirm` → `review`
- `finalize / finalise / lock / close / wrap up / sign off` → `finalize`
- `fix / resolve / repair / address / troubleshoot` → `fix`
- `schedule / book / arrange / plan` → `schedule`

So "Send the Q3 budget" merges with "Share Q3 budgets" — they both normalise to `send q3 budget`.

### 6.3 SPOC canonicalisation

`utils.identifiers.clean_identifier()` rejects any value that looks like an opaque ID — `users/12345...`, `(unknown)`, `n/a`, ALL-CAPS 24+ char alphanumeric strings, anything without a 2+ char alphabetic run. Returns `None` for those so the SPOC column stays clean.

`transcription.lexicon.canonical_spoc()` then folds full-name variants to short forms:

- `"Anchit Tandon"`, `"Anchit (Self)"` → `"Anchit"`
- `"Aman Gupta"` → `"Aman"`

Configurable in `transcription/lexicon.py::CANONICAL_DISPLAY`.

### 6.4 SPOC contact preference order

For the SPOC Contact column:

1. Per-task `owner_contact` field that the LLM extracted (preferred)
2. Source-level fallback (e.g. the email sender's address for email-sourced tasks)
3. None — blank cell, never a placeholder

---

## 7. The Sheet surface (`sheets/`)

### 7.1 Column layout (HEADERS)

| Col | Header | Source field | Notes |
|---|---|---|---|
| A | Task Heading | `task` | Imperative, ≤70 chars |
| B | Task Description | `task_description` | One sentence, ≤180 chars, context-first |
| C | Status | `status` | `open` / `done` / `dropped` |
| D | Source | derived | "Email from X" / "Google Chat with X" / "In-person meeting (X)" |
| E | Source Link | `source_link` | Direct URL to original Gmail thread or Chat space |
| F | Task Given On | `date_given` formatted | Pretty: "11th May 2026, 11:09 PM" (local TZ) |
| G | Why We're Doing This | `rationale` | One sentence, tied to a Vahdam growth lever |
| H | Growth Pillar | `growth_pillar` | One of 10 fixed enum values |
| I | SPOC | `sender_or_speaker` | Real name only (no `users/...`) |
| J | SPOC Contact | `spoc_contact` | Email or phone — blank if neither |
| K | Priority | `urgency` | Low / Medium / High / Critical |
| L | Task Deadline | `deadline` formatted | Pretty date if ISO; else NL phrase ("by Tuesday") |
| M | All Updates | `all_updates` | `[date · person · source] body` lines, newest-last |
| N | Remarks | always blank | For human use |
| O | `_iso_sort_key` | `date_given` raw ISO | **HIDDEN.** Only purpose: chronological sort. |

The hidden col O exists because Google Sheets cannot chronologically
sort our pretty-formatted text strings ("9th May" sorts before "1st
May" alphabetically). Forward sync writes a raw ISO 8601 timestamp
there, and after every push `SheetsClient.sort_tab_desc_by_sort_key`
re-sorts the entire tab by col O descending.

### 7.2 Visual polish (applied by `scripts/rebuild_live_sheet.py`)

- **Headers**: bold, light-grey fill, row 1 frozen.
- **Column A frozen**: heading stays visible during horizontal scroll.
- **Column O hidden**: `hiddenByUser=true`.
- **Cell number format on F and L**: plain `TEXT` so Sheets can't auto-parse our pretty dates into number serials.
- **Conditional formatting** by row:
  - Status `done` → light green row + strike-through
  - Status `dropped` → light grey row + strike-through
- **Conditional formatting** on col K (Priority):
  - Critical → red cell + bold
  - High → orange cell + bold
  - Medium → yellow cell + bold
  - Low → grey cell + bold
- **Wrap text** on Description, Why We're Doing This, All Updates.
- **Pixel widths** preset per column so the sheet opens with sensible proportions.

### 7.3 Forward sync (`sheets/sync.py`)

`SheetsSyncWorker` runs every 30s:

1. Reads up to `BATCH_SIZE` (50) unsynced tasks ordered DESC by `date_given`.
2. Groups by source tab.
3. For each tab: appends rows via `values().append()`; the response gives us the starting row number which we persist as `sheet_row_all` / `sheet_row_source` for that batch.
4. **Dual write** to source tab AND Master.
5. After all batches flush: calls `sort_tab_desc_by_sort_key(tab)` on each touched tab (Sheets API `sortRange` request, sorted by col O DESC).
6. Mirrors the same appends to `tasks.xlsx` via `ExcelMirror`.

### 7.4 Reverse sync (`sheets/reverse_sync.py`)

`ReverseSyncWorker` runs every 60s. Since forward sync re-sorts on every push, row indices are unstable, so we use content-based lookup:

1. Read columns A (heading), C (status), I (SPOC) for every row of Master Task List in one API call.
2. Build an in-memory index of open DB tasks keyed on `(normalized_heading, lowercase SPOC)`.
3. For each sheet row, compute the same key and look up the DB task.
4. Normalise the sheet status via `_STATUS_ALIASES` (`todo` / `pending` / `wip` / `blocked` → `open`; `complete` / `closed` / `shipped` → `done`; `cancelled` / `wontfix` → `dropped`).
5. If sheet status differs from DB, call `db.update_task_status`.

This lets you freely add columns to the sheet, sort the sheet manually, or even add hand-written rows — reverse sync just won't match them and skips them silently.

### 7.5 Excel mirror (`sheets/excel_mirror.py`)

Append-only writer for `tasks.xlsx` at the repo root. Mirrors the same tabs + columns as the live Sheet. Three concerns:

- **Windows file lock**: if you have `tasks.xlsx` open in Excel, the writer logs a warning and skips. Next successful flush catches up.
- **Schema healing**: on startup, detects legacy schemas via `LEGACY_SCHEMAS` and inserts missing columns to migrate without data loss. Renames legacy tabs via `LEGACY_TAB_RENAMES`.
- **Hidden column O**: openpyxl `ws.column_dimensions['O'].hidden = True` + width 0.01 so the sort key is invisible.

---

## 8. Database (`database/`)

SQLite, single file at `data/personal_ai_os.db`. WAL mode enabled in `schema.sql`.

### 8.1 Tables

- **`extracted_tasks`** — the canonical task store. One row per logical task; updates from re-extractions get merged into `all_updates`.
- **`transcript_chunks`** — every audio chunk's transcript + LLM insights JSON.
- **`processed_emails`** — message ID dedup index. Status is `processed` / `skipped` / `failed`.
- **`processing_logs`** — append-only event log; also the home of chat per-space high-water-marks (level=`WATERMARK`).
- **`daily_summaries`** — one row per day, unique on `date`. Stores the LLM-generated briefing + raw insights JSON.
- **`meeting_sessions`** — one row per audio session, linked to chunks.

### 8.2 Migrations

`_migrate()` in `db.py` runs on every boot. It inspects `PRAGMA table_info(extracted_tasks)` and ALTERs to add any missing columns. This is how new columns (`task_description`, `rationale`, `growth_pillar`, `source_detail`, `source_link`, `date_given`, `spoc_contact`, `all_updates`, `normalized_heading`, `sheet_row_source`, `sheet_row_all`) have been added incrementally without data loss.

### 8.3 The dedupe hash

`extracted_tasks.dedupe_hash` is a SHA-256 of `(source_type, source_ref_id, task_heading)`. It's a UNIQUE constraint so the same email re-processed (e.g. on a restart before `processed_emails` was checked) can't insert duplicates of the same task.

This is the FIRST line of dedup. The `normalize_heading` + SPOC merge in TaskService is the SECOND line — it catches cross-source duplicates (the same task mentioned in email AND a meeting).

---

## 9. Configuration (`.env`)

| Key | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | Single name or comma-chain (e.g. `gemini,groq,ollama`) |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | Required if Gemini is in the chain |
| `GEMINI_MODEL` | `gemini-2.0-flash` | |
| `GROQ_API_KEY` | — | Required if Groq is in the chain |
| `GROQ_MODEL` | `llama-3.1-8b-instant` | |
| `OLLAMA_MODEL` | `llama3.1:8b` | |
| `OLLAMA_HOST` | `http://localhost:11434` | |
| `GOOGLE_SHEET_ID` | — | Required. Spreadsheet ID from the URL. |
| `POLLING_INTERVAL` | `30` | Gmail poll seconds |
| `GMAIL_QUERY_FILTER` | `is:unread newer_than:2d` | |
| `ENABLE_CHAT_POLLER` | `True` | |
| `CHAT_POLLING_INTERVAL` | `60` | |
| `ENABLE_MEETING_CAPTURE` | `True` | |
| `AUDIO_CHUNK_MINUTES` | `2` | |
| `AUDIO_SAMPLE_RATE` | `16000` | |
| `AUDIO_INPUT_DEVICE` | `None` (system default) | Set to a device index or name substring if the default mic is wrong |
| `STT_BACKEND` | `local` | `local` (faster-whisper) or `groq` |
| `WHISPER_MODEL` | `base` | Used when STT_BACKEND=local |
| `WHISPER_DEVICE` | `cpu` | `cpu` or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `int8` | int8 for CPU, float16 for GPU |
| `GROQ_WHISPER_MODEL` | `whisper-large-v3-turbo` | Used when STT_BACKEND=groq |
| `DAILY_SUMMARY_HOUR` | `21` | 0-23, local time |
| `NOTIFICATION_RECIPIENT` | falls back to `EXPECTED_GOOGLE_ACCOUNT` | Where the daily digest is mailed |
| `ENABLE_NOTIFICATIONS` | `True` | |
| `SELF_DISPLAY_NAME` | `Anchit (Self)` | Used to label meeting tasks |
| `EXPECTED_GOOGLE_ACCOUNT` | — | OAuth bind. If set, refuses tokens not owned by this email. |
| `INITIAL_SCAN_DAYS` | `0` | One-time historical Gmail sweep on first boot |

---

## 10. Operational runbook

### Daily

Just run `python main.py`. Workers handle everything else.

### When something looks wrong

1. **No tasks in last hour?** Check the mic-health probe in the logs. If it says `peak=0.000000`, the mic is muted (Windows privacy, F4 mute key, wrong device).
2. **Lots of "Thank you" transcripts?** Same root cause — silent input. Verify the mic-health line on startup.
3. **Sheet not updating?** Check `synced_to_sheets` counts in the DB:
   ```sql
   SELECT synced_to_sheets, COUNT(*) FROM extracted_tasks GROUP BY synced_to_sheets;
   ```
   Or just look for `Sheets sync: pushed N task row(s)` lines in the logs.
4. **Same task appearing twice?** Run `python -c "from services.task_service import normalize_heading; print(normalize_heading('the task'))"` for each heading. If they normalise the same and have the same SPOC, that's a bug — paste both rows and I'll investigate. Otherwise the dedup correctly considers them different.
5. **Quota errors halting work?** Confirm `LLM_PROVIDER` is the chain `gemini,groq,ollama` and Ollama is running locally as the safety net.

### One-shot scripts

| Script | When to run |
|---|---|
| `python -m scripts.migrate_v2` | After a DB column / sheet column change, to reshape existing data |
| `python -m scripts.rebuild_live_sheet` | When the live Sheet looks broken (extra tabs, stale data, drift). Wipes + recreates the 3 tabs and re-applies conditional formatting. Pair with: `python -c "from database import get_db; get_db().execute('UPDATE extracted_tasks SET synced_to_sheets=0, sheet_row_all=NULL, sheet_row_source=NULL')"` then restart main.py to push all rows fresh. |
| `python -m scripts.backfill_columns` | Recover missing `date_given`, `source_link`, `spoc_contact` on historical rows |

### Restarting main.py

Necessary whenever any of these change:

- `.env` (e.g. provider chain, sheet ID)
- Module-level constants like `TAB_ORDER`, `HEADERS` — the running interpreter has the old values cached
- A code path the worker uses (silence detection, sort logic, dedup rules)

Workflow:
1. Ctrl+C the running process; wait for "Shutdown complete." line.
2. `python main.py`.
3. First 60 seconds: confirm "Mic health probe OK", "SheetsSyncWorker started", "RoutedClient initialised with priority chain".

---

## 11. Known limits

- **Mic capture is single-channel** — no speaker diarisation. Every meeting task is attributed to `SELF_DISPLAY_NAME` unless the LLM finds another speaker in the transcript text.
- **Whisper on Hindi/English code-switching** struggles. Speak closer to the mic and use a lapel mic for important conversations.
- **No WhatsApp** ingestion (removed). Only Email + Chat + voice memos.
- **No phone app**. Audio capture works only on the laptop.
- **Reverse sync is heading+SPOC keyed**, so if you rename a task's heading in the Sheet, reverse sync stops being able to match it back to its DB row. Status edits keep working as long as A + I aren't manually edited.
- **Forward sync row mappings (`sheet_row_all`, `sheet_row_source`) drift** after every sort. Reverse sync no longer relies on them; they're kept for future use but not load-bearing.
- **Audio chunks live forever** in `data/audio_chunks/`. Manual cleanup recommended every few weeks.

---

## 12. Where each major decision lives

| Decision | File |
|---|---|
| Vahdam-only filter, prompt language | `ai/prompts.py` |
| Which LLM gets called first | `.env` `LLM_PROVIDER`, `ai/__init__.py` |
| Quota fallback behaviour | `ai/routed_client.py` |
| What counts as "the same task" for dedup | `services/task_service.py::normalize_heading` |
| Real-name predicate (no `users/...`) | `utils/identifiers.py` |
| Full-name → short-name folding | `transcription/lexicon.py::CANONICAL_DISPLAY` |
| Source label wording in the sheet | `sheets/sync.py::_format_source_label` |
| Update-line tagging format | `services/task_service.py::_format_update_line` |
| Task Given On display format | `sheets/sync.py::_format_iso_timestamp` |
| Sheet tab names + column order | `sheets/client.py::HEADERS`, `TAB_ORDER` |
| Conditional formatting rules | `scripts/rebuild_live_sheet.py` |
| Silence detection threshold | `transcription/audio_capture.py::_SILENCE_PEAK_THRESHOLD` |
| Daily digest recipient + time | `.env` `NOTIFICATION_RECIPIENT`, `DAILY_SUMMARY_HOUR` |
