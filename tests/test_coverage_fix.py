"""Coverage fix (2026-11-08) — regression tests modelled on API behaviour observed on a live channel.

Behaviour reproduced here (all numbers are synthetic):
* format totals: Shorts 5,000 · long-form 400 · livestream 300 · posts 11 = 5,711
* Shorts filter query: 120 rows, 4,993 views (not capped; 7 views assigned to no video)
* long-form 60 rows / 400, livestream 90 rows / 300, posts 1 row / 11
* unfiltered single query: 199 rows although 270 videos had views (truncated below 200)
"""

from datetime import date

import pytest

from test_clients_and_pipeline import FakeRequest, FakeYouTubeService, _http_error, _resp
from yt_report import analysis
from yt_report import discord_notifier as dn
from yt_report.analytics_client import AnalyticsClient
from yt_report.config import load_config
from yt_report.models import (
    LIVE, LONG_FORM, METHOD_BY_FORMAT, METHOD_MIXED, METHOD_SINGLE_QUERY, SHORTS,
)
from yt_report.periods import Period
from yt_report.pipeline import build_report
from yt_report.youtube_client import YouTubeClient

P = Period(date(2026, 10, 18), date(2026, 10, 24))
POST_ID = "pst00000001"
VIDEO_HEADERS = ["video", "views", "engagedViews", "estimatedMinutesWatched", "averageViewDuration",
                 "averageViewPercentage", "subscribersGained"]


