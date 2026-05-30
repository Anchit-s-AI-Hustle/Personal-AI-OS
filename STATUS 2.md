# Personal AI OS — Status & Open Problems

A single document covering: what we're building, what works, what's broken,
and what I can't solve from inside this environment.

---

## 1. The vision

A local AI productivity layer that runs on Anchit's laptop and feeds every
actionable item into ONE place (Google Sheet + Excel mirror), all biased
toward Vahdam D2C growth.

### Capture sources (intended)

| Source | Status |
|---|---|
| Gmail inbox (work account) | ✅ working |
| Google Chat — DMs, group chats, spaces | ✅ working |
| Microphone — laptop voice memos / meetings | ⚠️ working but fragile |
| Phone microphone (24/7 capture from phone) | ❌ NOT built |
| WhatsApp chats and groups | ❌ NOT built |

### Output surfaces

| Surface | Status |
|---|---|
| Google Sheet with 3 tabs (All / Discussions / Mails) | ✅ working |
| Master Task List + per-source detail tabs | ✅ working |
| Excel mirror at repo root (`tasks.xlsx`) | ✅ working (was corrupted, now rebuilt) |
| Daily strategic briefing at 21:00 local | ✅ working (logs to SQLite, no email yet) |
| WhatsApp digest with interactive checklist | ❌ NOT built |
| Telegram digest with inline buttons | ❌ NOT built (proposed as easier alternative) |
| Email digest of pending tasks | ❌ NOT built |

### Intelligence stack

| Layer | Current backend |
|---|---|
| LLM extraction | Groq `llama-3.1-8b-instant` (started Anthropic → Gemini → Groq; routed-client fallback to Gemini / Ollama) |
| Speech-to-text | Groq Whisper Large v3 Turbo (`STT_BACKEND=groq`); local Faster-Whisper available but segfaults on Python 3.14 |
| Storage | SQLite (`data/personal_ai_os.db`) with FTS5 transcript search |
| Auto-start | Windows Task Scheduler at user logon |

---

## 2. What's actually working today

- **Email polling**: every 30s, strict filter rejects newsletters / noreply / promos / mailing-list digests; LLM only sees mail addressed to Anchit with concrete asks. Vahdam-only filter is enforced in the prompt.
- **Chat polling**: every 60s. Knows "self" is `users/110152155675645013355`, labels outgoing messages as "Anchit (Self)", computes "DM with X" / "Space: ..." for the Source column. Tracks per-space high-water-marks so re-polls don't reprocess history.
- **Mic capture (when enabled)**: continuous 2-min chunks → Groq Whisper → Llama 3.1 8B extraction. Hindi/English mixed. Names canonicalised via lexicon ("Anshith" → "Anchit", "Maneesha" → "Manisha", etc.).
- **Tab structure (current)**: 13 columns
  - Task Heading, Task Description, Status, Source, Source Link, Date Given,
    Why We're Doing This, Growth Pillar, SPOC, SPOC Contact, Priority,
    Go Live (deadline), Remarks
- **Source Link contents (current)**:
  - Email → `https://mail.google.com/mail/u/0/#inbox/<thread_id>`
  - Chat → `https://mail.google.com/chat/u/0/#chat/dm/<space>` or `.../space/<space>`
  - Meeting → multi-line: `Audio: file:///...wav` + `Transcript: file:///...txt`
- **De-duplication**: SHA256 hash of `source_type|source_ref_id|lower(task_heading)` prevents the same task entering twice.
- **Same-task-merge**: arriving updates on an open task append a chronological note to the existing row's `all_updates` column instead of creating a duplicate.
- **Dual-write**: Google Sheet + Excel mirror updated atomically per task. Excel failure does NOT roll back Sheet (Sheet is source of truth).
- **Auto-restart**: Task Scheduler restarts up to 999 times after failure.

---

## 3. Open problems — what's broken or unfinished

### CRITICAL (blocking real use)

#### 3.1 Native crash — `0xC0000005` access violation
Process dies sporadically with a C-level fault. Smoking gun appears to be
`openpyxl + lxml` reading a corrupt `tasks.xlsx`, but it might also be
`numpy`, `sounddevice`, or `ctranslate2` faulting on the current Python
build. Restarts via Task Scheduler but if the cause is persistent the
process crash-loops.

