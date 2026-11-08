"""Reporting periods anchored to calendar weeks (REPORT_WEEK_END, default Saturday).

Runs are simulated at real wall-clock times in Bratislava / UTC and converted
to the Pacific "today" exactly as production does (periods.analytics_today).
"""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from test_clients_and_pipeline import FakeAnalyticsService, FakeYouTubeService
from yt_report.analytics_client import AnalyticsClient
from yt_report.config import ConfigError, load_config
from yt_report.periods import SATURDAY, Period, analytics_today, compute_periods
from yt_report.pipeline import build_report
from yt_report.youtube_client import YouTubeClient

BRATISLAVA = ZoneInfo("Europe/Bratislava")
UTC = ZoneInfo("UTC")
LAG = 3  # production DATA_LAG_DAYS
NOV_1_7 = Period(date(2026, 11, 1), date(2026, 11, 7))


def period_at(when: datetime, lag: int = LAG) -> tuple[Period, Period]:
    return compute_periods(analytics_today(when), lag, 7, week_end=SATURDAY)


def local(y, m, d, h=10, minute=0, tz=BRATISLAVA) -> datetime:
    return datetime(y, m, d, h, minute, tzinfo=tz)


def hourly(start: datetime, end: datetime):
    t = start
    while t <= end:
        yield t
        t += timedelta(hours=1)


# ------------------------------------------------------------- selection ---
def test_tuesday_production_run_selects_november_1_to_7():
    current, previous = period_at(local(2026, 11, 10, 10))           # Tue 10:00 CET = Tue 01:00 PST
    assert current == NOV_1_7
    assert previous == Period(date(2026, 10, 25), date(2026, 10, 31))


def test_scheduled_run_and_retries_select_identical_periods():
    runs = [local(2026, 11, 10, 10), local(2026, 11, 10, 14),        # Pi cron run + retry (CET)
            local(2026, 11, 10, 9, tz=UTC), local(2026, 11, 10, 13, tz=UTC)]  # GitHub schedule + manual re-run
    assert {period_at(r) for r in runs} == {(NOV_1_7, NOV_1_7.shift(-7))}


def test_tuesday_run_and_following_days_up_to_monday_select_the_same_week():
    # Every execution from Tuesday 00:00 PT to the following Monday 23:00 PT reports Nov 1–7,
    # so a Monday re-run (Nov 16) or any retry still targets the week the Tuesday run reported.
    la = ZoneInfo("America/Los_Angeles")
    periods = {period_at(t)[0] for t in hourly(datetime(2026, 11, 10, 0, tzinfo=la), datetime(2026, 11, 16, 23, tzinfo=la))}
    assert periods == {NOV_1_7}


def test_monday_before_the_tuesday_still_reports_the_previous_complete_week():
    # With DATA_LAG_DAYS=3 Saturday Nov 7 is not lag-complete before PT Tuesday, so a Monday run
    # (any time of Monday in Bratislava) selects Oct 25–31, never a partially processed week.
    for hour in (0, 8, 10, 12, 23):
        assert period_at(local(2026, 11, 9, hour))[0] == Period(date(2026, 10, 25), date(2026, 10, 31))


def test_dry_run_time_of_investigation_selects_last_complete_week():
    # 2026-11-08 10:23 CET = PT Sunday: Nov 1–7 is not selectable yet (lag does not permit it).
    assert period_at(local(2026, 11, 8, 10, 23))[0] == Period(date(2026, 10, 25), date(2026, 10, 31))


def test_week_switches_exactly_at_pacific_midnight_tuesday():
    la = ZoneInfo("America/Los_Angeles")
    assert period_at(datetime(2026, 11, 9, 23, 59, tzinfo=la))[0].end == date(2026, 10, 31)
    assert period_at(datetime(2026, 11, 10, 0, 0, tzinfo=la))[0].end == date(2026, 11, 7)