def _rows(prefix, n, total, pct=50.0):
    """n rows summing to ``total``: two large rows, the rest 1 view each (sorted by views desc)."""
    small = n - 2
    first = (total - small) * 2 // 3
    rows = [[f"{prefix}000", first], [f"{prefix}001", total - small - first]]
    rows += [[f"{prefix}{i:03d}", 1] for i in range(2, n)]
    return [[vid, v, max(0, v // 2), v, 30, pct, 0] for vid, v in rows]


SHORTS_ROWS = _rows("s", 120, 4993, 70.0)      # 7 views below the 5,000 total, no row cap
VOD_ROWS = _rows("v", 60, 400, 35.0)
LIVE_ROWS = _rows("l", 90, 300, 5.0)
POST_ROWS = [[POST_ID, 11, 5, 2, 10, 90.0, 0]]
TYPE_OF = {r[0]: "shorts" for r in SHORTS_ROWS} | {r[0]: "videoOnDemand" for r in VOD_ROWS} \
    | {r[0]: "liveStream" for r in LIVE_ROWS} | {POST_ID: "posts"}


class OctWeek:
    """Analytics fake reproducing the live 2026-10-18..24 responses."""

    def __init__(self, fail_filters=(), classify_ok=True, format_ok=True, posts_views=11, extra_rows=None):
        self.fail_filters, self.classify_ok, self.format_ok = set(fail_filters), classify_ok, format_ok
        self.posts_rows = [[POST_ID, posts_views, 6, 2, 10, 90.0, 0]] if posts_views else []
        self.posts_views = posts_views
        self.extra_rows = extra_rows or {}
        self.calls = []

    def reports(self):
        return self

    def by_filter(self, value):
        base = {"shorts": SHORTS_ROWS, "videoOnDemand": VOD_ROWS, "liveStream": LIVE_ROWS, "posts": self.posts_rows}
        return base[value] + self.extra_rows.get(value, [])

    def query(self, **kw):
        self.calls.append(kw)
        dims, flt = kw.get("dimensions"), kw.get("filters") or ""

        def run():
            if dims is None:
                return _resp(["views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
                              "subscribersGained", "subscribersLost"], [[5713, 2000, 30, 40.0, 120, 40]])
            if dims == "day":
                return _resp(["day", "views", "estimatedMinutesWatched", "subscribersGained", "subscribersLost"],
                             [[str(date(2026, 10, d)), 1400, 400, 20, 10] for d in range(11, 32)])
            if dims == "creatorContentType":
                if not self.format_ok:
                    raise _http_error()
                rows = [["shorts", 5000, 700, 15, 57.0], ["videoOnDemand", 400, 500, 150, 31.0],
                        ["liveStream", 300, 200, 110, 2.7]]
                if self.posts_views:
                    rows.append(["posts", self.posts_views, 2, 10, 90.0])
                return _resp(["creatorContentType", "views", "estimatedMinutesWatched", "averageViewDuration",
                              "averageViewPercentage"], rows)
            if dims == "video,creatorContentType":
                if not self.classify_ok:
                    raise _http_error()
                ids = flt[len("video=="):].split(",")
                return _resp(["video", "creatorContentType", "views"], [[i, TYPE_OF[i], 1] for i in ids if i in TYPE_OF])
            if dims == "video":
                if flt.startswith("creatorContentType=="):
                    value = flt[len("creatorContentType=="):]
                    if value in self.fail_filters:
                        raise _http_error()
                    return _resp(VIDEO_HEADERS, self.by_filter(value))
                # Unfiltered: truncated to 199 rows, like the live API.
                everything = sorted(SHORTS_ROWS + VOD_ROWS + LIVE_ROWS + self.posts_rows, key=lambda r: -r[1])
                return _resp(VIDEO_HEADERS, everything[:199])
            raise AssertionError(dims)

        return FakeRequest(run)


def _fetch(svc):
    an = AnalyticsClient(svc)
    totals = an.format_totals(P) if svc.format_ok else None
    return an.fetch_videos(P, totals, 5713)


# ================================================== unassigned API views ===
def test_shorts_shortfall_below_row_cap_keeps_primary_result():
    svc = OctWeek()
    videos, typed, cov = _fetch(svc)
    assert typed and cov.method == METHOD_BY_FORMAT and cov.fallback_reason is None
    assert not any(c.get("dimensions") == "video" and not c.get("filters") for c in svc.calls)  # no single query
    assert sum(v.views for v in videos) == 4993 + 400 + 300 == 5693
    assert cov.views_rows == 5693 + 11 == 5704 and cov.views_reference == 5711
    assert cov.unassigned_views == 7 and cov.truncated_views == 0 and not cov.capped
    assert cov.missing_views == 7 and not cov.complete                          # stays INCOMPLETE
    assert analysis.coverage_pct_text(cov) == "99.9%"
    assert analysis.coverage_causes(cov) == ["7 views not assigned to any video by the API"]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for k in ("AI_RECOMMENDATIONS", "ANTHROPIC_API_KEY", "YT_TOKEN_JSON", "DISCORD_WEBHOOK_URL", "REPORT_WEEK_END",
              "REPORT_TIMEZONE"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("DATA_LAG_DAYS=3\nLOG_DIR=\nMIN_VIEWS_WEAKEST=1\nMIN_VIDEOS_PER_FORMAT=2\n")
    return load_config(env)


def _report(cfg, svc):
    return build_report(cfg, YouTubeClient(FakeYouTubeService(uploads={})), AnalyticsClient(svc),
                        today=date(2026, 11, 3))  # Saturday-anchored: current Oct 25–31, previous Oct 18–24


def test_pipeline_reports_october_18_24_coverage_exactly(cfg):
    rep = _report(cfg, OctWeek())
    assert rep.previous_period == P
    cov = rep.coverage_previous
    assert (cov.views_rows, cov.views_reference, cov.unassigned_views, cov.complete) == (5704, 5711, 7, False)
    note = next(n for n in rep.notes if "previous period account for" in n)
    assert note.startswith("Per-video rows for the previous period account for 5,704 of 5,711 views (99.9%): "
                           "7 views not assigned to any video by the API.")
    assert "API limit" not in note and "API cap" not in " ".join(rep.notes)  # no false cap claim
    assert any("includes 11 views of posts" in n for n in rep.notes)
    issues = analysis.completeness_issues(rep)
    assert "Per-video rows cover 99.9% of views (5,704 of 5,711; 7 unassigned by API) (prev. period)" in issues
    assert "✅" not in dn.build_embeds(rep)[0]["fields"][-1]["value"]


# ============================================================== posts ===
def test_posts_count_for_coverage_but_are_never_videos(cfg):
    svc = OctWeek()
    videos, _, cov = _fetch(svc)
    assert cov.other_type_views == {"POSTS": 11}
    assert POST_ID not in {v.video_id for v in videos}
    assert any(c.get("filters") == "creatorContentType==posts" for c in svc.calls)  # raw API value used


def test_large_post_is_not_ranked_as_top_or_weakest(cfg):
    rep = _report(cfg, OctWeek(posts_views=50_000))  # the post out-views every video
    assert POST_ID not in {v.video_id for v in rep.top_videos}
    assert rep.weakest is None or rep.weakest.video.video_id != POST_ID
    assert all(v.content_type in (SHORTS, LONG_FORM, LIVE) for v in rep.videos)
    assert rep.coverage_current.other_type_views == {"POSTS": 50_000}


# ============================================================ excess ===
def test_rows_above_format_total_are_flagged_not_replaced():
    extra = {"liveStream": [["l999", 40, 20, 40, 30, 5.0, 0]]}  # live rows now 340 > 300
    videos, _, cov = _fetch(OctWeek(extra_rows=extra))
    assert cov.method == METHOD_BY_FORMAT and cov.excess_views == 40
    assert cov.views_rows == 5704                       # live counted only up to its total
    assert not cov.complete
    assert "l999" in {v.video_id for v in videos}       # primary rows kept, not replaced by the fallback


# ===================================================== single query ===
def test_single_query_with_199_rows_is_suspected_truncation():
    svc = OctWeek(format_ok=False)
    videos, typed, cov = _fetch(svc)
    assert cov.method == METHOD_SINGLE_QUERY and cov.rows == 199
    assert cov.truncation_suspected and not cov.capped and not cov.complete
    causes = analysis.coverage_causes(cov)
    assert causes == ["single query returned 199 rows — possibly truncated by the API's 200-row limit "
                      "(suspected, not confirmed)"]
    assert not any("API limit of 200 rows" in c and "suspected" not in c for c in causes)


def test_short_single_query_is_not_claimed_as_capped():
    class Few(OctWeek):
        def query(self, **kw):
            if kw.get("dimensions") == "video" and not kw.get("filters"):
                self.calls.append(kw)
                rows = sorted(SHORTS_ROWS + VOD_ROWS, key=lambda r: -r[1])[:120]
                return FakeRequest(lambda: _resp(VIDEO_HEADERS, rows))
            return super().query(**kw)

    _, _, cov = _fetch(Few(format_ok=False))
    assert not cov.capped and not cov.truncation_suspected and not cov.complete
    assert analysis.coverage_causes(cov) == ["cause not identified"]   # incomplete, but no invented cause


# ================================================ partial format failure ===
def test_one_failed_format_keeps_successful_formats():
    svc = OctWeek(fail_filters={"liveStream"})
    videos, typed, cov = _fetch(svc)
    assert cov.method == METHOD_MIXED and LIVE in cov.failed_types and "LIVE_STREAM" in cov.fallback_reason
    by_id = {v.video_id: v for v in videos}
    assert all(by_id[r[0]].views == r[1] for r in SHORTS_ROWS + VOD_ROWS)          # primary rows preserved
    live_from_single = [v for v in videos if v.content_type == LIVE]
    assert live_from_single and all(v.video_id.startswith("l") for v in live_from_single)
    got_live = sum(v.views for v in live_from_single)
    assert cov.unexplained_views == 300 - got_live > 0 and cov.truncation_suspected  # 199-row single query
    assert cov.unassigned_views == 7
    assert cov.views_rows == 4993 + 400 + got_live + 11 and not cov.complete


def test_failed_format_without_classification_is_reported_uncovered():
    _, _, cov = _fetch(OctWeek(fail_filters={"liveStream"}, classify_ok=False))
    assert cov.method == METHOD_BY_FORMAT and "not covered" in cov.fallback_reason
    assert cov.views_rows == 4993 + 400 + 11 and cov.missing_views == 7 + 300
    assert "query failed for Livestream" in analysis.coverage_causes(cov)


def test_failed_posts_query_is_reported_and_not_hidden():
    _, _, cov = _fetch(OctWeek(fail_filters={"posts"}))
    assert cov.failed_types == ["POSTS"] and cov.method == METHOD_BY_FORMAT
    assert cov.missing_views == 7 + 11 and not cov.complete
    assert "query failed for posts" in analysis.coverage_causes(cov)


# ======================================================= no duplicates ===
def test_video_in_two_format_queries_is_counted_once():
    extra = {"videoOnDemand": [["s000", 5, 2, 5, 30, 40.0, 0]]}   # s000 also reported as long-form
    videos, _, cov = _fetch(OctWeek(extra_rows=extra))
    ids = [v.video_id for v in videos]
    assert len(ids) == len(set(ids)) == 120 + 60 + 90
    s000 = next(v for v in videos if v.video_id == "s000")
    assert s000.views == SHORTS_ROWS[0][1] + 5 and s000.content_type is None
    assert cov.excess_views == 5 and cov.views_rows == 5704   # per-type accounting, never double counted


def test_mixed_path_does_not_add_a_video_twice():
    # liveStream filter fails; the single query also contains Shorts/long-form rows already present.
    videos, _, _ = _fetch(OctWeek(fail_filters={"liveStream"}))
    ids = [v.video_id for v in videos]
    assert len(ids) == len(set(ids))
    assert sum(1 for v in videos if v.video_id == "s000") == 1


# ===================================================== confirmed cap ===
def test_capped_format_query_shortfall_is_truncation_not_unassigned():
    class Capped(OctWeek):
        def by_filter(self, value):
            if value == "shorts":
                return [[f"c{i:03d}", 20, 10, 20, 30, 70.0, 0] for i in range(200)]  # 4,000 of 5,000
            return super().by_filter(value)

    _, _, cov = _fetch(Capped())
    assert cov.capped and cov.truncated_views == 1000 and cov.unassigned_views == 0
    assert cov.capped_min_views == 20
    assert analysis.coverage_causes(cov) == ["1,000 views missing from a query that hit the API limit of 200 rows"]
    notes = analysis.completeness_notes(cov, None, None, None, [], True, weakest_min_views=15)
    assert any("hit the API limit of 200 rows while its smallest row still had 20 views" in n for n in notes)
