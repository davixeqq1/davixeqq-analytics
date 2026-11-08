"""Regression tests for the final audit: notes lifecycle, derived watch-time warning,
per-format retention logic, rounding, config/security validation, reliability paths."""

from datetime import date

import httplib2
import pytest
from googleapiclient.errors import HttpError

from test_clients_and_pipeline import FakeAnalyticsService, FakeRequest, FakeYouTubeService
from yt_report import analysis
from yt_report import discord_notifier as dn
from yt_report.analytics_client import AnalyticsClient
from yt_report.config import ConfigError, is_discord_webhook, load_config
from yt_report.formatting import fmt_change, fmt_duration, fmt_hours, fmt_pct, fmt_pp, round_half_up
from yt_report.models import (
    LIVE, LONG_FORM, SHORTS, SOURCE_API, SOURCE_DERIVED, SOURCE_UNAVAILABLE, ChannelTotals, FormatStats,
)
from yt_report.pipeline import ReportDataError, build_report
from yt_report.recommendations import build_recommendations
from yt_report.retry import execute_with_retry, is_retryable
from yt_report.youtube_client import YouTubeClient

TODAY = date(2026, 11, 9)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for k in ("AI_RECOMMENDATIONS", "ANTHROPIC_API_KEY", "YT_TOKEN_JSON", "DISCORD_WEBHOOK_URL", "REPORT_TIMEZONE",
              "REPORT_WEEK_END"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    # Rolling window: these tests exercise metrics, not period anchoring (see test_period_anchor.py).
    env.write_text("DATA_LAG_DAYS=3\nMIN_VIEWS_WEAKEST=10\nMIN_VIDEOS_PER_FORMAT=2\nREPORT_WEEK_END=none\n")
    return load_config(env)


def _err(status=400, reason=None):
    body = b'{"error": {"message": "x", "errors": [{"reason": "%s"}]}}' % (reason or "badRequest").encode()
    return HttpError(httplib2.Response({"status": status}), body)


class PrevFormatFails(FakeAnalyticsService):
    """Format totals fail only for the previous period (the cosmetic-bug scenario)."""

    def query(self, **kw):
        if kw.get("dimensions") == "creatorContentType" and kw["startDate"] < "2026-10-31":
            self.calls.append(kw)

            def boom():
                raise _err(400)
            return FakeRequest(boom)
        return super().query(**kw)


def _build(cfg, svc):
    return build_report(cfg, YouTubeClient(FakeYouTubeService()), AnalyticsClient(svc), today=TODAY)


# ---------------------------------------------------------------- Issue 2 ---
def test_recovered_previous_period_has_no_stale_unavailable_note(cfg):
    rep = _build(cfg, PrevFormatFails())
    assert all(f.source == SOURCE_API for f in rep.formats_current.values())
    assert all(f.source == SOURCE_DERIVED for ct, f in rep.formats_previous.items())
    assert not any("unavailable" in n for n in rep.notes)
    assert any("previous period were derived" in n for n in rep.notes)
    assert not any("this period were derived" in n for n in rep.notes)


def test_format_query_not_repeated_after_failure(cfg):
    svc = PrevFormatFails()
    _build(cfg, svc)
    prev_format_calls = [c for c in svc.calls if c.get("dimensions") == "creatorContentType" and c["startDate"] < "2026-10-31"]
    assert len(prev_format_calls) == 1  # previously the failing query was sent twice


def test_notes_describe_final_state_only():
    api = {ct: FormatStats(ct, 1, 1.0, source=SOURCE_API) for ct in (SHORTS, LONG_FORM, LIVE)}
    der = {ct: FormatStats(ct, 1, 1.0, source=SOURCE_DERIVED) for ct in (SHORTS, LONG_FORM, LIVE)}
    una = {ct: FormatStats(ct, source=SOURCE_UNAVAILABLE) for ct in (SHORTS, LONG_FORM, LIVE)}
    assert analysis.data_quality_notes(api, api) == []
    assert analysis.data_quality_notes(api, una) == [analysis.NOTE_UNAVAILABLE.format(which="the previous period")]
    notes = analysis.data_quality_notes(der, der)
    assert notes.count(analysis.NOTE_DERIVED_WATCH_TIME) == 1  # once, not per period
    assert analysis.data_quality_notes(api, api, videos_typed=False) == [analysis.NOTE_NO_VIDEO_TYPES]


# ---------------------------------------------------------------- Issue 1 ---
def test_derived_watch_time_warning_only_in_derived_mode(cfg):
    api_rep = _build(cfg, FakeAnalyticsService())
    assert analysis.NOTE_DERIVED_WATCH_TIME not in api_rep.notes
    der_rep = _build(cfg, FakeAnalyticsService(format_supported=False))
    assert analysis.NOTE_DERIVED_WATCH_TIME in der_rep.notes
    assert der_rep.formats_current[SHORTS].minutes_watched == 240  # plain sum, no artificial correction


def test_discord_shows_watch_time_warning_only_when_derived(cfg):
    fmt_embed = lambda rep: next(e for e in dn.build_embeds(rep) if "Livestream" in e["title"])  # noqa: E731
    assert "minute rounding" not in fmt_embed(_build(cfg, FakeAnalyticsService()))["footer"]["text"]
    assert "minute rounding" in fmt_embed(_build(cfg, FakeAnalyticsService(format_supported=False)))["footer"]["text"]


def test_discord_omits_trends_when_methods_differ(cfg):
    rep = _build(cfg, PrevFormatFails())
    emb = next(e for e in dn.build_embeds(rep) if "Livestream" in e["title"])
    assert "different method" in emb["footer"]["text"]
    assert not any("▲" in f["value"] or "▼" in f["value"] for f in emb["fields"])


# ---------------------------------------------------------------- Issue 3 ---
def _fmt(shorts, long_, live, source=SOURCE_API):
    """(views, avg % viewed) per format."""
    return {SHORTS: FormatStats(SHORTS, shorts[0], 100.0, 30.0, shorts[1], source),
            LONG_FORM: FormatStats(LONG_FORM, long_[0], 500.0, 150.0, long_[1], source),
            LIVE: FormatStats(LIVE, live[0], 200.0, 180.0, live[1], source)}


def test_mix_shift_is_not_reported_as_retention_change(report):
    # Channel-wide % viewed halves only because the format mix changed; each format is unchanged.
    report.formats_previous = _fmt((8000, 57.0), (600, 31.0), (300, 2.7))
    report.formats_current = _fmt((1100, 57.5), (700, 31.5), (250, 2.8))
    report.current.avg_view_pct, report.previous.avg_view_pct = 17.0, 35.0
    changes = {c.content_type: c.status for c in analysis.retention_changes(report.formats_current, report.formats_previous)}
    assert changes == {SHORTS: "stable", LONG_FORM: "stable", LIVE: "stable"}
    titles = [r.title for r in build_recommendations(report, limit=10)]
    assert not any("retention dropped" in t or "retention improved" in t for t in titles)
    assert "Keep retention as the weekly focus metric" not in titles


def test_real_format_decline_is_detected(report):
    report.formats_previous = _fmt((2000, 58.0), (600, 38.0), (300, 5.0))
    report.formats_current = _fmt((2100, 57.0), (620, 30.0), (310, 5.1))
    recs = build_recommendations(report, limit=10)
    lf = next(r for r in recs if r.title == "Long-form retention dropped")
    assert "30.0%" in lf.body and "38.0%" in lf.body and "-8.0 pp" in lf.body and "620 vs. 600 views" in lf.body
    assert not any(r.title.startswith("Shorts retention") for r in recs)  # -1 pp is noise for Shorts


def test_retention_improvement_is_detected():
    prev, cur = _fmt((500, 50.0), (300, 30.0), (150, 4.0)), _fmt((520, 58.0), (310, 30.5), (160, 5.5))
    status = {c.content_type: c.status for c in analysis.retention_changes(cur, prev)}
    assert status == {SHORTS: "improved", LONG_FORM: "stable", LIVE: "improved"}


@pytest.mark.parametrize("prev_views,cur_views", [(99, 500), (500, 99), (None, 500), (500, None)])
def test_retention_needs_enough_views_in_both_periods(prev_views, cur_views):
    prev = {LONG_FORM: FormatStats(LONG_FORM, prev_views, 1.0, 1.0, 40.0, SOURCE_API)}
    cur = {LONG_FORM: FormatStats(LONG_FORM, cur_views, 1.0, 1.0, 20.0, SOURCE_API)}
    rc = next(c for c in analysis.retention_changes(cur, prev) if c.content_type == LONG_FORM)
    assert rc.status == "insufficient"


@pytest.mark.parametrize("cur_src,prev_src", [(SOURCE_DERIVED, SOURCE_API), (SOURCE_API, SOURCE_DERIVED),
                                              (SOURCE_UNAVAILABLE, SOURCE_API)])
def test_retention_requires_api_data_in_both_periods(cur_src, prev_src):
    cur, prev = _fmt((500, 20.0), (500, 20.0), (500, 2.0), cur_src), _fmt((500, 40.0), (500, 40.0), (500, 4.0), prev_src)
    assert all(c.status == "insufficient" for c in analysis.retention_changes(cur, prev))


def test_stable_fallback_recommendation_is_per_format(report):
    report.current = ChannelTotals(5000, 9000.0, 100.0, 40.0, 50, 10)
    report.previous = ChannelTotals(4900, 8800.0, 100.0, 40.0, 48, 10)
    # Healthy, stable week: no other rule fires, so the per-format stability note is used.
    report.formats_previous = _fmt((2000, 75.0), (2500, 40.0), (400, 4.0))
    report.formats_current = _fmt((2050, 75.5), (2520, 41.0), (430, 4.2))
    report.top_videos, report.weakest = [], None
    recs = build_recommendations(report)
    stable = next(r for r in recs if r.title == "Retention is stable within each format")
    assert "Shorts 75.5% (+0.5 pp)" in stable.body and "Long-form 41.0% (+1.0 pp)" in stable.body
    assert "Livestream 4.2% (+0.2 pp)" in stable.body
    assert "40.0%" not in stable.body  # the channel-wide average is not used


# -------------------------------------------------------- metrics & rounding ---
def test_reference_audit_values_format_correctly():
    # Synthetic rounding edge cases (decimal half-up vs binary float rounding).
    assert fmt_hours(1227 / 60) == "20.5 h"         # half-up, not float-truncated "20.4 h"
    assert fmt_duration(85) == "1:25"
    assert fmt_hours(249 / 60) == "4.15 h" and fmt_hours(801 / 60) == "13.4 h" and fmt_hours(231 / 60) == "3.85 h"


@pytest.mark.parametrize("value,digits,expected", [(20.65, 1, 20.7), (84.5, 0, 85.0), (-2.25, 1, -2.3),
                                                   (2.675, 2, 2.68), (1.005, 2, 1.01), (-0.04, 1, 0.0)])
def test_round_half_up(value, digits, expected):
    assert round_half_up(value, digits) == expected


def test_no_negative_zero_and_na():
    assert fmt_change(-0.04) == "0.0%" and fmt_pp(-0.04) == "0.0 pp"
    assert fmt_pct(None) == fmt_hours(None) == fmt_duration(None) == fmt_change(None) == "n/a"


def test_empty_current_period_aborts_report(cfg):
    class Empty(FakeAnalyticsService):
        def query(self, **kw):
            if kw.get("dimensions") is None:
                return FakeRequest(lambda: {"columnHeaders": [], "rows": []})
            return super().query(**kw)

    with pytest.raises(ReportDataError):
        _build(cfg, Empty())


def test_missing_previous_period_is_noted_not_zero(cfg):
    class NoPrev(FakeAnalyticsService):
        def query(self, **kw):
            if kw.get("dimensions") is None and kw["startDate"] < "2026-10-31":
                return FakeRequest(lambda: {"columnHeaders": [], "rows": []})
            return super().query(**kw)

    rep = _build(cfg, NoPrev())
    assert rep.previous.views is None
    assert any("No data for the previous period" in n for n in rep.notes)
    views = next(c for c in analysis.compare_totals(rep.current, rep.previous) if c.key == "views")
    assert views.change_pct is None


# ------------------------------------------------------- config & security ---
@pytest.mark.parametrize("url,ok", [
    ("https://discord.com/api/webhooks/123/abc", True),
    ("https://discordapp.com/api/webhooks/123/abc", True),
    ("http://discord.com/api/webhooks/123/abc", False),
    ("https://discord.com.evil.example/api/webhooks/123/abc", False),
    ("https://evil.example/api/webhooks/123/abc", False),
    ("https://discord.com/api/webhooks/REPLACE_ME/REPLACE_ME", False),
    ("https://discord.com/channels/1/2", False),
    (None, False), ("", False),
])
def test_only_discord_webhooks_accepted(url, ok):
    assert is_discord_webhook(url) is ok


def test_invalid_timezone_rejected_before_any_api_call(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORT_TIMEZONE", "Mars/Olympus")
    env = tmp_path / ".env"
    env.write_text("")
    with pytest.raises(ConfigError):
        load_config(env)


def test_rate_limit_403_is_retried_but_quota_is_not(monkeypatch):
    assert is_retryable(_err(403, "rateLimitExceeded")) and is_retryable(_err(503))
    assert not is_retryable(_err(403, "quotaExceeded")) and not is_retryable(_err(400))
    monkeypatch.setattr("yt_report.retry.time.sleep", lambda s: None)
    calls = []

    class Req:
        def execute(self, num_retries=0):
            calls.append(1)
            if len(calls) < 3:
                raise _err(403, "userRateLimitExceeded")
            return {"ok": True}

    assert execute_with_retry(Req()) == {"ok": True} and len(calls) == 3


def test_build_services_uses_http_timeout(monkeypatch):
    from yt_report import auth
    seen = {}
    import httplib2 as h

    real = h.Http

    def spy(*a, **kw):
        seen["timeout"] = kw.get("timeout")
        return real(*a, **kw)

    monkeypatch.setattr(h, "Http", spy)
    creds = auth.Credentials(token="t", refresh_token="r", client_id="c", client_secret="s",
                             token_uri="https://oauth2.googleapis.com/token", scopes=auth.SCOPES)
    auth.build_services(creds)
    assert seen["timeout"] == auth.HTTP_TIMEOUT_S


def test_discord_never_gets_non_https_thumbnails(report):
    report.top_videos[0].thumbnail_url = "data:image/svg+xml;base64,AAAA"
    embeds = dn.build_embeds(report)
    assert all(e.get("thumbnail", {}).get("url", "https://").startswith("https://") for e in embeds)


def test_discord_429_with_non_json_body(monkeypatch):
    class R:
        def __init__(self, status):
            self.status_code, self.ok, self.content, self.text = status, status < 300, b"x", "x"

        def json(self):
            raise ValueError("not json")

    seq = iter([R(429), R(200)])
    monkeypatch.setattr(dn.requests, "post", lambda *a, **k: next(seq))
    monkeypatch.setattr(dn.time, "sleep", lambda s: None)
    dn.post("https://discord.com/api/webhooks/1/x", {"content": "x"})
