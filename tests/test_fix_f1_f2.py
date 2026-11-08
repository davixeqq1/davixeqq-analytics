"""Audit fixes F1 (posts in the full single-query fallback) and F2 (cause attribution with
ambiguously classified videos in the mixed path). Builds on the synthetic coverage scenario in test_coverage_fix.py."""

from datetime import date

from test_clients_and_pipeline import FakeRequest, FakeYouTubeService, _resp
from test_coverage_fix import LIVE_ROWS, POST_ID, SHORTS_ROWS, TYPE_OF, VOD_ROWS, VIDEO_HEADERS, OctWeek, _fetch
from yt_report import analysis
from yt_report.analytics_client import AnalyticsClient
from yt_report.config import load_config
from yt_report.models import LIVE, LONG_FORM, METHOD_BY_FORMAT, METHOD_MIXED, METHOD_SINGLE_QUERY, SHORTS
from yt_report.pipeline import build_report
from yt_report.storage import Storage
from yt_report.youtube_client import YouTubeClient

VIDEO_TYPES = {SHORTS, LONG_FORM, LIVE}


# ===================================================================== F1 ===
def test_f1_post_with_50000_views_never_enters_fallback_videos():
    videos, typed, cov = _fetch(OctWeek(format_ok=False, posts_views=50_000))
    assert typed and cov.method == METHOD_SINGLE_QUERY
    assert POST_ID not in {v.video_id for v in videos}
    assert all(v.content_type in VIDEO_TYPES for v in videos if v.views > 0)
    assert cov.other_type_views == {"POSTS": 50_000}
    # Coverage still counts every returned row, the post included.
    assert cov.views_rows == sum(v.views for v in videos) + 50_000
    assert cov.rows == 199                                    # rows returned by the API (cap logic unchanged)
    assert analysis.top_videos(videos, 3)[0].video_id == "s000"


def test_f1_fallback_after_all_format_queries_fail_also_excludes_posts():
    svc = OctWeek(fail_filters={"shorts", "videoOnDemand", "liveStream"}, posts_views=50_000)
    videos, _, cov = _fetch(svc)
    assert cov.method == METHOD_SINGLE_QUERY and cov.fallback_reason == "format filter rejected by the API"
    assert POST_ID not in {v.video_id for v in videos} and cov.other_type_views == {"POSTS": 50_000}


def test_f1_legitimate_fallback_rows_are_preserved():
    svc = OctWeek(format_ok=False, posts_views=50_000)
    videos, _, _ = _fetch(svc)
    everything = sorted(SHORTS_ROWS + VOD_ROWS + LIVE_ROWS + svc.posts_rows, key=lambda r: -r[1])[:199]
    expected = {r[0]: r[1] for r in everything if r[0] != POST_ID}
    assert {v.video_id: v.views for v in videos} == expected
    assert {v.video_id: v.content_type for v in videos if v.video_id in ("s000", "v000", "l000")} == \
        {"s000": SHORTS, "v000": LONG_FORM, "l000": LIVE}


