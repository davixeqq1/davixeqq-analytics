"""Reporting period calculation.

YouTube Analytics reports dates in US Pacific time and data typically lags
2-3 days, so the report window ends ``lag_days`` before "today" (Pacific).

With ``week_end`` set (``REPORT_WEEK_END``, default Saturday) that end date is
moved back to the most recent ``week_end`` weekday, so every run between two
anchor days — the scheduled run, a retry, or a manual re-run — selects the
same calendar week, independent of the exact time or timezone offset of the
run. An explicit ``end`` (``--end-date``) is used exactly as given.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ANALYTICS_TZ = ZoneInfo("America/Los_Angeles")

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")  # date.weekday() order
SATURDAY = 5


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def label(self) -> str:
        if self.start.year == self.end.year:
            return f"{self.start:%b} {self.start.day} – {self.end:%b} {self.end.day}, {self.end.year}"
        return f"{self.start:%b} {self.start.day}, {self.start.year} – {self.end:%b} {self.end.day}, {self.end.year}"

    def shift(self, days: int) -> "Period":
        delta = timedelta(days=days)
        return Period(self.start + delta, self.end + delta)


def analytics_today(now: datetime | None = None) -> date:
    now = now or datetime.now(tz=ANALYTICS_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ANALYTICS_TZ)
    return now.astimezone(ANALYTICS_TZ).date()


def compute_periods(
    today: date, lag_days: int = 3, length: int = 7, end: date | None = None, week_end: int | None = None
) -> tuple[Period, Period]:
    """Return (current, previous) consecutive periods of equal length.

    ``week_end`` (0 = Monday … 6 = Sunday, None = rolling window) anchors the
    automatically calculated end to the most recent such weekday on or before
    ``today - lag_days``. It never changes an explicit ``end``.
    """
    if length < 1:
        raise ValueError("length must be >= 1")
    if week_end is not None and not 0 <= week_end <= 6:
        raise ValueError("week_end must be 0 (Monday) … 6 (Sunday) or None")
    if end is None:
        if lag_days < 1:
            raise ValueError("lag_days must be >= 1 (today is never complete)")
        end = today - timedelta(days=lag_days)
        if week_end is not None:
            end -= timedelta(days=(end.weekday() - week_end) % 7)
    elif end >= today:
        raise ValueError(f"end date {end} is not a completed day (today is {today})")
    current = Period(end - timedelta(days=length - 1), end)
    previous = current.shift(-length)
    return current, previous