- **What I tried**: deleted corrupt `tasks.xlsx`, the immediate trigger is gone but the underlying native fault hasn't been root-caused.
- **What I can't do from here**: attach a debugger to get a stack trace from the native side. Need `Procdump` / WinDbg / Python's faulthandler dumping to a known file.
- **What might fix it**: recreate the venv with Python 3.12 (a known-good ctranslate2 wheel exists for 3.12) — but `uv` keeps switching the Python interpreter back to whatever it prefers (saw 3.13 and 3.14 swap during the session).

#### 3.2 Sync silently stalled for 5 days
Between **May 14** (first "Sheets sync cycle crashed" error) and **May 19** (5 process restarts, 0 successful sync flushes), the sheet stopped receiving new tasks even though chat + email polling kept writing to the DB. 250 tasks accumulated unsynced. Root cause was the native crash above + corrupt `tasks.xlsx`.
- **Now fixed for this incident** — backlog drained, xlsx clean.
- **Still vulnerable**: nothing alerts you when the unsynced count grows. If the same crash happens tomorrow, you'd notice only when checking the sheet.
- **Proposed fix (not yet built)**: daily-summary email/Telegram message with "Unsynced backlog: N rows" so it's caught within hours.

#### 3.3 Git push blocked
The cached Git credential is for the wrong GitHub account (`anchittandon-create` rather than `anchittandon-vahdam`). Push fails with "Repository not found" (private repo + wrong creds → 404).
- **What I can't do**: open the GUI credential prompt — needs a real interactive terminal.
- **Local commits accumulated**: ~7 commits sitting unpushed (rate-limit guards, lexicon, Groq client, Excel mirror, etc.).
- **What you need to do** (in a fresh PowerShell window on your machine):
  ```powershell
  cd "C:\Users\Archit Tandon\Desktop\Personal-AI-OS"
  git credential-manager erase https://github.com
  # cmdkey /list | findstr github  (also clear stored creds if needed)
  git push origin main
  ```
  Sign in as `anchittandon-vahdam` when the GUI prompt appears.

---

### HIGH (planned features, real work to build)

#### 3.4 WhatsApp integration with interactive checklist
**Asked for**, **not built**. Three sub-issues:
1. **WhatsApp Business API access** — required for outbound messaging from a bot. Costs (a) Meta business verification, (b) BSP fees (~$0.005/conversation, or Twilio $$$).
2. **Persistent webhook server** — bot needs to be reachable 24/7 to receive button-presses. Your laptop being on/off can't be the runtime. Requires a cheap VPS (~$5/month) or Cloud Run.
3. **Interactive UI** — WhatsApp doesn't support a custom calendar view. Best-case is reply-buttons ("Mark Done", "Add Blocker") and rich lists.

- **What I can't do**: provision the Business API account, set up a domain + webhook, host the server.
- **Cheaper alternative I'd recommend**: **Telegram Bot** — free, no business verification, supports inline keyboards / quick polls, takes ~30 min to build a digest sender. Still needs a small persistent server for receiving button presses, but a free Render/Railway worker covers it.

#### 3.5 Phone microphone listening (24/7 from phone)
**Asked for**, **not built**. This is a separate iOS/Android app project:
- iOS blocks always-on background mic capture without user-active foreground.
- Android allows it via foreground service + persistent notification.
- App-Store review for an "always-listening" app is a non-trivial bar (privacy disclosure, store policy).
- Multi-week effort, not something to fit in chat session work.

#### 3.6 Bi-directional sync (Sheet edits → DB)
`ReverseSyncWorker` exists and pulls task updates from the sheet (saw "Reverse sync: N task update(s) pulled from checklist" in logs). Status changes from the sheet flow back into the DB.
- **Verified working**: status edits do roundtrip.
- **Not verified**: edits to other columns (e.g. Remarks, Description, Task Deadline) — the worker may only handle Status. Needs explicit test.

---

### MEDIUM (cosmetic + nice-to-haves)

#### 3.7 DM partner names show as `users/<digits>` instead of names
For Google Chat DMs, the API doesn't return the partner's displayName via the scopes we have (`chat.spaces.readonly`, `chat.messages.readonly`). To resolve, add `chat.memberships.readonly` and re-consent.
- **Workaround already in place**: when the user is the sender, we cache the OTHER party's identity the first time they reply, but if all messages in a DM are FROM the user, the partner stays as `users/...`.

