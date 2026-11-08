"""Retry helper for Google API requests (quota bursts, transient 5xx)."""

from __future__ import annotations

import logging
import random
import time

from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 403 is retried only for short-term rate limits; quotaExceeded / forbidden are permanent for the day.
RETRYABLE_403_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


def _error_reasons(exc: HttpError) -> set[str]:
    try:
        return {d.get("reason") for d in (exc.error_details or []) if isinstance(d, dict)}
    except Exception:  # pragma: no cover - defensive
        return set()


def is_retryable(exc: HttpError) -> bool:
    status = getattr(exc.resp, "status", None)
    if status in RETRYABLE_STATUS:
        return True
    return status == 403 and bool(_error_reasons(exc) & RETRYABLE_403_REASONS)


def execute_with_retry(request, attempts: int = 4, base_delay: float = 2.0):
    for attempt in range(1, attempts + 1):
        try:
            return request.execute(num_retries=0)
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if not is_retryable(exc) or attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            log.warning("Google API returned %s; retrying in %.1fs (attempt %d/%d)", status, delay, attempt, attempts)
            time.sleep(delay)
        except (ConnectionError, TimeoutError, OSError) as exc:
            if attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            log.warning("Network error (%s); retrying in %.1fs", type(exc).__name__, delay)
            time.sleep(delay)
    raise RuntimeError("unreachable")


def http_error_reason(exc: HttpError) -> str:
    """Short, secret-free description of a Google API error."""
    status = getattr(exc.resp, "status", "?")
    try:
        reason = exc.error_details[0].get("message") if exc.error_details else exc.reason
    except Exception:  # pragma: no cover - defensive
        reason = None
    return f"HTTP {status}: {reason or 'unknown error'}"
