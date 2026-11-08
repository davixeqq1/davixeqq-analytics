"""v1.2.0: format-filtered per-video rows, coverage, publication statistics,
views-trend decision tree, schema v2 migration, Discord fields. Synthetic data only."""

import json
import sqlite3
from datetime import date, timedelta

import pytest

from test_clients_and_pipeline import FakeAnalyticsService, FakeRequest, FakeYouTubeService, _resp
from yt_report import analysis
from yt_report import discord_notifier as dn
from yt_report import storage as storage_mod
from yt_report.ai_recommendations import metrics_payload
from yt_report.analytics_client import (
    FALLBACK_FILTER_FAILED, MAX_VIDEO_ROWS, AnalyticsClient, derive_formats,
)
from yt_report.config import load_config
from yt_report.models import (
    LIVE, LONG_FORM, METHOD_BY_FORMAT, METHOD_SINGLE_QUERY, PUB_OK, SHORTS, SOURCE_API, UNSPECIFIED, Coverage,
    DailyStat, FormatStats, PublicationStats, ViewsByAge,
)
from yt_report.periods import Period
from yt_report.pipeline import build_report
from yt_report.recommendations import (
    BRANCH_PERFORMANCE, BRANCH_PUBLISHING, BRANCH_UNAVAILABLE, BRANCH_UNSEPARATED, BRANCH_UPLOAD_SPIKE,
    build_recommendations, views_trend_branch,
)
from yt_report.storage import SCHEMA, SCHEMA_VERSION, Storage
from yt_report.youtube_client import MAX_UPLOAD_PAGES, VideoMeta, YouTubeClient