# ------------------------------------------------------------- invariants ---
def test_every_period_is_sunday_to_saturday_with_previous_week_before():
    for offset in range(400):  # every "today" from 2026-06-01 over 400 days
        today = date(2026, 6, 1) + timedelta(days=offset)
        current, previous = compute_periods(today, LAG, 7, week_end=SATURDAY)
        assert current.start.weekday() == 6 and current.end.weekday() == 5 and current.days == 7, today
        assert previous.days == 7 and previous.end + timedelta(days=1) == current.start, today
        assert previous == current.shift(-7)
        assert current.end <= today - timedelta(days=LAG), today                      # never inside the lag window
        assert today - timedelta(days=LAG) - current.end < timedelta(days=7), today   # the most recent such week


def _distinct_periods(times):
    seen = []
    for t in times:
        p = period_at(t)[0]
        if not seen or seen[-1] != p:
            seen.append(p)
    return seen


def _assert_contiguous(periods):
    assert len(periods) >= 2
    for a, b in zip(periods, periods[1:]):
        assert b.start == a.end + timedelta(days=1), (a, b)          # no gap, no overlap
        assert b.days == a.days == 7


def test_consecutive_periods_never_overlap_or_gap_across_a_full_year():
    # Hourly wall-clock simulation (UTC) over Oct 2026 – Oct 2027: both DST changes in both regions.
    periods = _distinct_periods(hourly(datetime(2026, 10, 1, tzinfo=UTC), datetime(2027, 10, 31, tzinfo=UTC)))
    _assert_contiguous(periods)
    # First hour = PT Wed 2026-09-30 17:00 -> Sep 27 -> Sat Sep 26.
    # Last hour  = PT Sat 2027-10-30 17:00 -> Oct 27 -> Sat Oct 23.
    assert periods[0] == Period(date(2026, 9, 20), date(2026, 9, 26))
    assert periods[-1] == Period(date(2027, 10, 17), date(2027, 10, 23))
    assert len(periods) == (periods[-1].end - periods[0].end).days // 7 + 1


@pytest.mark.parametrize("label,first,last", [
    ("Oct/Nov 2026: EU ends DST Oct 25, US ends DST Nov 1", date(2026, 10, 6), date(2026, 11, 24)),
    ("Mar 2027: US starts DST Mar 14, EU starts DST Mar 28", date(2027, 3, 2), date(2027, 4, 6)),
])
def test_weekly_tuesday_10_00_runs_across_dst_transitions(label, first, last):
    runs, d = [], first
    while d <= last:
        runs.append(local(d.year, d.month, d.day, 10))                # cron 0 10 * * 2 (Europe/Bratislava)
        runs.append(local(d.year, d.month, d.day, 14))                # retry 0 14 * * 2
        runs.append(datetime(d.year, d.month, d.day, 9, tzinfo=UTC))  # GitHub cron 0 9 * * 2
        d += timedelta(days=7)
    by_day: dict[date, set] = {}
    for r in runs:
        by_day.setdefault(r.date(), set()).add(period_at(r)[0])
    assert all(len(s) == 1 for s in by_day.values()), label            # run, retry and GitHub agree every week
    weekly = [next(iter(by_day[k])) for k in sorted(by_day)]
    _assert_contiguous(weekly)
    for r in runs:                                                     # Tuesday 10:00 CET is always Tuesday in PT
        assert analytics_today(r).weekday() == 1, (label, r)


def test_hourly_runs_across_dst_windows_only_step_by_one_week():
    for start, end in [(datetime(2026, 10, 18, tzinfo=UTC), datetime(2026, 11, 15, tzinfo=UTC)),
                       (datetime(2027, 3, 7, tzinfo=UTC), datetime(2027, 4, 4, tzinfo=UTC))]:
        _assert_contiguous(_distinct_periods(hourly(start, end)))