def _cfg(tmp_path, monkeypatch):
    for k in ("AI_RECOMMENDATIONS", "ANTHROPIC_API_KEY", "YT_TOKEN_JSON", "DISCORD_WEBHOOK_URL", "REPORT_WEEK_END",
              "REPORT_TIMEZONE"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("DATA_LAG_DAYS=3\nLOG_DIR=\nMIN_VIEWS_WEAKEST=1\nMIN_VIDEOS_PER_FORMAT=2\n")
    return load_config(env)


def test_f1_post_absent_from_top_weakest_and_stored_video_stats(tmp_path, monkeypatch):
    rep = build_report(_cfg(tmp_path, monkeypatch), YouTubeClient(FakeYouTubeService(uploads={})),
                       AnalyticsClient(OctWeek(format_ok=False, posts_views=50_000)), today=date(2026, 11, 3))
    assert rep.coverage_current.method == METHOD_SINGLE_QUERY
    assert POST_ID not in {v.video_id for v in rep.videos}
    assert POST_ID not in {v.video_id for v in rep.top_videos}
    assert rep.weakest is None or rep.weakest.video.video_id != POST_ID
    with Storage(":memory:") as db:
        db.save_report(rep)
        stored = db.dashboard_data()["reports"][0]["videos"]
    assert stored and POST_ID not in {v["video_id"] for v in stored}
    assert any("includes 50,000 views of posts" in n for n in rep.notes)


def test_f1_primary_path_unchanged():
    videos, typed, cov = _fetch(OctWeek())
    assert cov.method == METHOD_BY_FORMAT and cov.fallback_reason is None
    assert (cov.views_rows, cov.views_reference, cov.unassigned_views) == (5704, 5711, 7)
    assert cov.other_type_views == {"POSTS": 11} and not cov.complete
    assert len(videos) == 120 + 60 + 90 and POST_ID not in {v.video_id for v in videos}
    assert analysis.coverage_pct_text(cov) == "99.9%"


# ===================================================================== F2 ===
class Ambiguous(OctWeek):
    """The format lookup reports ``ambiguous_ids`` under two content types (live + shorts)."""

    def __init__(self, ambiguous_ids=("l000",), single_rows=199, **kw):
        super().__init__(**kw)
        self.ambiguous_ids, self.single_rows = set(ambiguous_ids), single_rows

    def query(self, **kw):
        dims, flt = kw.get("dimensions"), kw.get("filters") or ""
        if dims == "video,creatorContentType":
            self.calls.append(kw)
            rows = []
            for i in flt[len("video=="):].split(","):
                if i in self.ambiguous_ids:
                    rows += [[i, "liveStream", 1], [i, "shorts", 1]]
                elif i in TYPE_OF:
                    rows.append([i, TYPE_OF[i], 1])
            return FakeRequest(lambda: _resp(["video", "creatorContentType", "views"], rows))
        if dims == "video" and not flt and self.single_rows != 199:
            self.calls.append(kw)
            everything = sorted(SHORTS_ROWS + VOD_ROWS + LIVE_ROWS + self.posts_rows, key=lambda r: -r[1])
            return FakeRequest(lambda: _resp(VIDEO_HEADERS, everything[:self.single_rows]))
        return super().query(**kw)


def _single_live(svc):
    """Unambiguous live rows of the single query (what the mixed path may add)."""
    everything = sorted(SHORTS_ROWS + VOD_ROWS + LIVE_ROWS + svc.posts_rows, key=lambda r: -r[1])[:svc.single_rows]
    return {r[0]: r[1] for r in everything if TYPE_OF.get(r[0]) == "liveStream" and r[0] not in svc.ambiguous_ids}


def test_f2_ambiguous_video_in_mixed_path_gives_unidentified_cause():
    svc = Ambiguous(fail_filters={"liveStream"})
    videos, _, cov = _fetch(svc)
    live = _single_live(svc)
    assert cov.method == METHOD_MIXED
    assert cov.unidentified_views == 300 - sum(live.values()) > 0
    assert cov.unexplained_views == 0 and not cov.truncation_suspected            # not blamed on truncation
    assert cov.unassigned_views == 7                                               # Shorts attribution unchanged
    causes = analysis.coverage_causes(cov)
    assert any(c.startswith(f"{cov.unidentified_views:,} views: cause not identified") for c in causes)
    assert not any("suspected" in c or "truncated" in c for c in causes)
    assert "l000" not in {v.video_id for v in videos}                              # ambiguous row not guessed


def test_f2_coverage_stays_exact_with_unknown_cause():
    svc = Ambiguous(fail_filters={"liveStream"})
    videos, _, cov = _fetch(svc)
    live_views = sum(_single_live(svc).values())
    assert cov.views_rows == 4993 + 400 + live_views + 11
    assert cov.missing_views == 7 + (300 - live_views) and not cov.complete
    assert cov.views_reference == 5711
    ids = [v.video_id for v in videos]
    assert len(ids) == len(set(ids))                                               # deduplicated


def test_f2_video_in_two_successful_formats_does_not_make_cause_unknown():
    # s000 is ambiguous (two types) but already returned by the successful Shorts query:
    # it cannot belong to the failed livestream rows, so the existing attribution applies.
    svc = Ambiguous(ambiguous_ids=("s000",), fail_filters={"liveStream"})
    videos, _, cov = _fetch(svc)
    assert cov.unidentified_views == 0
    assert cov.unexplained_views > 0 and cov.truncation_suspected                  # suspected truncation unchanged
    assert sum(1 for v in videos if v.video_id == "s000") == 1


def test_f2_mixed_path_without_ambiguity_is_unchanged():
    videos, _, cov = _fetch(OctWeek(fail_filters={"liveStream"}))
    assert cov.method == METHOD_MIXED and cov.unidentified_views == 0
    assert cov.unexplained_views > 0 and cov.truncation_suspected and cov.unassigned_views == 7


def test_f2_confirmed_cap_keeps_truncation_attribution():
    svc = Ambiguous(fail_filters={"liveStream"}, single_rows=200)
    _, _, cov = _fetch(svc)
    assert cov.capped and cov.truncated_views == 300 - sum(_single_live(svc).values())
    assert cov.unidentified_views == 0 and cov.unexplained_views == 0


def test_f2_completeness_issue_reports_cause_not_identified(tmp_path, monkeypatch):
    rep = build_report(_cfg(tmp_path, monkeypatch), YouTubeClient(FakeYouTubeService(uploads={})),
                       AnalyticsClient(Ambiguous(fail_filters={"liveStream"})), today=date(2026, 11, 3))
    cov = rep.coverage_previous
    issue = next(i for i in analysis.completeness_issues(rep) if i.endswith("(prev. period)"))
    assert f"{cov.unidentified_views:,} cause not identified" in issue and "truncation suspected" not in issue
    note = next(n for n in rep.notes if "previous period account for" in n)
    assert "cause not identified" in note and "possibly truncated" not in note
