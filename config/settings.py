"""
Centralised configuration loaded from environment variables (.env).

Importing `settings` validates required keys at startup so the rest of the
codebase can rely on the values being present.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env once, the first time this module is imported.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(key, default)
    if val is None:
        return None
    val = val.strip()
    return val if val != "" else None


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {key} must be an int, got {raw!r}") from exc


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _resolve_path(raw: Optional[str], default_relative: str) -> Path:
    if raw is None:
        raw = default_relative
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


@dataclass(frozen=True)
class Settings:
    # LLM (Google Gemini)
    llm_api_key: str
    llm_model: str

    # Google Sheets
    google_sheet_id: str
    google_sheet_tab: str

    # Gmail
    polling_interval: int
    gmail_query_filter: str

    # Audio
    audio_chunk_minutes: int
    audio_sample_rate: int
    audio_input_device: Optional[str]
    enable_meeting_capture: bool

    # Whisper
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    whisper_language: Optional[str]

    # Paths
    project_root: Path
    database_path: Path
    audio_chunks_dir: Path
    transcripts_dir: Path
    logs_dir: Path
    google_credentials_path: Path
    google_token_path: Path

    # Logging
    log_level: str

    # Daily summary
    daily_summary_hour: int

    # OAuth account binding
    expected_google_account: Optional[str]
    oauth_chrome_profile: Optional[str]

    # One-time historical Gmail scan
    initial_scan_days: int       # 0 = disabled
    initial_scan_max_messages: int

    # Google Chat poller
    enable_chat_poller: bool
    chat_polling_interval: int

    # OAuth scopes — Chat scopes added so we can also read Spaces/DMs.
    # Adding new scopes here triggers a re-consent on next run if the
    # current token doesn't have them.
    oauth_scopes: tuple = field(
        default=(
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/chat.spaces.readonly",
            "https://www.googleapis.com/auth/chat.messages.readonly",
        )
    )

    def ensure_directories(self) -> None:
        for p in (
            self.audio_chunks_dir,
            self.transcripts_dir,
            self.logs_dir,
            self.database_path.parent,
        ):
            p.mkdir(parents=True, exist_ok=True)


def _load() -> Settings:
    api_key = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Copy .env.example to .env and fill it in. "
            "Get a key at https://aistudio.google.com/apikey"
        )

    sheet_id = _env("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID is missing in .env")
    # Tolerate users pasting the chunk after `/d/` from the sheet URL.
    if sheet_id.startswith("d/"):
        sheet_id = sheet_id[2:]

    # Some users save the file as `credentials.json.json` by accident — handle that.
    creds_path_raw = _env("GOOGLE_CREDENTIALS_PATH", "./credentials.json")
    creds_path = _resolve_path(creds_path_raw, "./credentials.json")
    if not creds_path.exists():
        legacy = creds_path.with_suffix(creds_path.suffix + ".json")
        if legacy.exists():
            creds_path = legacy

    return Settings(
        llm_api_key=api_key,
        llm_model=_env("GEMINI_MODEL", "gemini-2.0-flash") or "gemini-2.0-flash",
        google_sheet_id=sheet_id,
        google_sheet_tab=_env("GOOGLE_SHEET_TAB", "Tasks") or "Tasks",
        polling_interval=_env_int("POLLING_INTERVAL", 30),
        gmail_query_filter=_env(
            "GMAIL_QUERY_FILTER", "is:unread newer_than:2d"
        ) or "is:unread newer_than:2d",
        audio_chunk_minutes=_env_int("AUDIO_CHUNK_MINUTES", 2),
        audio_sample_rate=_env_int("AUDIO_SAMPLE_RATE", 16000),
        audio_input_device=_env("AUDIO_INPUT_DEVICE"),
        enable_meeting_capture=_env_bool("ENABLE_MEETING_CAPTURE", True),
        whisper_model=_env("WHISPER_MODEL", "base") or "base",
        whisper_device=_env("WHISPER_DEVICE", "cpu") or "cpu",
        whisper_compute_type=_env("WHISPER_COMPUTE_TYPE", "int8") or "int8",
        whisper_language=_env("WHISPER_LANGUAGE"),
        project_root=PROJECT_ROOT,
        database_path=_resolve_path(_env("DATABASE_PATH"), "./data/personal_ai_os.db"),
        audio_chunks_dir=_resolve_path(_env("AUDIO_CHUNKS_DIR"), "./data/audio_chunks"),
        transcripts_dir=_resolve_path(_env("TRANSCRIPTS_DIR"), "./data/transcripts"),
        logs_dir=_resolve_path(_env("LOGS_DIR"), "./logs"),
        google_credentials_path=creds_path,
        google_token_path=_resolve_path(_env("GOOGLE_TOKEN_PATH"), "./token.json"),
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        daily_summary_hour=_env_int("DAILY_SUMMARY_HOUR", 21),
        expected_google_account=_env("EXPECTED_GOOGLE_ACCOUNT"),
        oauth_chrome_profile=_env("OAUTH_CHROME_PROFILE"),
        initial_scan_days=_env_int("INITIAL_SCAN_DAYS", 0),
        initial_scan_max_messages=_env_int("INITIAL_SCAN_MAX_MESSAGES", 1000),
        enable_chat_poller=_env_bool("ENABLE_CHAT_POLLER", True),
        chat_polling_interval=_env_int("CHAT_POLLING_INTERVAL", 60),
    )


settings = _load()
settings.ensure_directories()