# ------------------------------------------------------- explicit end date ---
@pytest.mark.parametrize("end", [date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 7), date(2026, 10, 31)])
def test_explicit_end_date_is_never_moved(end):
    current, previous = compute_periods(date(2026, 11, 10), LAG, 7, end=end, week_end=SATURDAY)
    assert current == Period(end - timedelta(days=6), end) and previous == current.shift(-7)


@pytest.fixture
def anchored_cfg(tmp_path, monkeypatch):
    for k in ("AI_RECOMMENDATIONS", "ANTHROPIC_API_KEY", "YT_TOKEN_JSON", "DISCORD_WEBHOOK_URL", "REPORT_WEEK_END"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("DATA_LAG_DAYS=3\nLOG_DIR=\n")                     # REPORT_WEEK_END not set -> default Saturday
    return load_config(env)


def test_pipeline_keeps_report_1_period_when_given_explicitly(anchored_cfg, caplog):
    with caplog.at_level(logging.WARNING, logger="yt_report.pipeline"):
        rep = build_report(anchored_cfg, YouTubeClient(FakeYouTubeService()), AnalyticsClient(FakeAnalyticsService()),
                           end=date(2026, 11, 4), today=date(2026, 11, 10))
    assert rep.period == Period(date(2026, 10, 29), date(2026, 11, 4))     # report #1's period, unchanged
    assert rep.previous_period == Period(date(2026, 10, 22), date(2026, 10, 28))
    assert any("is a Wednesday, not the configured week end (Saturday)" in r.message for r in caplog.records)


def test_pipeline_uses_anchored_period_by_default(anchored_cfg, caplog):
    with caplog.at_level(logging.WARNING, logger="yt_report.pipeline"):
        rep = build_report(anchored_cfg, YouTubeClient(FakeYouTubeService()), AnalyticsClient(FakeAnalyticsService()),
                           today=date(2026, 11, 10))
    assert rep.period == NOV_1_7 and rep.previous_period == NOV_1_7.shift(-7)
    assert not any("configured week end" in r.message for r in caplog.records)


def test_demo_reports_follow_calendar_weeks(anchored_cfg):
    from yt_report.demo import build_demo_reports

    reps = build_demo_reports(anchored_cfg, weeks=4, today=date(2026, 11, 10))
    assert reps[-1].period == NOV_1_7
    _assert_contiguous([r.period for r in reps])


# ------------------------------------------------------------------ config ---
@pytest.mark.parametrize("raw,expected", [
    (None, SATURDAY), ("", SATURDAY), ("saturday", SATURDAY), ("Saturday", SATURDAY), ("SAT", SATURDAY),
    ("sunday", 6), ("thu", 3), ("monday", 0), ("none", None), ("rolling", None),
])
def test_report_week_end_parsing(tmp_path, monkeypatch, raw, expected):
    monkeypatch.delenv("REPORT_WEEK_END", raising=False)
    env = tmp_path / ".env"
    env.write_text("" if raw is None else f"REPORT_WEEK_END={raw}\n")
    assert load_config(env).report_week_end == expected


@pytest.mark.parametrize("raw", ["saturdays", "6", "weekend"])
def test_invalid_report_week_end_is_rejected(tmp_path, monkeypatch, raw):
    monkeypatch.delenv("REPORT_WEEK_END", raising=False)
    env = tmp_path / ".env"
    env.write_text(f"REPORT_WEEK_END={raw}\n")
    with pytest.raises(ConfigError):
        load_config(env)


def test_invalid_week_end_index_is_rejected():
    with pytest.raises(ValueError):
        compute_periods(date(2026, 11, 10), LAG, 7, week_end=7)


def test_rolling_window_is_unchanged_when_disabled():
    assert compute_periods(date(2026, 11, 8), LAG, 7)[0] == Period(date(2026, 10, 30), date(2026, 11, 5))
    assert compute_periods(date(2026, 11, 8), LAG, 7, week_end=None)[0] == Period(date(2026, 10, 30), date(2026, 11, 5))
