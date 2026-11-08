"""Shared fixtures. Synthetic data for unit tests only — never production statistics."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from yt_report.models import (
    LIVE, LONG_FORM, SHORTS, SOURCE_API, ChannelTotals, DailyStat, FormatStats, VideoStats, WeeklyReport,
)
from yt_report.periods import Period


def make_video(vid, views, pct, ct=LONG_FORM, minutes=None, avd=120.0, title=None, engaged=None):
    return VideoStats(
        video_id=vid, views=views, minutes_watched=minutes if minutes is not None else views * 2.0,
        avg_view_duration_s=avd, avg_view_pct=pct, content_type=ct, subs_gained=1, engaged_views=engaged,
        title=title or f"Video {vid}", thumbnail_url=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
    )


@pytest.fixture
def videos():
    return [
        make_video("L1", 900, 48.0), make_video("L2", 400, 42.0), make_video("L3", 300, 25.0),
        make_video("L4", 120, 45.0), make_video("S1", 1500, 92.0, SHORTS, avd=28), make_video("S2", 700, 85.0, SHORTS, avd=25),
        make_video("S3", 200, 60.0, SHORTS, avd=18), make_video("TINY", 5, 5.0),
    ]


@pytest.fixture
def report(videos):
    from yt_report import analysis
    from yt_report.recommendations import build_recommendations

    cur = Period(date(2026, 10, 29), date(2026, 11, 4))
    prev = cur.shift(-7)
    weakest, note = analysis.find_weakest(videos)
    r = WeeklyReport(
        channel_id="UC_TEST", channel_title="Test Channel", subscriber_count=1234,
        period=cur, previous_period=prev,
        current=ChannelTotals(4125, 8250.0, 120.0, 55.0, 30, 5),
        previous=ChannelTotals(5000, 7000.0, 84.0, 50.0, 20, 8),
        formats_current={
            SHORTS: FormatStats(SHORTS, 2400, 1000.0, 25.0, 88.0, SOURCE_API),
            LONG_FORM: FormatStats(LONG_FORM, 1725, 7250.0, 252.0, 40.0, SOURCE_API),
            LIVE: FormatStats(LIVE, 0, 0.0, None, None, SOURCE_API),
        },
        formats_previous={
            SHORTS: FormatStats(SHORTS, 3000, 1100.0, 22.0, 80.0, SOURCE_API),
            LONG_FORM: FormatStats(LONG_FORM, 2000, 5900.0, 177.0, 38.0, SOURCE_API),
            LIVE: FormatStats(LIVE, 0, 0.0, None, None, SOURCE_API),
        },
        videos=videos, top_videos=analysis.top_videos(videos), weakest=weakest, weakest_note=note,
        daily=[DailyStat(date(2026, 10, 22) + timedelta(days=i), 500 + i, 1000.0, 3, 1) for i in range(14)],
        generated_at=datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc),
    )
    r.recommendations = build_recommendations(r)
    return r
