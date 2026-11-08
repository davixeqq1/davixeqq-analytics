"""API client parsing and full pipeline with a fake Google service (no network)."""

from datetime import date, timedelta

import httplib2
import pytest
from googleapiclient.errors import HttpError

from yt_report.analytics_client import AnalyticsClient
from yt_report.config import load_config
from yt_report.logging_utils import redact
from yt_report.models import LIVE, LONG_FORM, SHORTS, SOURCE_API, SOURCE_DERIVED, UNSPECIFIED, normalize_content_type
from yt_report.pipeline import build_report
from yt_report.youtube_client import YouTubeClient


def _resp(headers, rows):
    return {"columnHeaders": [{"name": h} for h in headers], "rows": rows}


def _http_error(status=400):
    return HttpError(httplib2.Response({"status": status}), b'{"error": {"message": "bad"}}')


class FakeRequest:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class FakeAnalyticsService:
    """Mimics the live API behaviour verified on 2026-11-07:
    * creatorContentType values are camelCase,
    * video,creatorContentType WITHOUT a video filter -> HTTP 400,
    * the same dimensions WITH a video==... filter -> one row per video that had views.
    * dimensions=video with filters=creatorContentType==<type> -> only that format's rows
      (verified 2026-11-07; undocumented for this report).
    Numbers are synthetic.
    """

    VIDEO_ROWS = [  # video, views, engagedViews, minutes, avd, avp, subs
        ["s1", 400, 180, 200, 30, 90.0, 3], ["v1", 300, 120, 2000, 400, 42.0, 5], ["l1", 120, 40, 400, 200, 5.0, 0],
        ["s2", 100, 45, 40, 24, 60.0, 0], ["v2", 80, 30, 500, 375, 30.0, 1], ["z0", 0, 0, 0, 0, 0.0, 0],
    ]
    TYPES = {"s1": "shorts", "s2": "shorts", "v1": "videoOnDemand", "v2": "videoOnDemand", "l1": "liveStream"}

    def __init__(self, format_supported=True, video_type_supported=True, format_rows=None, format_filter_supported=True,
                 last_data_day=None):
        self.format_supported = format_supported
        self.video_type_supported = video_type_supported
        self.format_rows = format_rows
        self.format_filter_supported = format_filter_supported
        self.last_data_day = last_data_day  # "YYYY-MM-DD": later days are "not processed yet" (no daily rows)
        self.calls = []

    def reports(self):
        return self

    def query(self, **kw):
        self.calls.append(kw)
        dims = kw.get("dimensions")
        current = kw["startDate"] >= "2026-10-31"

        def run():
            if dims is None:
                v = 1000 if current else 800
                return _resp(["views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
                              "subscribersGained", "subscribersLost"], [[v, 3140, 188, 40.1, 12, 2]])
            if dims == "day":
                # Like the live API: one row per processed day, none for days not processed yet.
                d, end, rows = date.fromisoformat(kw["startDate"]), date.fromisoformat(kw["endDate"]), []
                while d <= end and (self.last_data_day is None or d.isoformat() <= self.last_data_day):
                    rows.append([d.isoformat(), 100, 300, 1, 0])
                    d += timedelta(days=1)
                return _resp(["day", "views", "estimatedMinutesWatched", "subscribersGained", "subscribersLost"], rows)
            if dims == "creatorContentType":
                if not self.format_supported:
                    raise _http_error()
                rows = self.format_rows if self.format_rows is not None else [
                    ["videoOnDemand", 380, 2500, 395, 39.5], ["liveStream", 120, 400, 200, 5.0],
                    ["shorts", 500, 240, 29, 84.0], ["creatorContentTypeUnspecified", 0, 0, 0, 0.0]]
                return _resp(["creatorContentType", "views", "estimatedMinutesWatched", "averageViewDuration",
                              "averageViewPercentage"], rows)
            if dims == "video,creatorContentType":
                if not self.video_type_supported or not kw.get("filters", "").startswith("video=="):
                    raise _http_error()  # the real API rejects this combination without a video filter
                ids = kw["filters"][len("video=="):].split(",")
                views = {r[0]: r[1] for r in self.VIDEO_ROWS}
                return _resp(["video", "creatorContentType", "views"],
                             [[i, self.TYPES[i], views[i]] for i in ids if views.get(i) and i in self.TYPES])
            if dims == "video":
                assert kw["metrics"].split(",")[:2] == ["views", "engagedViews"]
                rows = self.VIDEO_ROWS
                flt = kw.get("filters") or ""
                if flt.startswith("creatorContentType=="):
                    if not self.format_filter_supported:
                        raise _http_error()
                    wanted = flt[len("creatorContentType=="):]
                    rows = [r for r in rows if self.TYPES.get(r[0]) == wanted]
                return _resp(["video", "views", "engagedViews", "estimatedMinutesWatched", "averageViewDuration",
                              "averageViewPercentage", "subscribersGained"], rows)
            raise AssertionError(dims)

        return FakeRequest(run)