#### 3.8 Excel file:// links can't be clicked from the Google Sheet
Browser security blocks `file://` link clicks from `https://` pages. The link text shows in Sheets but copy-pasting is the only way to follow it. Excel locally → works fine.
- **If you want clickable audio in browser**: would need a tiny local HTTP server (`python -m http.server 8000` over `data/audio_chunks/`) + rewriting links from `file://` to `http://localhost:8000/...`. ~30 min build.

#### 3.9 139 historical "failed" emails from the Gemini-quota era
On May 8 (Gemini-quota outage day), 139 emails got `status='failed'` in `processed_emails`. They sit there blocked by the dedup gate — never retried.
- **Not auto-retried**: the system treats them as "already processed".
- **Fix script exists** (`retry_failed.py` — I built it earlier in this session, then deleted at your interrupt). Can rebuild in 5 min if you want them processed.

---

### LOW (housekeeping)

| Item | Detail |
|---|---|
| Audio chunks growing | ~9.3 GB on disk now; no retention policy. Suggested: `AUDIO_RETENTION_DAYS=14` auto-prune. |
| SSL "record layer failure" | Frequent network glitches with Google APIs; retries handle them but eat CPU + log noise. Indicator of flaky network, not a bug. |
| `tasks-corrupt-<timestamp>.xlsx` backup | Sitting at repo root as a snapshot of the corrupt file. Delete when comfortable. |
| Stale orphan venv files | `data/.initial_scan_done` sentinel exists; would re-run the historical scan if deleted. Currently disabled (correct behavior). |
| `gh` CLI not installed | Means `/create-pr` slash command can never work from my side. `winget install --id GitHub.cli -e` fixes it. |

---

## 4. What I literally CAN'T figure out / do from this environment

These are blocked on you, not on me writing more code:

1. **Native crash root cause.** Need a debugger attached to the process when it faults. I can only see exit code `0xC0000005`. Suggested approach: enable Python's `faulthandler.dump_traceback_later()` to dump a stack trace on next crash.
2. **Git push.** Credential manager dialog is blocked from non-interactive shells. Has to be done in your own PowerShell window once.
3. **WhatsApp Business API.** Account + verification + payment is your call.
4. **Cloud hosting decision.** Whether you want a $5/mo VPS so the bot survives laptop-off.
5. **Phone listening app.** Not a chat-session-sized task.
6. **Python interpreter pinning.** `uv` keeps swapping between 3.13 and 3.14. To pin: `uv python pin 3.12` then recreate venv. I can do this but you should confirm Python 3.12 is acceptable first (some Whisper backends may behave differently).

---

## 5. Recommended next steps (in priority order)

1. **You**: fix the Git push (3.3) — gets all the unpushed code on origin. 2 minutes.
2. **You / me**: pin Python to 3.12 via `uv python pin 3.12` + recreate venv (3.1). Removes the native-crash risk for good. 10 minutes.
3. **Me**: add the unsynced-backlog alert (3.2) — daily email or Telegram ping if backlog > 20. Prevents another 5-day silent stall.
4. **You**: install gh CLI (`winget install GitHub.cli`). Makes future PR automation possible.
5. **Me + you**: build a Telegram-bot daily digest as the lightweight stand-in for WhatsApp (3.4). Free, ~1-hour build, deployable on Railway free tier.
6. **Later / decision**: WhatsApp Business API + interactive checklist (3.4) — only if Telegram alternative isn't enough.
7. **Later / much bigger**: phone listening app (3.5).

---

## 6. The honest truth

Most of the heavy lifting we set out to do **is built and working** —
- Email → AI → Sheet
- Chat → AI → Sheet
- Voice → AI → Sheet
- Excel mirror in repo
- All with deduplication, name canonicalisation, Vahdam-only filter, source-tagged columns, and reverse-sync from Sheet edits.

The **gaps that remain** are mostly:
- Operational hardening (don't silently stall again)
- Notification surfaces (WhatsApp / Telegram / email digest)
- Infrastructure to support 24/7 phone listening + interactive bots (not local-laptop scoped)

Nothing here is an architectural dead end. They're all "fix the env" or "build the next layer" — both feasible, just not in a single chat session and not without external services / cost decisions from you.

Last updated: 2026-05-20
