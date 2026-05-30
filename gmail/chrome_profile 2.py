"""
Chrome profile detection on Windows.

Used by the OAuth flow so consent opens in the Chrome window already
signed into the user's work Google account, instead of whichever
profile happens to be the system default (usually a personal one).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)


def _user_data_dir() -> Optional[Path]:
    """Return Chrome's User Data dir on Windows, or None if not found."""
    local_app = os.environ.get("LOCALAPPDATA")
    if not local_app:
        return None
    p = Path(local_app) / "Google" / "Chrome" / "User Data"
    return p if p.exists() else None


def find_profile_for_email(email: str) -> Optional[str]:
    """
    Return the Chrome profile directory name (e.g. 'Profile 2', 'Default')
    that is signed into `email`, or None if not found.
    """
    if not email:
        return None
    target = email.strip().lower()

    udd = _user_data_dir()
    if udd is None:
        logger.debug("Chrome User Data directory not found.")
        return None

    # Source 1: Local State / profile.info_cache (most reliable on modern Chrome)
    local_state = udd / "Local State"
    if local_state.exists():
        try:
            data = json.loads(local_state.read_text(encoding="utf-8", errors="replace"))
            cache = (data.get("profile") or {}).get("info_cache") or {}
            for prof_dir, info in cache.items():
                emails = {
                    (info.get("user_name") or "").lower(),
                    (info.get("gaia_name") or "").lower(),
                }
                if target in emails:
                    logger.info(
                        "Matched Chrome profile %r for %s via Local State",
                        prof_dir,
                        email,
                    )
                    return prof_dir
        except Exception as exc:
            logger.debug("Could not parse Local State: %s", exc)

    # Source 2: per-profile Preferences file (older Chrome / sync edge cases)
    for sub in udd.iterdir():
        if not sub.is_dir():
            continue
        prefs = sub / "Preferences"
        if not prefs.exists():
            continue
        try:
            data = json.loads(prefs.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        # Several places the email can live in:
        accounts = (
            (data.get("account_info") or [])
            if isinstance(data.get("account_info"), list)
            else []
        )
        candidates: set[str] = set()
        for a in accounts:
            if isinstance(a, dict) and a.get("email"):
                candidates.add(a["email"].lower())
        signin_email = (
            (data.get("google") or {})
            .get("services", {})
            .get("last_sync_time", "")
        )
        if signin_email:
            candidates.add(str(signin_email).lower())
        if target in candidates:
            logger.info(
                "Matched Chrome profile %r for %s via Preferences", sub.name, email
            )
            return sub.name

    logger.debug("No Chrome profile found for %s", email)
    return None


def find_chrome_executable() -> Optional[str]:
    """Locate chrome.exe on Windows."""
    # 1. PATH lookup
    for name in ("chrome.exe", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    # 2. Standard install locations
    candidates = [
        os.environ.get("PROGRAMFILES", "") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("PROGRAMFILES(X86)", "") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def open_in_chrome_profile(url: str, profile_dir: str) -> bool:
    """
    Open `url` in Chrome using the given profile directory.
    Returns True if a Chrome process was spawned, False otherwise.
    """
    chrome = find_chrome_executable()
    if not chrome:
        logger.warning("chrome.exe not found on this machine.")
        return False
    try:
        subprocess.Popen(
            [chrome, f"--profile-directory={profile_dir}", url],
            close_fds=True,
        )
        logger.info("Launched Chrome (profile=%r) for OAuth.", profile_dir)
        return True
    except Exception as exc:
        logger.warning("Failed to launch Chrome with profile %r: %s", profile_dir, exc)
        return False
