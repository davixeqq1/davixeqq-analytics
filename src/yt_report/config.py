"""Configuration loaded from environment variables / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import find_dotenv, load_dotenv

from .periods import SATURDAY, WEEKDAYS

DISCORD_HOSTS = {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}")
    return value


def _week_end(raw: str | None) -> int | None:
    """REPORT_WEEK_END: weekday name (or 3-letter abbreviation) -> 0..6; 'none' = rolling window."""
    value = (raw or "").strip().lower() or "saturday"
    if value in {"none", "rolling"}:
        return None
    for i, name in enumerate(WEEKDAYS):
        if value in (name, name[:3]):
            return i
    raise ConfigError(f"REPORT_WEEK_END must be a weekday name (e.g. saturday) or 'none', got {raw!r}")


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class Config:
    base_dir: Path
    client_secrets_file: Path
    token_file: Path
    token_json: str | None
    discord_webhook_url: str | None
    database_path: Path
    log_dir: Path | None
    log_level: str
    data_lag_days: int
    period_days: int
    min_views_weakest: int
    min_videos_per_format: int
    top_videos_count: int
    report_timezone: str
    ai_enabled: bool
    anthropic_api_key: str | None
    anthropic_model: str
    dashboard_host: str
    dashboard_port: int
    report_week_end: int | None = SATURDAY  # 0 = Monday … 6 = Sunday; None = rolling window

    @property
    def discord_configured(self) -> bool:
        return is_discord_webhook(self.discord_webhook_url)


def is_discord_webhook(url: str | None) -> bool:
    """Only real Discord webhook URLs are accepted, so analytics are never posted elsewhere."""
    if not url or "REPLACE_ME" in url:
        return False
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname in DISCORD_HOSTS
            and parsed.path.startswith("/api/webhooks/") and len(parsed.path.split("/")) >= 5)


def load_config(env_file: str | os.PathLike | None = None) -> Config:
    """Load configuration. Existing environment variables take precedence over .env."""
    env_path = Path(env_file) if env_file else (Path(p) if (p := find_dotenv(usecwd=True)) else None)
    if env_path and env_path.exists():
        load_dotenv(env_path, override=False)
        base_dir = env_path.resolve().parent
    else:
        base_dir = Path.cwd()

    def path(name: str, default: str) -> Path:
        p = Path(os.getenv(name, "").strip() or default).expanduser()
        return p if p.is_absolute() else (base_dir / p)

    log_dir_raw = os.getenv("LOG_DIR", "logs").strip()
    report_tz = os.getenv("REPORT_TIMEZONE", "Europe/Bratislava").strip() or "Europe/Bratislava"
    try:
        ZoneInfo(report_tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"REPORT_TIMEZONE {report_tz!r} is not a valid IANA timezone") from exc
    ai_flag = _bool(os.getenv("AI_RECOMMENDATIONS"), False)
    api_key = _empty_to_none(os.getenv("ANTHROPIC_API_KEY"))

    return Config(
        base_dir=base_dir,
        client_secrets_file=path("YT_CLIENT_SECRETS_FILE", "credentials.json"),
        token_file=path("YT_TOKEN_FILE", "token.json"),
        token_json=_empty_to_none(os.getenv("YT_TOKEN_JSON")),
        discord_webhook_url=_empty_to_none(os.getenv("DISCORD_WEBHOOK_URL")),
        database_path=path("DATABASE_PATH", "data/yt_report.db"),
        log_dir=path("LOG_DIR", "logs") if log_dir_raw else None,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        data_lag_days=_int("DATA_LAG_DAYS", 3, minimum=1),
        period_days=_int("PERIOD_DAYS", 7, minimum=1),
        min_views_weakest=_int("MIN_VIEWS_WEAKEST", 30),
        min_videos_per_format=_int("MIN_VIDEOS_PER_FORMAT", 3, minimum=2),
        top_videos_count=_int("TOP_VIDEOS_COUNT", 3, minimum=1),
        report_timezone=report_tz,
        ai_enabled=ai_flag and api_key is not None,
        anthropic_api_key=api_key,
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "").strip() or "claude-opus-5-5",
        dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1",
        dashboard_port=_int("DASHBOARD_PORT", 8050, minimum=1, maximum=65535),
        report_week_end=_week_end(os.getenv("REPORT_WEEK_END")),
    )
