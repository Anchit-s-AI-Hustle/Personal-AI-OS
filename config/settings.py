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
    # LLM provider selector
    llm_provider: str   # "gemini" | "groq" | "ollama"

    # Gemini-specific (always populated; ignored if provider != gemini)
    llm_api_key: str    # alias for gemini_api_key, kept for backward compat
    llm_model: str

    # Groq-specific
    groq_api_key: str
    groq_model: str

    # Ollama-specific (local model server, no API key)
    ollama_model: str
    ollama_host: str

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

    # Speech-to-text
    stt_backend: str   # "local" | "groq"
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    whisper_language: Optional[str]
    groq_whisper_model: str

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

    # "Self" identity for chat — used to label outgoing messages/tasks.
    self_chat_user_id: Optional[str]
    self_display_name: str

    # One-time historical Gmail scan
    initial_scan_days: int       # 0 = disabled
    initial_scan_max_messages: int

    # Google Chat poller
    enable_chat_poller: bool
    chat_polling_interval: int

    # Outbound notifications (Gmail digest)
    notification_recipient: Optional[str]
    enable_notifications: bool

    # OAuth scopes — Chat scopes added so we can also read Spaces/DMs.
    # gmail.send added for the outbound digest. Adding new scopes here
    # triggers a re-consent on next run if the current token doesn't
    # have them.
    oauth_scopes: tuple = field(
        default=(
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
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
    raw_provider = (_env("LLM_PROVIDER", "gemini") or "gemini").strip().lower()
    # Accept a single provider OR a comma-separated priority chain.
    # The chain is iterated left-to-right by RoutedClient on quota
    # exhaustion. Validation: every name must be one of the three known
    # providers, and the API key for any cloud provider in the chain
    # must be configured.
    provider_chain = [p.strip() for p in raw_provider.split(",") if p.strip()]
    if not provider_chain:
        raise RuntimeError("LLM_PROVIDER is empty.")
    valid_providers = {"gemini", "groq", "ollama"}
    unknown = [p for p in provider_chain if p not in valid_providers]
    if unknown:
        raise RuntimeError(
            f"LLM_PROVIDER contains unknown provider(s) {unknown!r}. "
            f"Valid: {sorted(valid_providers)}. "
            f"Use a single name (e.g. 'gemini') or a chain (e.g. 'gemini,groq,ollama')."
        )
    # `provider` retained as the canonical normalised string for the
    # Settings dataclass; consumers split it themselves.
    provider = ",".join(provider_chain)

    gemini_api_key = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY") or ""
    groq_api_key = _env("GROQ_API_KEY") or ""

    # Validate the key for any cloud provider that's anywhere in the
    # chain (it might be the fallback, but we still need credentials
    # ready for when it's needed). Ollama is local and needs no key.
    if "gemini" in provider_chain and not gemini_api_key:
        raise RuntimeError(
            "LLM_PROVIDER includes 'gemini' but GEMINI_API_KEY is missing in .env. "
            "Get a key at https://aistudio.google.com/apikey "
            "(use a personal Google account — Workspace accounts get limit:0)."
        )
    if "groq" in provider_chain and not groq_api_key:
        raise RuntimeError(
            "LLM_PROVIDER includes 'groq' but GROQ_API_KEY is missing in .env. "
            "Get a key at https://console.groq.com/keys (free tier, no Workspace restriction)."
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
        llm_provider=provider,
        llm_api_key=gemini_api_key,
        llm_model=_env("GEMINI_MODEL", "gemini-2.0-flash") or "gemini-2.0-flash",
        groq_api_key=groq_api_key,
        groq_model=_env("GROQ_MODEL", "llama-3.1-8b-instant") or "llama-3.1-8b-instant",
        ollama_model=_env("OLLAMA_MODEL", "llama3.1:8b") or "llama3.1:8b",
        ollama_host=_env("OLLAMA_HOST", "http://localhost:11434") or "http://localhost:11434",
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
        stt_backend=(_env("STT_BACKEND", "local") or "local").strip().lower(),
        whisper_model=_env("WHISPER_MODEL", "base") or "base",
        whisper_device=_env("WHISPER_DEVICE", "cpu") or "cpu",
        whisper_compute_type=_env("WHISPER_COMPUTE_TYPE", "int8") or "int8",
        whisper_language=_env("WHISPER_LANGUAGE"),
        groq_whisper_model=_env("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo") or "whisper-large-v3-turbo",
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
        self_chat_user_id=_env("SELF_CHAT_USER_ID"),
        self_display_name=_env("SELF_DISPLAY_NAME", "Anchit (Self)") or "Anchit (Self)",
        initial_scan_days=_env_int("INITIAL_SCAN_DAYS", 0),
        initial_scan_max_messages=_env_int("INITIAL_SCAN_MAX_MESSAGES", 1000),
        enable_chat_poller=_env_bool("ENABLE_CHAT_POLLER", True),
        chat_polling_interval=_env_int("CHAT_POLLING_INTERVAL", 60),
        notification_recipient=_env("NOTIFICATION_RECIPIENT")
        or _env("EXPECTED_GOOGLE_ACCOUNT"),
        enable_notifications=_env_bool("ENABLE_NOTIFICATIONS", True),
    )


settings = _load()
settings.ensure_directories()
