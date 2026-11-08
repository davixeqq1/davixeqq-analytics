from datetime import date, datetime, timezone

import pytest

from yt_report.periods import Period, analytics_today, compute_periods


def test_default_periods_with_lag():
    cur, prev = compute_periods(date(2026, 11, 9), lag_days=3)
    assert cur == Period(date(2026, 10, 31), date(2026, 11, 6))
    assert prev == Period(date(2026, 10, 24), date(2026, 10, 30))
    assert cur.days == prev.days == 7
    assert (cur.start - prev.end).days == 1  # contiguous, non-overlapping


def test_explicit_end_date():
    cur, prev = compute_periods(date(2026, 11, 9), end=date(2026, 11, 1))
    assert cur.end == date(2026, 11, 1) and cur.start == date(2026, 10, 26)
    assert prev.end == date(2026, 10, 25)


def test_rejects_incomplete_day():
    with pytest.raises(ValueError):
        compute_periods(date(2026, 11, 9), end=date(2026, 11, 9))
    with pytest.raises(ValueError):
        compute_periods(date(2026, 11, 9), lag_days=0)


def test_year_boundary_label():
    p = Period(date(2025, 12, 29), date(2026, 1, 4))
    assert "2025" in p.label() and "2026" in p.label()


def test_analytics_today_uses_pacific_time():
    # 05:00 UTC on Nov 9 is still Nov 8 in Los Angeles.
    assert analytics_today(datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc)) == date(2026, 11, 8)
