"""
Shared Google OAuth helper for Gmail + Sheets.

The same `credentials.json` (Desktop OAuth client) and `token.json` are
used for both APIs — the scope list lives in `config.settings`.
"""
from __future__ import annotations

import threading
from typing import Optional

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
_cached_creds: Optional[Credentials] = None


def get_credentials(force_refresh: bool = False) -> Credentials:
    """
    Returns valid OAuth credentials. Triggers an interactive browser flow
    on first run; thereafter the refresh token is reused silently.
    """
    global _cached_creds

    with _lock:
        if _cached_creds is not None and not force_refresh and _cached_creds.valid:
            return _cached_creds

        creds: Optional[Credentials] = None
        token_path = settings.google_token_path
        creds_path = settings.google_credentials_path

        if token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(
                    str(token_path), list(settings.oauth_scopes)
                )
            except Exception as exc:  # corrupt token file
                logger.warning("Could not load %s: %s — restarting OAuth.", token_path, exc)
                creds = None

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("Refreshed Google OAuth token.")
            except Exception as exc:
                logger.warning("Token refresh failed (%s) — restarting OAuth.", exc)
                creds = None

        if not creds or not creds.valid:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Google OAuth client file not found at {creds_path}. "
                    "Download the Desktop OAuth credentials JSON from Google Cloud Console "
                    "and save it there (or update GOOGLE_CREDENTIALS_PATH in .env)."
                )
            logger.info(
                "Launching browser for Google OAuth consent... "
                "Sign in with the account that owns the destination Sheet."
            )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(creds_path), list(settings.oauth_scopes)
            )
            creds = flow.run_local_server(
                port=0,
                open_browser=True,
                authorization_prompt_message=(
                    "Open this URL to authorise:\n  {url}\n"
                    "(if the browser doesn't open automatically)"
                ),
                success_message=(
                    "Authentication complete. You can close this tab — "
                    "main.py will continue."
                ),
            )
            token_path.write_text(creds.to_json(), encoding="utf-8")
            logger.info("Saved OAuth token to %s", token_path)

        # Log WHICH account got authenticated so the user has visual confirmation.
        try:
            session = AuthorizedSession(creds)
            resp = session.get(
                "https://www.googleapis.com/oauth2/v2/userinfo", timeout=10
            )
            if resp.status_code == 200:
                info = resp.json()
                logger.info(
                    "Authenticated as %s (%s)",
                    info.get("email", "<unknown>"),
                    info.get("name", "<no name>"),
                )
        except Exception as exc:
            logger.debug("Could not fetch userinfo: %s", exc)

        _cached_creds = creds
        return creds