TODAY = date(2026, 11, 9)
CUR = Period(date(2026, 10, 31), date(2026, 11, 6))   # periods produced by the pipeline for TODAY
PREV = CUR.shift(-7)
P = Period(date(2026, 10, 29), date(2026, 11, 4))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for k in ("AI_RECOMMENDATIONS", "ANTHROPIC_API_KEY", "YT_TOKEN_JSON", "DISCORD_WEBHOOK_URL", "REPORT_TIMEZONE",
              "REPORT_WEEK_END"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    # Rolling window: these tests exercise metrics, not period anchoring (see test_period_anchor.py).
    env.write_text("DATA_LAG_DAYS=3\nMIN_VIEWS_WEAKEST=10\nMIN_VIDEOS_PER_FORMAT=2\nLOG_DIR=\nREPORT_WEEK_END=none\n")
    return load_config(env)


def _build(cfg, an=None, yt=None):
    return build_report(cfg, YouTubeClient(yt or FakeYouTubeService()), AnalyticsClient(an or FakeAnalyticsService()),
                        today=TODAY)


def _fmt_totals(shorts=500, vod=380, live=120, unspecified=0):
    return {SHORTS: FormatStats(SHORTS, shorts, source=SOURCE_API), LONG_FORM: FormatStats(LONG_FORM, vod, source=SOURCE_API),
            LIVE: FormatStats(LIVE, live, source=SOURCE_API), UNSPECIFIED: FormatStats(UNSPECIFIED, unspecified, source=SOURCE_API)}


class ManyRows(FakeAnalyticsService):
    """Per-video query returning ``n`` rows of ``each`` views (single-query path only)."""

    def __init__(self, n, each, **kw):
        super().__init__(format_supported=False, format_filter_supported=False, **kw)
        self.n, self.each = n, each

    def query(self, **kw):
        if kw.get("dimensions") == "video":
            self.calls.append(kw)
            rows = [[f"x{i:03d}", self.each, self.each // 2, 1, 30, 50.0, 0] for i in range(self.n)]
            return FakeRequest(lambda: _resp(["video", "views", "engagedViews", "estimatedMinutesWatched",
                                              "averageViewDuration", "averageViewPercentage", "subscribersGained"], rows))
        return super().query(**kw)


# ======================================================= per-video rows ====
def test_by_format_rows_match_totals_and_are_complete():
    svc = FakeAnalyticsService()
    videos, typed, cov = AnalyticsClient(svc).fetch_videos(P, _fmt_totals(), 1000)
    assert typed and cov.method == METHOD_BY_FORMAT and cov.complete and cov.ratio == 1.0
    assert cov.fallback_reason is None and cov.views_rows == 1000 and cov.rows == 5
    filters = sorted(c["filters"] for c in svc.calls if c.get("dimensions") == "video")
    assert filters == ["creatorContentType==liveStream", "creatorContentType==shorts", "creatorContentType==videoOnDemand"]
    assert {v.video_id: v.content_type for v in videos}["l1"] == LIVE
    assert [v.views for v in videos] == sorted((v.views for v in videos), reverse=True)
    assert not any(c.get("dimensions") == "video,creatorContentType" for c in svc.calls)  # type known from the query


def test_format_filter_failure_falls_back_to_single_query():
    svc = FakeAnalyticsService(format_filter_supported=False)
    videos, typed, cov = AnalyticsClient(svc).fetch_videos(P, _fmt_totals(), 1000)
    assert cov.method == METHOD_SINGLE_QUERY and cov.fallback_reason == FALLBACK_FILTER_FAILED
    assert typed and cov.complete and len(videos) == 6  # incl. the zero-view row
    assert any(c.get("dimensions") == "video,creatorContentType" for c in svc.calls)


def test_rows_above_format_total_keep_primary_and_flag_inconsistency(cfg):
    # Long-form rows sum to 380 but the API format total says 300 (coverage fix 2026-11-08: no blind fallback).
    rows = [["videoOnDemand", 300, 2500, 395, 39.5], ["shorts", 500, 240, 29, 84.0], ["liveStream", 120, 400, 200, 5.0]]
    svc = FakeAnalyticsService(format_rows=rows)
    rep = _build(cfg, svc)
    cov = rep.coverage_current
    assert cov.method == METHOD_BY_FORMAT and cov.fallback_reason is None
    assert cov.excess_views == 80 and not cov.complete                 # never "complete" with inconsistent data
    assert cov.views_rows == 500 + 300 + 120 == cov.views_reference    # excess capped per type, cannot hide gaps
    assert {v.video_id for v in rep.videos} == {"s1", "s2", "v1", "v2", "l1"}  # primary rows kept
    assert not any(c.get("dimensions") == "video" and not c.get("filters") for c in svc.calls)  # no single query
    assert any("exceed the API format totals by 80 views" in n for n in rep.notes)
    assert "Per-video rows exceed the format totals by 80 views" in analysis.completeness_issues(rep)


def test_no_format_totals_uses_channel_total_as_reference():
    videos, typed, cov = AnalyticsClient(FakeAnalyticsService()).fetch_videos(P, None, 1000)
    assert cov.method == METHOD_SINGLE_QUERY and cov.views_reference == 1000 and cov.complete


def test_unspecified_views_make_by_format_coverage_incomplete():
    _, _, cov = AnalyticsClient(FakeAnalyticsService()).fetch_videos(P, _fmt_totals(unspecified=25), 1025)
    assert cov.method == METHOD_BY_FORMAT and not cov.complete and cov.missing_views == 25


def test_exactly_200_rows_with_matching_sum_is_complete():  # regression B3
    svc = ManyRows(MAX_VIDEO_ROWS, 5)
    videos, _, cov = AnalyticsClient(svc).fetch_videos(P, None, MAX_VIDEO_ROWS * 5)
    assert len(videos) == 200 and cov.capped and cov.complete
    assert all(f.complete for f in derive_formats(videos, complete=cov.complete).values())


def test_200_rows_with_missing_views_are_reported(cfg):
    class Capped(ManyRows):
        def query(self, **kw):
            if kw.get("dimensions") is None:  # channel total larger than the 200 rows
                return FakeRequest(lambda: _resp(["views", "estimatedMinutesWatched", "averageViewDuration",
                                                  "averageViewPercentage", "subscribersGained", "subscribersLost"],
                                                 [[1010, 3140, 188, 40.1, 12, 2]]))
            return super().query(**kw)

    rep = _build(cfg, Capped(MAX_VIDEO_ROWS, 5))
    cov = rep.coverage_current
    assert cov.capped and not cov.complete and cov.missing_views == 10
    assert any("account for 1,000 of 1,010 views (99.0%): single query returned 200 rows, the API limit of 200 rows" in n
               for n in rep.notes)
    assert not all(f.complete for f in rep.formats_current.values() if f.source == "derived")
    completeness = dn.build_embeds(rep)[0]["fields"][-1]
    assert completeness["name"] == "Data completeness" and "⚠ Per-video rows cover 99.0% of views" in completeness["value"]


def test_capped_format_query_with_large_last_row_warns_about_weakest():
    cov = Coverage(METHOD_BY_FORMAT, 200, 5000, 5000, capped=True, capped_min_views=40)
    notes = analysis.completeness_notes(cov, None, None, None, [], True, weakest_min_views=30)
    assert any("weakest-video check may have missed" in n for n in notes)
    cov.capped_min_views = 3
    notes = analysis.completeness_notes(cov, None, None, None, [], True, weakest_min_views=30)
    assert not any("weakest-video" in n for n in notes)


def test_by_format_capped_query_is_flagged():
    class OneFormatCapped(FakeAnalyticsService):
        def query(self, **kw):
            if kw.get("filters") == "creatorContentType==shorts":
                rows = [[f"s{i}", 2, 1, 1, 20, 80.0, 0] for i in range(MAX_VIDEO_ROWS)]
                return FakeRequest(lambda: _resp(["video", "views", "engagedViews", "estimatedMinutesWatched",
                                                  "averageViewDuration", "averageViewPercentage", "subscribersGained"], rows))
            return super().query(**kw)

    _, _, cov = AnalyticsClient(OneFormatCapped()).fetch_videos(P, _fmt_totals(shorts=400), 900)
    assert cov.method == METHOD_BY_FORMAT and cov.capped and cov.capped_min_views == 2 and cov.complete


def test_video_under_two_formats_is_merged_and_unclassified():
    class Dup(FakeAnalyticsService):
        def query(self, **kw):
            flt = kw.get("filters") or ""
            if flt == "creatorContentType==liveStream":
                return FakeRequest(lambda: _resp(["video", "views", "engagedViews", "estimatedMinutesWatched",
                                                  "averageViewDuration", "averageViewPercentage", "subscribersGained"],
                                                 [["l1", 100, 30, 300, 200, 5.0, 0], ["v1", 20, 5, 50, 100, 10.0, 0]]))
            return super().query(**kw)

    videos, _, cov = AnalyticsClient(Dup()).fetch_videos(P, _fmt_totals(), 1000)
    v1 = next(v for v in videos if v.video_id == "v1")
    assert v1.views == 320 and v1.content_type is None and v1.avg_view_pct is None
    assert cov.views_rows == 1000


@pytest.mark.parametrize("rows,ref,expected", [(9999, 10000, "99.9%"), (99999, 100000, "99.9%"), (500, 1000, "50.0%"),
                                               (10, 10, "100.0%"), (0, 0, "100.0%")])
def test_coverage_text_never_rounds_incomplete_up_to_100(rows, ref, expected):
    assert analysis.coverage_pct_text(Coverage(METHOD_BY_FORMAT, 1, rows, ref)) == expected


# ================================================================ daily ====
def test_missing_day_in_the_middle_is_noted():  # regression B4
    start = date(2026, 10, 22)
    daily = [DailyStat(start + timedelta(days=i), 1, 1.0, 0, 0) for i in range(14) if i != 5]
    gaps = analysis.missing_days(daily, start, start + timedelta(days=13))
    assert gaps == [date(2026, 10, 27)]
    notes = analysis.completeness_notes(None, None, None, None, gaps, True)
    assert any("1 day(s): Oct 27" in n for n in notes)


def test_full_daily_series_has_no_gap_note():
    start = date(2026, 10, 22)
    daily = [DailyStat(start + timedelta(days=i), 1, 1.0, 0, 0) for i in range(14)]
    assert analysis.missing_days(daily, start, start + timedelta(days=13)) == []


# ======================================================== uploads scan ====
class Pages:
    """playlistItems fake returning predefined pages keyed by page token."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def playlistItems(self):  # noqa: N802
        return self

    def list(self, **kw):
        self.calls.append(kw)
        return FakeRequest(lambda: self.pages[kw.get("pageToken")])


def _page(ids, nxt=None):
    resp = {"items": [{"contentDetails": {"videoId": i}} for i in ids]}
    if nxt is not None:
        resp["nextPageToken"] = nxt
    return resp


def test_uploads_scan_pages_dedupes_and_completes():
    svc = Pages({None: _page(["b", "a"], "t1"), "t1": _page(["c", "a", "d"], "t2"), "t2": _page(["e"])})
    scan = YouTubeClient(svc).list_uploads("UU1")
    assert scan.complete and scan.pages == 3 and scan.duplicates == 1 and scan.reason is None
    assert scan.video_ids == ["b", "a", "c", "d", "e"]  # playlist order kept; not assumed to be by date
    assert svc.calls[1]["pageToken"] == "t1" and "pageToken" not in svc.calls[0]


def test_uploads_scan_page_limit_is_incomplete():
    pages = {None: _page(["v0"], "t1")} | {f"t{i}": _page([f"v{i}"], f"t{i + 1}") for i in range(1, 60)}
    scan = YouTubeClient(Pages(pages)).list_uploads("UU1")
    assert scan.pages == MAX_UPLOAD_PAGES == 40 and not scan.complete and scan.reason == "page_limit"
    assert len(scan.video_ids) == 40


def test_uploads_scan_exactly_at_limit_without_next_token_is_complete():
    pages = {None: _page(["v0"], "t1")} | {f"t{i}": _page([f"v{i}"], f"t{i + 1}" if i < 2 else None) for i in range(1, 3)}
    scan = YouTubeClient(Pages(pages)).list_uploads("UU1", max_pages=3)
    assert scan.complete and scan.pages == 3


def test_uploads_scan_repeated_token_stops_safely():
    scan = YouTubeClient(Pages({None: _page(["a"], "t1"), "t1": _page(["b"], "t1")})).list_uploads("UU1")
    assert not scan.complete and scan.reason == "repeated_page_token" and scan.video_ids == ["a", "b"]


def test_uploads_scan_handles_missing_items_and_empty_token():
    pages = {None: {"nextPageToken": "t1"}, "t1": {"items": [{"contentDetails": {}}, {"contentDetails": {"videoId": "x"}}],
                                                   "nextPageToken": ""}}
    scan = YouTubeClient(Pages(pages)).list_uploads("UU1")
    assert scan.complete and scan.video_ids == ["x"]


# =================================================== publication stats ====
def _meta(vid, published, privacy="public", live=None, actual=None, scheduled=None):
    return VideoMeta(vid, vid, None, published, privacy_status=privacy, live_broadcast_content=live or "none",
                     actual_start_time=actual, scheduled_start_time=scheduled)


def _stats(period, metas, types, complete=True):
    return analysis.publication_stats(period, list(metas), metas, types, complete)


def test_pacific_boundary_late_evening_belongs_to_previous_day():
    # 06:30 UTC on Oct 31 = 23:30 PDT on Oct 30.
    metas = {"a": _meta("a", "2026-10-31T06:30:00Z")}
    assert _stats(Period(date(2026, 10, 31), date(2026, 11, 6)), metas, {"a": SHORTS}).count(SHORTS) == 0
    assert _stats(Period(date(2026, 10, 24), date(2026, 10, 30)), metas, {"a": SHORTS}).count(SHORTS) == 1


def test_pacific_boundary_after_midnight_belongs_to_new_day():
    # 07:30 UTC on Oct 31 = 00:30 PDT on Oct 31.
    metas = {"a": _meta("a", "2026-10-31T07:30:00Z")}
    assert _stats(Period(date(2026, 10, 31), date(2026, 11, 6)), metas, {"a": SHORTS}).count(SHORTS) == 1


def test_pacific_boundary_after_dst_change():
    # DST ends Nov 1, 2026: 07:30 UTC on Nov 8 = 23:30 PST on Nov 7 (UTC-8, not -7).
    metas = {"a": _meta("a", "2026-11-08T07:30:00Z")}
    assert _stats(Period(date(2026, 11, 1), date(2026, 11, 7)), metas, {"a": LONG_FORM}).count(LONG_FORM) == 1
    assert _stats(Period(date(2026, 11, 8), date(2026, 11, 14)), metas, {"a": LONG_FORM}).count(LONG_FORM) == 0


def test_private_and_unlisted_are_excluded_but_counted():
    metas = {"p": _meta("p", "2026-11-01T12:00:00Z", "private"), "u": _meta("u", "2026-11-02T12:00:00Z", "unlisted"),
             "x": _meta("x", "2026-11-03T12:00:00Z")}
    s = _stats(CUR, metas, {"p": SHORTS, "u": SHORTS, "x": SHORTS})
    assert s.count(SHORTS) == 1 and s.private == 1 and s.unlisted == 1 and s.video_ids == ["x"]


def test_upcoming_livestream_is_never_counted():
    metas = {"up": _meta("up", "2026-11-01T12:00:00Z", live="upcoming", scheduled="2026-11-03T18:00:00Z")}
    s = _stats(CUR, metas, {"up": LIVE})
    assert s.count(LIVE) == 0 and s.total_public == 0 and s.upcoming == 1


def test_premiere_classified_as_video_on_demand_is_long_form():
    metas = {"pr": _meta("pr", "2026-11-02T12:00:00Z", actual="2026-11-02T18:00:00Z")}
    s = _stats(CUR, metas, {"pr": LONG_FORM})
    assert s.count(LONG_FORM) == 1 and s.count(LIVE) == 0


def test_livestream_dated_by_actual_start_time():
    # Broadcast created on Oct 28 (previous period), went live on Nov 2 (current period).
    metas = {"l": _meta("l", "2026-10-28T12:00:00Z", actual="2026-11-02T18:00:00Z")}
    assert _stats(CUR, metas, {"l": LIVE}).count(LIVE) == 1
    assert _stats(PREV, metas, {"l": LIVE}).count(LIVE) == 0


def test_new_upload_without_analytics_type_stays_unclassified():
    metas = {"n": _meta("n", "2026-11-05T12:00:00Z")}
    s = _stats(CUR, metas, {})
    assert s.unclassified == 1 and s.published == {} and s.video_ids == ["n"]


def test_duplicate_upload_ids_counted_once_and_missing_metadata_is_incomplete():
    metas = {"a": _meta("a", "2026-11-02T12:00:00Z")}
    s = analysis.publication_stats(CUR, ["a", "a", "gone"], metas, {"a": SHORTS}, True)
    assert s.count(SHORTS) == 1 and not s.complete and s.incomplete_reason == "missing_metadata"


def test_deleted_upload_outside_period_keeps_stats_complete():
    # Live finding: the uploads playlist still lists a video deleted in 2024 ("Deleted video", no metadata).
    metas = {"a": _meta("a", "2026-11-02T12:00:00Z")}
    s = analysis.publication_stats(CUR, ["a", "gone"], metas, {"a": SHORTS}, True,
                                   item_dates={"gone": "2024-05-21T10:49:17Z"})
    assert s.complete and s.count(SHORTS) == 1 and s.total_public == 1


def test_deleted_upload_inside_period_is_never_counted_but_marks_incomplete():
    s = analysis.publication_stats(CUR, ["gone"], {}, {}, True, item_dates={"gone": "2026-11-02T12:00:00Z"})
    assert s.total_public == 0 and not s.complete


def test_uploads_scan_records_item_dates():
    pages = {None: {"items": [{"snippet": {"publishedAt": "2024-05-21T10:49:17Z"}, "contentDetails": {"videoId": "d"}},
                              {"contentDetails": {"videoId": "e", "videoPublishedAt": "2026-11-01T00:00:00Z"}}]}}
    svc = Pages(pages)
    scan = YouTubeClient(svc).list_uploads("UU1")
    assert scan.item_dates == {"d": "2024-05-21T10:49:17Z", "e": "2026-11-01T00:00:00Z"}
    assert svc.calls[0]["part"] == "snippet,contentDetails"


# ================================================ pipeline integration ====
NEW_UPLOADS = {
    **FakeYouTubeService.DEFAULT_UPLOADS,
    "s1": {"publishedAt": "2026-11-02T15:00:00Z"},                          # new Short this period
    "l1": {"publishedAt": "2026-10-20T15:00:00Z", "actualStartTime": "2026-11-03T18:00:00Z"},  # live, by start
    "s2": {"publishedAt": "2026-10-26T15:00:00Z"},                          # Short last period
    "n1": {"publishedAt": "2026-11-05T15:00:00Z"},                          # no analytics yet -> unclassified
    "pv": {"publishedAt": "2026-11-04T15:00:00Z", "privacy": "private"},
    "ul": {"publishedAt": "2026-11-01T15:00:00Z", "privacy": "unlisted"},
    "up": {"publishedAt": "2026-11-01T15:00:00Z", "liveBroadcastContent": "upcoming"},
}


def test_pipeline_publication_counts(cfg):
    an = FakeAnalyticsService()
    rep = _build(cfg, an, FakeYouTubeService(uploads=NEW_UPLOADS))
    pc, pp = rep.publications_current, rep.publications_previous
    assert pc.status == PUB_OK and pc.complete and pp.complete
    assert (pc.count(SHORTS), pc.count(LONG_FORM), pc.count(LIVE), pc.unclassified) == (1, 0, 1, 1)
    assert (pc.private, pc.unlisted) == (1, 1)
    assert pp.count(SHORTS) == 1 and pp.total_public == 1
    lookups = [c for c in an.calls if c.get("dimensions") == "video,creatorContentType"]
    assert lookups and all(c["filters"].startswith("video==") for c in lookups)
    assert "n1" in lookups[0]["filters"] and "up" not in lookups[0]["filters"]
    assert any("1 public upload(s) in this period have no YouTube Analytics format" in n for n in rep.notes)
    age = rep.views_by_age
    assert (age.new_cur, age.prev_uploads_cur, age.new_prev) == (520, 100, 100) and age.complete


def test_pipeline_survives_playlist_error(cfg):
    rep = _build(cfg, yt=FakeYouTubeService(playlist_error=True))
    assert not rep.publications_current.available and rep.views_by_age is None
    assert any("Publication statistics unavailable (YouTube Data API error)" in n for n in rep.notes)
    assert "Publication counts unavailable" in analysis.completeness_issues(rep)


def test_pipeline_without_uploads_playlist(cfg):
    rep = _build(cfg, yt=FakeYouTubeService(with_playlist=False))
    assert rep.publications_current.incomplete_reason == "no_uploads_playlist"


def test_pipeline_page_limit_marks_publications_incomplete(cfg):
    uploads = {f"old{i}": {} for i in range(41 * 2)}
    rep = _build(cfg, yt=FakeYouTubeService(uploads=uploads, page_size=2))
    assert not rep.publications_current.complete
    assert any("stopped at the 40-page limit" in n for n in rep.notes)
    published = dn.build_embeds(rep)[0]["fields"][-2]
    assert published["name"] == "📅 Published" and "incomplete scan" in published["value"]


class RevisedChannelTotal(FakeAnalyticsService):
    """Observed API behaviour (synthetic numbers): format totals and per-video rows sum to 1,990,
    while the revised channel total (basic report) says 1,985."""

    VIDEO_ROWS = [  # video, views, engagedViews, minutes, avd, avp, subs
        ["s1", 1000, 450, 400, 30, 60.0, 1], ["s2", 40, 18, 15, 30, 55.0, 0], ["v1", 700, 280, 750, 150, 32.0, 2],
        ["v2", 50, 20, 50, 150, 30.0, 0], ["l1", 200, 70, 200, 180, 5.0, 0],
    ]

    def __init__(self, **kw):
        super().__init__(format_rows=[["shorts", 1040, 415, 30, 59.0], ["videoOnDemand", 750, 800, 150, 31.0],
                                      ["liveStream", 200, 200, 180, 5.0]], **kw)

    def query(self, **kw):
        if kw.get("dimensions") is None:
            self.calls.append(kw)
            views = 1985 if kw["startDate"] >= "2026-10-31" else 1989
            return FakeRequest(lambda: _resp(["views", "estimatedMinutesWatched", "averageViewDuration",
                                              "averageViewPercentage", "subscribersGained", "subscribersLost"],
                                             [[views, 1400, 80, 20.0, 10, 5]]))
        return super().query(**kw)


def test_views_by_age_uses_the_same_base_as_the_video_rows(cfg):
    rep = _build(cfg, RevisedChannelTotal(), FakeYouTubeService(uploads=NEW_UPLOADS))
    assert rep.current.views == 1985 and rep.coverage_current.views_reference == 1990 and rep.coverage_current.complete
    age = rep.views_by_age
    assert (age.total_cur, age.total_prev) == (1990, 1990)            # format-total base, not 1,985 / 1,989
    assert (age.new_cur, age.prev_uploads_cur, age.new_prev) == (1000 + 200, 40, 40)
    # The older catalog (v1, v2: published before the previous period) equals its own rows exactly.
    assert age.older_cur == 700 + 50 == 750                            # was 745 with the channel-total base
    assert age.older_prev == 1990 - 40
    assert not any("sums to" in n for n in rep.notes)                  # 5-view gap is within the 0.5 % tolerance


def test_views_by_age_falls_back_to_channel_total_without_reference(cfg):
    rep = _build(cfg, RevisedChannelTotal(format_supported=False), FakeYouTubeService(uploads=NEW_UPLOADS))
    assert rep.coverage_current.views_reference == 1985                # no format totals -> channel total
    assert rep.views_by_age.total_cur == 1985


def test_pipeline_api_call_budget(cfg):
    an, yt = FakeAnalyticsService(), FakeYouTubeService(uploads=NEW_UPLOADS)
    _build(cfg, an, yt)
    assert len(an.calls) <= 14  # totals 2 + formats 2 + filtered rows 6 + daily 1 + new-upload lookup
    assert sum(1 for r, _ in yt.calls if r == "playlistItems") == 1


# ===================================================== decision tree ====
def _pubs(cur, prev, complete=True, unclassified=0):
    mk = lambda d: PublicationStats(status=PUB_OK, complete=complete, published=dict(d), unclassified=unclassified)  # noqa: E731
    return mk(cur), mk(prev)


def _setup(report, cur_views, prev_views, pubs, age):
    report.current.views, report.previous.views = cur_views, prev_views
    report.publications_current, report.publications_previous = pubs
    report.views_by_age = age
    return report


def _texts(report):
    return " ".join(f"{r.title} {r.body}" for r in build_recommendations(report, limit=10))


def test_upload_spike_decline_does_not_blame_titles(report):
    age = ViewsByAge(total_cur=4500, total_prev=10000, new_cur=300, prev_uploads_cur=400, new_prev=6000, complete=True)
    _setup(report, 4500, 10000, _pubs({SHORTS: 2}, {SHORTS: 2}), age)
    assert views_trend_branch(report, -1) == BRANCH_UPLOAD_SPIKE
    text = _texts(report)
    assert "Views fell after last week's upload spike" in text and "60.0%" in text and "coincided with" in text
    assert "thumbnail" not in text.lower() and "title" not in text.lower().replace("titles", "")


def test_spike_then_fewer_uploads_reports_publishing_not_thumbnails(report):
    # Synthetic: ~69 % of last week's views came from its new uploads; fewer uploads followed.
    age = ViewsByAge(total_cur=2000, total_prev=8000, new_cur=800, prev_uploads_cur=400, new_prev=5500, complete=True)
    _setup(report, 2000, 8000, _pubs({SHORTS: 1, LIVE: 1}, {SHORTS: 3, LIVE: 1}), age)
    assert views_trend_branch(report, -1) == BRANCH_PUBLISHING
    rec = next(r for r in build_recommendations(report, limit=10) if r.title == "Publishing slowed down")
    assert "Shorts 1 vs. 3" in rec.body and "coincided with" in rec.body and "75.0%" in rec.body
    assert "New-upload views 5,500 → 800; older catalog 2,500 → 800." in rec.body
    assert "thumbnail" not in _texts(report).lower()


def test_performance_branch_requires_both_segments_down(report):
    age = ViewsByAge(total_cur=4000, total_prev=6000, new_cur=1000, prev_uploads_cur=200, new_prev=1500, complete=True)
    _setup(report, 4000, 6000, _pubs({SHORTS: 2, LONG_FORM: 1}, {SHORTS: 2, LONG_FORM: 1}), age)
    assert views_trend_branch(report, -1) == BRANCH_PERFORMANCE
    rec = next(r for r in build_recommendations(report, limit=10) if r.title == "Views fell across new and older videos")
    assert "thumbnails" in rec.body and "3 vs. 3 public uploads" in rec.body and "Retention by format" in rec.body


def test_unavailable_publications_give_neutral_text(report):
    report.publications_current = report.publications_previous = None
    assert views_trend_branch(report, -1) == BRANCH_UNAVAILABLE
    rec = next(r for r in build_recommendations(report, limit=10) if r.title == "Views declined")
    assert "cannot be separated" in rec.body and "17.5%" in rec.body and "thumbnail" not in rec.body


def test_incomplete_publications_are_treated_as_unavailable(report):
    age = ViewsByAge(4125, 5000, 100, 100, 3000, complete=False)
    _setup(report, 4125, 5000, _pubs({SHORTS: 1}, {SHORTS: 3}, complete=False), age)
    assert views_trend_branch(report, -1) == BRANCH_UNAVAILABLE


def test_unseparated_branch_when_no_pattern_matches(report):
    # Same uploads, new-upload views up but catalog down: no single pattern.
    age = ViewsByAge(total_cur=4000, total_prev=6000, new_cur=1600, prev_uploads_cur=100, new_prev=1500, complete=True)
    _setup(report, 4000, 6000, _pubs({SHORTS: 2}, {SHORTS: 2}), age)
    assert views_trend_branch(report, -1) == BRANCH_UNSEPARATED
    assert "No single pattern" in _texts(report)


def test_growth_driven_by_new_uploads(report):
    age = ViewsByAge(total_cur=10000, total_prev=4500, new_cur=6000, prev_uploads_cur=200, new_prev=600, complete=True)
    _setup(report, 10000, 4500, _pubs({SHORTS: 2}, {SHORTS: 2}), age)
    assert views_trend_branch(report, 1) == BRANCH_UPLOAD_SPIKE
    assert "Views rose with this week's new uploads" in _texts(report)


def test_growth_with_more_publishing(report):
    age = ViewsByAge(total_cur=8000, total_prev=5000, new_cur=2000, prev_uploads_cur=500, new_prev=1500, complete=True)
    _setup(report, 8000, 5000, _pubs({SHORTS: 4}, {SHORTS: 2}), age)
    assert views_trend_branch(report, 1) == BRANCH_PUBLISHING
    text = _texts(report)
    assert "Publishing picked up" in text and "Shorts 4 vs. 2" in text and "coincided with" in text


def test_growth_performance_across_new_and_older(report):
    age = ViewsByAge(total_cur=8000, total_prev=5000, new_cur=2000, prev_uploads_cur=300, new_prev=1500, complete=True)
    _setup(report, 8000, 5000, _pubs({SHORTS: 2}, {SHORTS: 2}), age)
    assert views_trend_branch(report, 1) == BRANCH_PERFORMANCE
    rec = next(r for r in build_recommendations(report, limit=10) if r.title == "Double down on what grew views")
    assert "most-viewed video" in rec.body and "driver" not in rec.body


@pytest.mark.parametrize("cur,prev", [(4500, 10000), (2000, 8000), (10000, 4500), (8000, 5000)])
def test_no_causal_language_in_any_branch(report, cur, prev):
    age = ViewsByAge(cur, prev, cur // 3, cur // 10, prev // 2, complete=True)
    _setup(report, cur, prev, _pubs({SHORTS: 1}, {SHORTS: 2}), age)
    text = _texts(report).lower()
    for phrase in (" caused", "because of", "due to", "top driver"):
        assert phrase not in text


# ============================================================ storage ====
def _v1_database(path, report):
    """A database exactly as v1.1.0 created it, with one report and its children."""
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    c.execute("INSERT INTO reports (channel_id, channel_title, subscriber_count, period_start, period_end, prev_start, "
              "prev_end, generated_at, notes, weakest_note, discord_sent_at) VALUES "
              "('UC_OLD','Old',10,'2026-10-29','2026-11-04','2026-10-22','2026-10-28','2026-11-02T09:00:00+01:00','[]',"
              "NULL,'2026-11-02T08:00:00+00:00')")
    c.execute("INSERT INTO totals VALUES (1,'current',2000,1200.0,80.0,20.0,10,5)")
    c.execute("INSERT INTO recommendations VALUES (1,0,'rules','Recover the drop in views','old text')")
    c.commit()
    c.close()


def _dump(path, tables=("reports", "totals", "format_stats", "video_stats", "recommendations", "daily_stats")):
    c = sqlite3.connect(path)
    out = {t: c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}
    c.close()
    return out


def test_migration_v1_to_v2_preserves_old_reports(tmp_path, report):
    db = tmp_path / "v1.db"
    _v1_database(db, report)
    before = _dump(db)
    with Storage(db) as s:
        assert s.schema_version() == SCHEMA_VERSION == 2
        data = s.dashboard_data()
    old = data["reports"][0]
    assert old["totals"]["current"]["views"] == 2000 and old["discord_sent_at"] == "2026-11-02T08:00:00+00:00"
    assert old["publications"] is None and old["coverage"] is None and old["views_by_age"] is None
    assert old["recommendations"][0]["title"] == "Recover the drop in views"
    assert _dump(db) == before  # no existing row changed


def test_migration_is_idempotent(tmp_path, report):
    db = tmp_path / "v1.db"
    _v1_database(db, report)
    for _ in range(3):
        with Storage(db) as s:
            assert s.schema_version() == 2
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM meta WHERE key='schema_version'").fetchone()[0] == 1
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    assert set(storage_mod.V2_TABLES) <= names


def test_new_report_next_to_migrated_one(tmp_path, report):
    db = tmp_path / "v1.db"
    _v1_database(db, report)
    report.publications_current, report.publications_previous = _pubs({SHORTS: 1}, {SHORTS: 3})
    report.coverage_current = Coverage(METHOD_BY_FORMAT, 10, 4125, 4125)
    with Storage(db) as s:
        s.save_report(report)
        data = s.dashboard_data()
    assert len(data["reports"]) == 2
    old, new = sorted(data["reports"], key=lambda r: r["id"])
    assert old["publications"] is None
    assert new["publications"]["current"]["shorts"] == 1 and new["publications"]["previous"]["shorts"] == 3
    assert new["coverage"]["current"]["complete"] == 1 and new["coverage"]["previous"] is None


def test_v2_fields_roundtrip_and_upsert_replaces(tmp_path, report):
    report.publications_current, report.publications_previous = _pubs({SHORTS: 1, LIVE: 1}, {SHORTS: 3}, unclassified=1)
    report.coverage_current = Coverage(METHOD_SINGLE_QUERY, 200, 990, 1000, capped=True, capped_min_views=1,
                                       fallback_reason=FALLBACK_FILTER_FAILED)
    report.coverage_previous = Coverage(METHOD_BY_FORMAT, 240, 8000, 8000)
    report.views_by_age = ViewsByAge(4125, 5000, 800, 300, 3000, complete=True)
    with Storage(tmp_path / "t.db") as s:
        s.save_report(report)
        s.save_report(report)
        data = s.dashboard_data()
        assert s.report_count() == 1
    r = data["reports"][0]
    cur = r["coverage"]["current"]
    assert (cur["method"], cur["views_rows"], cur["views_reference"], cur["capped"], cur["complete"]) == \
        (METHOD_SINGLE_QUERY, 990, 1000, 1, 0)
    assert r["publications"]["current"]["livestream"] == 1 and r["publications"]["current"]["unclassified"] == 1
    assert r["views_by_age"]["older_cur"] == 4125 - 800 - 300 and r["views_by_age"]["older_prev"] == 2000
    json.dumps(data)


def test_newer_schema_is_refused(tmp_path):
    db = tmp_path / "future.db"
    c = sqlite3.connect(db)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO meta VALUES ('schema_version', '99')")
    c.commit()
    c.close()
    with pytest.raises(sqlite3.DatabaseError):
        Storage(db)


# ============================================================ discord ====
def _complete_report(report):
    report.publications_current, report.publications_previous = _pubs({SHORTS: 1, LIVE: 1}, {SHORTS: 3, LIVE: 1})
    report.publications_current.private = 2
    report.coverage_current = Coverage(METHOD_BY_FORMAT, 8, 4125, 4125)
    report.coverage_previous = Coverage(METHOD_BY_FORMAT, 9, 5000, 5000)
    return report


def test_published_field_lists_counts_and_exclusions(report):
    fields = dn.build_embeds(_complete_report(report))[0]["fields"]
    pub = next(f for f in fields if f["name"] == "📅 Published")
    assert "Shorts **1** (prev 3)" in pub["value"] and "Long-form **0** (prev 0)" in pub["value"]
    assert "Live **1** (prev 1)" in pub["value"] and "not counted: 2 private" in pub["value"]
    assert pub["inline"] is False


def test_completeness_field_ok_when_everything_measured(report):
    fields = dn.build_embeds(_complete_report(report))[0]["fields"]
    assert fields[-1] == {"name": "Data completeness", "value": "✅ Complete", "inline": False}
    assert [f["name"] for f in fields[:7]] == ["Views", "Watch time", "Net subscribers", "Subscribers gained",
                                               "Subscribers lost", "Avg view duration", "Avg % viewed"]


def test_completeness_field_lists_issues(report):
    _complete_report(report)
    report.coverage_previous = Coverage(METHOD_SINGLE_QUERY, 200, 7990, 8000, capped=True, capped_min_views=1)
    report.publications_current.unclassified = 1
    report.missing_days = [date(2026, 10, 27)]
    value = dn.build_embeds(report)[0]["fields"][-1]["value"]
    assert "⚠ Per-video rows cover 99.9% of views (7,990 of 8,000; API cap 200) (prev. period)" in value
    assert "⚠ 1 new upload(s) not classified by format" in value and "⚠ Daily series missing 1 day(s)" in value
    assert "✅" not in value


def test_published_field_when_unavailable(report):
    pub = next(f for f in dn.build_embeds(report)[0]["fields"] if f["name"] == "📅 Published")
    assert pub["value"] == "_Publication data unavailable._"
    assert "Per-video coverage not measured" in dn.build_embeds(report)[0]["fields"][-1]["value"]


def test_new_fields_respect_discord_limits(report):
    _complete_report(report)
    report.publications_current.published = {f"TYPE{i}": 1 for i in range(200)}
    report.missing_days = [date(2026, 10, 22) + timedelta(days=i) for i in range(14)]
    report.top_videos[0].title = "x" * 1000
    embeds = dn.build_embeds(report)
    for e in embeds:
        assert len(e.get("fields", [])) <= 25 and all(len(f["value"]) <= 1024 for f in e.get("fields", []))
    for msg in dn.split_messages(embeds):
        assert len(msg) <= 10 and sum(dn.embed_chars(e) for e in msg) <= 6000


# =============================================================== demo ====
def test_demo_exercises_new_paths_without_external_calls(cfg, monkeypatch):
    from yt_report.demo import build_demo_reports

    def forbidden(*a, **k):
        raise AssertionError("demo mode must not call external services")

    monkeypatch.setattr("yt_report.auth.build_services", forbidden)
    monkeypatch.setattr("yt_report.discord_notifier.requests.post", forbidden)
    reps = build_demo_reports(cfg, weeks=3, today=TODAY)
    latest = reps[-1]
    assert all(r.coverage_current.method == METHOD_BY_FORMAT and r.coverage_current.complete for r in reps)
    assert all(r.publications_current.available and r.publications_current.complete for r in reps)
    assert latest.publications_current.total_public >= 1 and latest.publications_current.private == 1
    assert all(r.views_by_age is not None and r.views_by_age.complete for r in reps)
    assert analysis.completeness_issues(latest) == []
    for r in reps:
        assert sum(f.views for f in r.formats_current.values()) == r.current.views


def test_ai_payload_includes_publications_and_age(report):
    _complete_report(report)
    report.views_by_age = ViewsByAge(4125, 5000, 800, 300, 3000, complete=True)
    payload = metrics_payload(report)
    json.dumps(payload)
    assert payload["publications"]["current"]["public_by_format"] == {SHORTS: 1, LIVE: 1}
    assert payload["views_by_age"]["older_catalog_previous"] == 2000
