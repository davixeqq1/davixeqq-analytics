import json

import pytest

from yt_report import discord_notifier as dn
from yt_report.analysis import Comparison
from yt_report.models import SOURCE_UNAVAILABLE, FormatStats


def test_embeds_structure(report):
    embeds = dn.build_embeds(report)
    titles = [e["title"] for e in embeds]
    assert "Weekly Report" in titles[0]
    assert any("Shorts" in t for t in titles)
    assert any("Weakest" in t for t in titles)
    assert titles[-1] == "💡 Recommendations"
    top = [e for e in embeds if e["title"].startswith(("🥇", "🥈", "🥉"))]
    assert len(top) == 3 and all(e["url"].startswith("https://www.youtube.com/") for e in top)
    assert all("thumbnail" in e for e in top)
    assert "Oct 29 – Nov 4, 2026" in embeds[0]["description"]


def test_embeds_respect_limits(report):
    report.top_videos[0].title = "x" * 1000
    report.recommendations[0].body = "y" * 5000
    for e in dn.build_embeds(report):
        assert len(e["title"]) <= 256
        assert len(e.get("description", "")) <= 4096
        assert all(len(f["value"]) <= 1024 for f in e.get("fields", []))
    for msg in dn.split_messages(dn.build_embeds(report)):
        assert len(msg) <= 10
        assert sum(dn.embed_chars(e) for e in msg) <= 6000


def test_split_many_embeds():
    embeds = [{"title": "t", "description": "d" * 1000} for _ in range(25)]
    msgs = dn.split_messages(embeds)
    assert sum(len(m) for m in msgs) == 25
    assert all(len(m) <= 10 and sum(dn.embed_chars(e) for e in m) <= 6000 for m in msgs)


def test_trend_indicators():
    assert "🟢 ▲" in dn.trend(Comparison("views", "Views", 120, 100))
    assert "🔴 ▼" in dn.trend(Comparison("views", "Views", 80, 100))
    assert "🔴 ▲" in dn.trend(Comparison("subs_lost", "Lost", 12, 10, higher_is_better=False))
    assert dn.trend(Comparison("views", "Views", 5, 0)) == "▲ new"
    assert dn.trend(Comparison("views", "Views", None, 10)) == "data unavailable"


def test_unavailable_format_is_labelled(report):
    report.formats_current["SHORTS"] = FormatStats("SHORTS", source=SOURCE_UNAVAILABLE)
    fmt = next(e for e in dn.build_embeds(report) if "Shorts" in e["title"])
    assert "unavailable" in json.dumps(fmt["fields"][0])


class FakeResp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}
        self.ok = 200 <= status < 300
        self.content = json.dumps(self._body).encode()
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def test_post_retries_on_rate_limit(monkeypatch):
    calls = []
    responses = iter([FakeResp(429, {"retry_after": 0.01}), FakeResp(200)])
    monkeypatch.setattr(dn.requests, "post", lambda *a, **k: calls.append(k) or next(responses))
    monkeypatch.setattr(dn.time, "sleep", lambda s: None)
    dn.post("https://discord.com/api/webhooks/1/abc", {"content": "x"})
    assert len(calls) == 2
    assert calls[0]["json"]["allowed_mentions"] == {"parse": []}


def test_post_error_does_not_leak_webhook(monkeypatch):
    url = "https://discord.com/api/webhooks/123/SECRETTOKEN"
    monkeypatch.setattr(dn.requests, "post", lambda *a, **k: FakeResp(404, {"message": f"Unknown Webhook {url}"}))
    with pytest.raises(dn.DiscordError) as exc:
        dn.post(url, {"content": "x"})
    assert "SECRETTOKEN" not in str(exc.value)


def test_three_format_fields(report):
    from yt_report.models import LIVE
    report.formats_current[LIVE] = FormatStats(LIVE, 250, 200.0, 190.0, 5.2, "api")
    fmt = next(e for e in dn.build_embeds(report) if "Livestream" in e["title"])
    names = [f["name"] for f in fmt["fields"]]
    assert names == ["📱 Shorts", "🎬 Long-form", "🔴 Livestream"]
    live = fmt["fields"][2]["value"]
    assert "250" in live and "5.2%" in live and "3:10" in live
    assert "Livestreams are counted separately" in fmt["footer"]["text"]


def test_missing_averages_render_as_na_not_zero(report):
    fmt = next(e for e in dn.build_embeds(report) if "Livestream" in e["title"])
    live = fmt["fields"][2]["value"]
    assert "Avg % viewed: **n/a**" in live and "0.0%" not in live


def test_overview_has_format_mix_note(report):
    overview = dn.build_embeds(report)[0]
    assert overview["footer"]["text"] == "ℹ Channel-wide averages depend on the format mix — compare per-format values."


def test_derived_footer_describes_fallback_method(report):
    from yt_report.models import SOURCE_DERIVED
    for f in report.formats_current.values():
        f.source, f.avg_view_pct = SOURCE_DERIVED, None
    fmt = next(e for e in dn.build_embeds(report) if "Livestream" in e["title"])
    assert "weighted by engaged views" in fmt["footer"]["text"]
    assert "view-weighted" not in fmt["footer"]["text"]
    assert all("Avg % viewed: **n/a**" in fld["value"] for fld in fmt["fields"])