class FakeYouTubeService:
    """Data API fake. ``uploads`` maps video id -> extra item fields (publishedAt, privacy, live details)."""

    DEFAULT_UPLOADS = {i: {} for i in ("s1", "v1", "l1", "s2", "z0")}  # v2 is deleted: not in the playlist

    def __init__(self, uploads=None, page_size=50, playlist_error=False, with_playlist=True):
        self.uploads = dict(self.DEFAULT_UPLOADS if uploads is None else uploads)
        self.page_size = page_size
        self.playlist_error = playlist_error
        self.with_playlist = with_playlist
        self.calls = []

    def channels(self):
        return _Lister(self, "channels")

    def videos(self):
        return _Lister(self, "videos")

    def playlistItems(self):  # noqa: N802 - Google client naming
        return _Lister(self, "playlistItems")

    def handle(self, resource, kw):
        self.calls.append((resource, kw))
        if resource == "channels":
            item = {"id": "UC1", "snippet": {"title": "Example Channel"},
                    "statistics": {"subscriberCount": "321", "hiddenSubscriberCount": False}}
            if self.with_playlist:
                item["contentDetails"] = {"relatedPlaylists": {"uploads": "UU1"}}
            return {"items": [item]}
        if resource == "playlistItems":
            if self.playlist_error:
                raise _http_error(404)
            ids = list(self.uploads)
            start = int(kw.get("pageToken") or 0)
            page = ids[start:start + self.page_size]
            resp = {"items": [{"contentDetails": {"videoId": i}} for i in page]}
            if start + self.page_size < len(ids):
                resp["nextPageToken"] = str(start + self.page_size)
            return resp
        ids = kw["id"].split(",")
        items = []
        for i in ids:
            if i == "v2":
                continue
            extra = self.uploads.get(i, {})
            item = {"id": i,
                    "snippet": {"title": f"T-{i}", "publishedAt": extra.get("publishedAt", "2026-10-01T00:00:00Z"),
                                "liveBroadcastContent": extra.get("liveBroadcastContent", "none"),
                                "thumbnails": {"high": {"url": f"https://i.ytimg.com/{i}.jpg"}}},
                    "status": {"privacyStatus": extra.get("privacy", "public")}}
            if "actualStartTime" in extra:
                item["liveStreamingDetails"] = {"actualStartTime": extra["actualStartTime"]}
            items.append(item)
        return {"items": items}


