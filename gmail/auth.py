"""
Shared Google OAuth helper for Gmail + Sheets.

The same `credentials.json` (Desktop OAuth client) and `token.json` are
used for both APIs. The scope list lives in `config.settings`.

If `EXPECTED_GOOGLE_ACCOUNT` is set, we:
  - Try to launch the OAuth consent in the Chrome profile signed into
    that account (auto-detected by reading Chrome's Local State).
  - After authentication, verify the token's owner matches; if it
    doesn't, delete the bad token and refuse to proceed so the user
    doesn't accidentally bind to a personal Gmail.
"""
from __future__ import annotations

import json
import threading
from typing import Optional

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config import settings
from utils.logger import get_logger

from . import chrome_profile

logger = get_logger(__name__)

_lock = threading.Lock()
_cached_creds: Optional[Credentials] = None


def _granted_scopes(token_path) -> set[str]:
    """Read the scopes Google actually granted from the saved token JSON."""
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return set(data.get("scopes") or [])


class WrongGoogleAccountError(RuntimeError):
    """Raised when OAuth completes but with a non-expected email."""


def _fetch_userinfo(creds: Credentials) -> Optional[dict]:
    try:
        session = AuthorizedSession(creds)
        resp = session.get(
            "https://www.googleapis.com/oauth2/v2/userinfo", timeout=10
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        logger.debug("Could not fetch userinfo: %s", exc)
    return None


def _interactive_oauth_flow(creds_path) -> Credentials:
    """Run the InstalledAppFlow, optionally opening Chrome with a specific profile."""
    flow = InstalledAppFlow.from_client_secrets_file(
        str(creds_path), list(settings.oauth_scopes)
    )

    expected = (settings.expected_google_account or "").strip()
    profile_override = (settings.oauth_chrome_profile or "").strip()

    # Decide if we're going to launch Chrome ourselves with a specific profile.
    target_profile: Optional[str] = profile_override or None
    if expected and not target_profile:
        target_profile = chrome_profile.find_profile_for_email(expected)

    if target_profile:
        logger.info(
            "Will open OAuth in Chrome profile %r (account hint: %s).",
            target_profile,
            expected or "<none>",
        )

        # We pass open_browser=False and use a one-shot redirect_uri so the
        # local server still catches the callback. Then we open Chrome with
        # the right profile pointed at the auth URL.
        def _opener(url: str) -> bool:
            return chrome_profile.open_in_chrome_profile(url, target_profile)

        # Hook: oauthlib doesn't accept a custom opener directly via
        # run_local_server, so we patch webbrowser.open for the duration.
        import webbrowser as _wb

        original_open = _wb.open

        def _patched_open(url, *args, **kwargs):
            ok = _opener(url)
            if ok:
                return True
            # Fall back to default browser if Chrome launch failed.
            return original_open(url, *args, **kwargs)

        _wb.open = _patched_open  # type: ignore[assignment]
        try:
            creds = flow.run_local_server(
                port=0,
                open_browser=True,
                authorization_prompt_message=(
                    "If a Chrome window didn't open, paste this URL into the "
                    f"Chrome window where you're signed in as {expected or 'your work account'}:\n"
                    "  {url}"
                ),
                success_message=(
                    "Authentication complete. You can close this tab — "
                    "main.py will continue."
                ),
            )
        finally:
            _wb.open = original_open  # type: ignore[assignment]
    else:
        # No profile hint — fall back to system default browser. Print the
        # URL prominently so the user can copy it into the right window.
        if expected:
            logger.warning(
                "Could not auto-detect a Chrome profile for %s. The system default "
                "browser will open; if it logs in with the wrong account, paste "
                "the URL into the Chrome window where %s is signed in.",
                expected,
                expected,
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

    return creds


def _verify_account(creds: Credentials, token_path) -> Credentials:
    """Refuse a token whose email doesn't match EXPECTED_GOOGLE_ACCOUNT."""
    expected = (settings.expected_google_account or "").strip().lower()
    info = _fetch_userinfo(creds)
    if info:
        actual_email = (info.get("email") or "").strip().lower()
        actual_name = info.get("name") or "<no name>"
        logger.info("Authenticated as %s (%s)", actual_email or "<unknown>", actual_name)
        if expected and actual_email and actual_email != expected:
            try:
                token_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise WrongGoogleAccountError(
                f"OAuth completed with {actual_email!r} but EXPECTED_GOOGLE_ACCOUNT "
                f"is {expected!r}. Token discarded. Re-run main.py and sign in "
                f"with {expected!r}."
            )
    return creds


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

            # If we've added new scopes since this token was issued, force a
            # fresh consent so all APIs work. Without this, calls to the new
            # scopes would silently 403.
            if creds is not None:
                granted = _granted_scopes(token_path)
                missing = set(settings.oauth_scopes) - granted
                if missing:
                    logger.info(
                        "Token missing scopes %s; restarting OAuth to grant them.",
                        sorted(missing),
                    )
                    try:
                        token_path.unlink(missing_ok=True)
                    except Exception:
                        pass
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
                    "Download the Desktop OAuth credentials JSON from Google "
                    "Cloud Console and save it there (or update "
                    "GOOGLE_CREDENTIALS_PATH in .env)."
                )
            logger.info("Launching Google OAuth consent flow...")
            creds = _interactive_oauth_flow(creds_path)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            logger.info("Saved OAuth token to %s", token_path)

        creds = _verify_account(creds, token_path)
        _cached_creds = creds
        return creds
