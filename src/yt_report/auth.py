"""Google OAuth 2.0 (installed-app flow) with safe token storage and refresh."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .config import Config

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


class AuthError(RuntimeError):
    """Raised when valid Google credentials cannot be obtained non-interactively."""


REAUTH_HINT = "Run `python -m yt_report auth` on a machine with a browser, then copy token.json to this host."


def _save_token(creds: Credentials, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Created owner-only (0600 on POSIX) from the first byte, not chmod-ed after writing.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    if os.name == "posix":
        os.chmod(tmp, 0o600)  # a pre-existing .tmp keeps its old mode; tighten it as well
    os.replace(tmp, path)  # atomic: never leaves a half-written token


def _load_existing(cfg: Config) -> tuple[Credentials | None, bool]:
    """Return (credentials, persist_to_file)."""
    if cfg.token_json:
        try:
            info = json.loads(cfg.token_json)
        except json.JSONDecodeError as exc:
            raise AuthError("YT_TOKEN_JSON is not valid JSON.") from exc
        return Credentials.from_authorized_user_info(info), False
    if cfg.token_file.exists():
        try:
            return Credentials.from_authorized_user_file(str(cfg.token_file)), True
        except (ValueError, json.JSONDecodeError):
            log.warning("Token file is unreadable; a new authorization is required.")
    return None, True


def get_credentials(cfg: Config, interactive: bool = False, open_browser: bool = True,
                    save_refreshed: bool = True) -> Credentials:
    """``save_refreshed=False`` keeps a refreshed access token in memory only (used by --dry-run)."""
    creds, persist = _load_existing(cfg)
    persist = persist and save_refreshed

    if creds is not None and not creds.has_scopes(SCOPES):
        log.warning("Stored token is missing required scopes; re-authorization required.")
        creds = None

    if creds is not None and creds.valid:
        return creds

    if creds is not None and creds.refresh_token:
        try:
            creds.refresh(Request())
            if persist:
                _save_token(creds, cfg.token_file)
            log.info("OAuth access token refreshed.")
            return creds
        except RefreshError:
            log.warning("OAuth refresh failed (token expired or revoked).")
            creds = None

    if not interactive:
        raise AuthError(f"No valid Google credentials. {REAUTH_HINT}")

    if not cfg.client_secrets_file.exists():
        raise AuthError(
            f"OAuth client file not found: {cfg.client_secrets_file.name}. "
            "Download it from Google Cloud Console (OAuth client, type 'Desktop app')."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(cfg.client_secrets_file), SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        port=0,
        open_browser=open_browser,
        prompt="consent",
        access_type="offline",
        authorization_prompt_message="Open this URL in your browser to authorize: {url}",
        success_message="Authorization complete. You can close this tab.",
    )
    _save_token(creds, cfg.token_file)
    log.info("Authorization successful; token saved to %s", cfg.token_file.name)
    return creds


HTTP_TIMEOUT_S = 60  # without it a stalled connection could block a cron run indefinitely


def build_services(creds: Credentials, timeout: int = HTTP_TIMEOUT_S):
    """Return (youtube_data_v3, youtube_analytics_v2) service objects with a socket timeout."""
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    def http():
        return AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))

    youtube = build("youtube", "v3", http=http(), cache_discovery=False)
    analytics = build("youtubeAnalytics", "v2", http=http(), cache_discovery=False)
    return youtube, analytics