class _Lister:
    def __init__(self, svc, resource):
        self.svc, self.resource = svc, resource

    def list(self, **kw):
        return FakeRequest(lambda: self.svc.handle(self.resource, kw))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for k in ("AI_RECOMMENDATIONS", "ANTHROPIC_API_KEY", "YT_TOKEN_JSON", "DISCORD_WEBHOOK_URL", "REPORT_WEEK_END"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    # Rolling window: these tests exercise metrics, not period anchoring (see test_period_anchor.py).
    env.write_text("DATA_LAG_DAYS=3\nMIN_VIEWS_WEAKEST=10\nMIN_VIDEOS_PER_FORMAT=2\nREPORT_WEEK_END=none\n")
    return load_config(env)


def _build(cfg, svc):
    return build_report(cfg, YouTubeClient(FakeYouTubeService()), AnalyticsClient(svc), today=date(2026, 11, 9))


def test_pipeline_happy_path(cfg):
    svc = FakeAnalyticsService()
    rep = _build(cfg, svc)
    assert rep.period.start == date(2026, 10, 31) and rep.period.end == date(2026, 11, 6)
    assert rep.current.views == 1000 and rep.previous.views == 800
    assert rep.current.net_subs == 10
    assert [v.video_id for v in rep.top_videos] == ["s1", "v1", "l1"]
    assert rep.top_videos[0].url == "https://www.youtube.com/shorts/s1"
    assert rep.top_videos[0].title == "T-s1"
    assert 2 <= len(rep.recommendations) <= 3
    # Top videos use the compatible query; the format lookup always carries a video filter.
    top_q = next(c for c in svc.calls if c.get("dimensions") == "video")
    assert top_q["sort"] == "-views" and top_q["maxResults"] <= 200 and top_q["ids"] == "channel==MINE"
    for c in svc.calls:
        if c.get("dimensions") == "video,creatorContentType":
            assert c["filters"].startswith("video==") and "z0" not in c["filters"]  # zero-view videos skipped


def test_private_video_title_placeholder(cfg):
    rep = _build(cfg, FakeAnalyticsService())
    v2 = next(v for v in rep.videos if v.video_id == "v2")
    assert v2.title == "(private or deleted video)"


def test_three_formats_mapped_from_camelcase(cfg):
    rep = _build(cfg, FakeAnalyticsService())
    f = rep.formats_current
    assert (f[SHORTS].views, f[LONG_FORM].views, f[LIVE].views) == (500, 380, 120)
    assert f[SHORTS].minutes_watched == 240 and f[SHORTS].avg_view_duration_s == 29 and f[SHORTS].avg_view_pct == 84.0
    assert f[LONG_FORM].avg_view_duration_s == 395 and f[LONG_FORM].avg_view_pct == 39.5
    assert f[LIVE].watch_hours == pytest.approx(400 / 60) and f[LIVE].avg_view_pct == 5.0
    assert all(f[ct].source == SOURCE_API for ct in (SHORTS, LONG_FORM, LIVE))
    assert "shorts" not in f and "videoOnDemand" not in f and "liveStream" not in f  # no raw keys leak
    assert f[UNSPECIFIED].views == 0


def test_livestream_not_counted_as_long_form(cfg):
    rep = _build(cfg, FakeAnalyticsService())
    assert rep.formats_current[LONG_FORM].views == 380  # 500 would mean live (120) was merged in
    live = next(v for v in rep.videos if v.video_id == "l1")
    assert live.content_type == LIVE


def test_format_views_sum_to_channel_total(cfg):
    rep = _build(cfg, FakeAnalyticsService())
    assert sum(f.views for f in rep.formats_current.values()) == rep.current.views
    assert not any("sums to" in n for n in rep.notes)


def test_format_sum_mismatch_is_reported(cfg):
    rows = [["videoOnDemand", 300, 2500, 395, 39.5], ["shorts", 500, 240, 29, 84.0]]
    rep = _build(cfg, FakeAnalyticsService(format_rows=rows))
    assert any("sums to 800 views but the channel total is 1,000" in n for n in rep.notes)


def test_missing_format_is_zero_views_but_averages_unavailable(cfg):
    rep = _build(cfg, FakeAnalyticsService(format_rows=[["shorts", 1000, 300, 25, 80.0]]))
    live = rep.formats_current[LIVE]
    assert live.views == 0 and live.avg_view_pct is None and live.avg_view_duration_s is None


def test_per_video_formats_from_filtered_query(cfg):
    # Primary path: the format comes from the creatorContentType-filtered query itself.
    rep = _build(cfg, FakeAnalyticsService())
    types = {v.video_id: v.content_type for v in rep.videos}
    assert types == {"s1": SHORTS, "s2": SHORTS, "v1": LONG_FORM, "v2": LONG_FORM, "l1": LIVE}
    # Fallback path: single query + video==... lookup; a zero-view row stays unclassified.
    rep = _build(cfg, FakeAnalyticsService(format_filter_supported=False))
    types = {v.video_id: v.content_type for v in rep.videos}
    assert types == {"s1": SHORTS, "s2": SHORTS, "v1": LONG_FORM, "v2": LONG_FORM, "l1": LIVE, "z0": None}


def test_weakest_compares_within_same_format(cfg):
    rep = _build(cfg, FakeAnalyticsService())
    # Shorts: s2 60 / median 75 = 0.80 ; long-form: v2 30 / median 36 = 0.83 ; live: 1 video -> skipped
    assert rep.weakest.video.video_id == "s2"
    assert rep.weakest.format_median_pct == pytest.approx(75.0)
    assert rep.weakest.peers == 2


def test_pipeline_format_fallback_is_marked_derived(cfg):
    rep = _build(cfg, FakeAnalyticsService(format_supported=False))
    f = rep.formats_current
    assert f[SHORTS].source == SOURCE_DERIVED
    assert (f[SHORTS].views, f[LONG_FORM].views, f[LIVE].views) == (500, 380, 120)
    assert any("derived" in n for n in rep.notes)
    # Fallback AVD: engagedViews-weighted mean of per-video AVD; avg % viewed unavailable.
    assert f[SHORTS].avg_view_duration_s == pytest.approx((30 * 180 + 24 * 45) / 225)   # 28.8 s
    assert f[LONG_FORM].avg_view_duration_s == pytest.approx((400 * 120 + 375 * 30) / 150)  # 395 s
    assert f[LIVE].avg_view_duration_s == pytest.approx(200)
    assert all(f[ct].avg_view_pct is None for ct in (SHORTS, LONG_FORM, LIVE))
    # Additive metrics still add up: derived views/minutes == per-video sums
    assert sum(f[ct].views for ct in (SHORTS, LONG_FORM, LIVE)) == 1000
    assert sum(f[ct].minutes_watched for ct in (SHORTS, LONG_FORM, LIVE)) == 3140


def test_api_path_unchanged_by_fallback_fix(cfg):
    f = _build(cfg, FakeAnalyticsService()).formats_current
    # API values are passed through untouched (no re-weighting in the production path).
    assert (f[SHORTS].avg_view_duration_s, f[SHORTS].avg_view_pct) == (29, 84.0)
    assert (f[LONG_FORM].avg_view_duration_s, f[LONG_FORM].avg_view_pct) == (395, 39.5)
    assert (f[LIVE].avg_view_duration_s, f[LIVE].avg_view_pct) == (200, 5.0)


def test_video_rows_carry_engaged_views(cfg):
    rep = _build(cfg, FakeAnalyticsService())
    assert {v.video_id: v.engaged_views for v in rep.videos}["s1"] == 180


def test_pipeline_without_content_type(cfg):
    rep = _build(cfg, FakeAnalyticsService(format_supported=False, video_type_supported=False))
    assert rep.weakest is None and "unavailable" in rep.weakest_note
    assert all(f.source == "unavailable" for f in rep.formats_current.values())
    assert all(f.views is None for f in rep.formats_current.values())  # unavailable, not zero
    assert all(v.content_type is None for v in rep.videos)
    assert [v.video_id for v in rep.top_videos] == ["s1", "v1", "l1"]  # top videos still work


def test_ambiguous_video_type_is_not_guessed():
    class Multi(FakeAnalyticsService):
        def query(self, **kw):
            return FakeRequest(lambda: _resp(["video", "creatorContentType", "views"],
                                             [["x", "videoOnDemand", 5], ["x", "liveStream", 7], ["y", "shorts", 3]]))

    from yt_report.periods import Period
    types = AnalyticsClient(Multi()).video_content_types(Period(date(2026, 1, 1), date(2026, 1, 7)), ["x", "y"])
    assert types == {"x": None, "y": SHORTS}


@pytest.mark.parametrize("raw,expected", [
    ("shorts", SHORTS), ("videoOnDemand", LONG_FORM), ("liveStream", LIVE), ("story", "STORY"),
    ("creatorContentTypeUnspecified", UNSPECIFIED), ("SHORTS", SHORTS), ("VIDEO_ON_DEMAND", LONG_FORM),
    ("LIVE_STREAM", LIVE), ("somethingNew", "SOMETHINGNEW"), (None, None), ("", None),
])
def test_normalize_content_type(raw, expected):
    assert normalize_content_type(raw) == expected


def test_empty_period_is_unavailable_not_zero():
    class Empty(FakeAnalyticsService):
        def query(self, **kw):
            return FakeRequest(lambda: {"columnHeaders": [], "rows": []})

    from yt_report.periods import Period
    t = AnalyticsClient(Empty()).channel_totals(Period(date(2026, 1, 1), date(2026, 1, 7)))
    assert t.views is None and t.minutes_watched is None and t.avg_view_pct is None and t.net_subs is None


def test_redaction():
    s = redact("url https://discord.com/api/webhooks/123/abcDEF token ya29.A0secret refresh 1//0gABCDEFGHIJKLMNOPQRSTUV sk-ant-api03-xyz GOCSPX-abc")
    for secret in ("abcDEF", "ya29.A0secret", "1//0gABCDEF", "sk-ant-api03", "GOCSPX-abc"):
        assert secret not in s
    assert redact('{"refresh_token": "abc123"}') == '{"refresh_token": "[REDACTED]"}'


def test_ai_disabled_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AI_RECOMMENDATIONS", "true")
    env = tmp_path / ".env"
    env.write_text("")
    assert load_config(env).ai_enabled is False
