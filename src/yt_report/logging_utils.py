"""Logging setup with secret redaction."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS = [
    (re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+"), "https://discord.com/api/webhooks/[REDACTED]"),
    (re.compile(r"ya29\.[\w\-.]+"), "[REDACTED_ACCESS_TOKEN]"),
    (re.compile(r"1//[\w\-]{20,}"), "[REDACTED_REFRESH_TOKEN]"),
    (re.compile(r"sk-ant-[\w\-]+"), "[REDACTED_API_KEY]"),
    (re.compile(r"GOCSPX-[\w\-]+"), "[REDACTED_CLIENT_SECRET]"),
    (re.compile(r"(?i)(\"?(?:client_secret|refresh_token|access_token|token|api_key)\"?\s*[:=]\s*\"?)[^\s\",}]+"), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_dir / "yt_report.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    # Google client libraries can be chatty and may log request URLs.
    for noisy in ("googleapiclient", "google_auth_oauthlib", "urllib3", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
