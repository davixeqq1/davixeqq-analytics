"""End-to-end CLI tests with all external services mocked (no Google, no Discord, temp dirs only)."""

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from test_clients_and_pipeline import FakeAnalyticsService, FakeRequest, FakeYouTubeService
from yt_report import __main__ as cli
from yt_report.dashboard import make_handler
from yt_report.storage import Storage

WEBHOOK = "https://discord.com/api/webhooks/123/test-token"


class Posted:
    def __init__(self):
        self.payloads = []

    def __call__(self, url, json=None, params=None, timeout=None):
        self.payloads.append(json)

        class R:
            status_code, ok, content, text = 200, True, b"{}", "{}"

            def json(self):
                return {}
        return R()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("AI_RECOMMENDATIONS", "ANTHROPIC_API_KEY", "YT_TOKEN_JSON", "DISCORD_WEBHOOK_URL", "DATABASE_PATH",
              "LOG_DIR", "REPORT_TIMEZONE", "DATA_LAG_DAYS", "REPORT_WEEK_END"):
        monkeypatch.delenv(k, raising=False)
    path = tmp_path / ".env"
    path.write_text(f"DISCORD_WEBHOOK_URL={WEBHOOK}\nDATABASE_PATH=data/yt_report.db\nLOG_DIR=\n"
                    "MIN_VIEWS_WEAKEST=10\nMIN_VIDEOS_PER_FORMAT=2\n")
    return path


@pytest.fixture
def posted(monkeypatch):
    p = Posted()
    monkeypatch.setattr("yt_report.discord_notifier.requests.post", p)
    monkeypatch.setattr("yt_report.discord_notifier.time.sleep", lambda s: None)
    return p


@pytest.fixture
def fake_google(monkeypatch):
    calls = {}

    def get_credentials(cfg, interactive=False, open_browser=True, save_refreshed=True):
        calls["save_refreshed"] = save_refreshed
        return object()

    monkeypatch.setattr("yt_report.auth.get_credentials", get_credentials)
    monkeypatch.setattr("yt_report.auth.build_services", lambda creds: (FakeYouTubeService(), FakeAnalyticsService()))
    return calls


def run(env, *args):
    return cli.main(["--env-file", str(env), *args])


# ------------------------------------------------------------ production path ---
def test_run_stores_sends_once_and_prevents_duplicates(env, posted, fake_google):
    assert run(env, "run", "--end-date", "2026-11-06") == 0
    first = len(posted.payloads)
    assert first >= 1 and all("embeds" in p for p in posted.payloads)
    assert run(env, "run", "--end-date", "2026-11-06") == 0  # same period again
    assert len(posted.payloads) == first                    # nothing re-sent
    with Storage(env.parent / "data" / "yt_report.db") as db:
        assert db.report_count() == 1
        assert db.dashboard_data()["reports"][0]["discord_sent_at"]


def test_dry_run_writes_nothing_and_keeps_token_in_memory(env, posted, fake_google):
    assert run(env, "run", "--dry-run", "--end-date", "2026-11-06") == 0
    assert posted.payloads == []
    assert not (env.parent / "data" / "yt_report.db").exists()
    assert fake_google["save_refreshed"] is False


def test_no_data_exits_6_without_report(env, posted, monkeypatch):
    class Empty(FakeAnalyticsService):
        def query(self, **kw):
            return FakeRequest(lambda: {"columnHeaders": [], "rows": []})

    monkeypatch.setattr("yt_report.auth.get_credentials", lambda *a, **k: object())
    monkeypatch.setattr("yt_report.auth.build_services", lambda creds: (FakeYouTubeService(), Empty()))
    assert run(env, "run", "--end-date", "2026-11-06") == 6
    assert len(posted.payloads) == 1 and "No analytics data" in posted.payloads[0]["embeds"][0]["title"]
    assert not (env.parent / "data" / "yt_report.db").exists()


def test_database_error_exits_7_and_sends_no_report(env, posted, fake_google, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("yt_report.storage.Storage", broken)
    assert run(env, "run", "--end-date", "2026-11-06") == 7
    assert len(posted.payloads) == 1 and "Database error" in posted.payloads[0]["embeds"][0]["title"]


def test_non_discord_webhook_is_rejected(env, posted, fake_google):
    env.write_text(env.read_text().replace(WEBHOOK, "https://evil.example/api/webhooks/1/x"))
    assert run(env, "run", "--end-date", "2026-11-06") == 2
    assert posted.payloads == []


# ------------------------------------------------------------------ demo mode ---
@pytest.fixture
def no_external(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("demo mode must not call external services")

    monkeypatch.setattr("yt_report.auth.get_credentials", forbidden)
    monkeypatch.setattr("yt_report.auth.build_services", forbidden)
    monkeypatch.setattr("yt_report.discord_notifier.requests.post", forbidden)


def test_demo_is_isolated(env, no_external):
    assert run(env, "demo", "--no-dashboard", "--weeks", "4") == 0
    root = env.parent / "data"
    assert not (root / "yt_report.db").exists()          # production DB untouched
    demo_db = root / "demo" / "demo.db"
    with Storage(demo_db) as db:
        data = db.dashboard_data()
    assert len(data["reports"]) == 4
    assert {r["channel_id"] for r in data["reports"]} == {"DEMO_CHANNEL"}
    assert all("synthetic" in r["channel_title"] for r in data["reports"])
    assert all(r["discord_sent_at"] is None for r in data["reports"])
    preview = json.loads((root / "demo" / "discord_preview.json").read_text(encoding="utf-8"))
    assert "Nothing was sent" in preview["_note"] and preview["messages"]


def test_demo_does_not_touch_existing_production_records(env, no_external, report):
    prod = env.parent / "data" / "yt_report.db"
    with Storage(prod) as db:
        db.save_report(report)
    before = prod.read_bytes()
    assert run(env, "demo", "--no-dashboard", "--weeks", "2") == 0
    assert prod.read_bytes() == before


def test_demo_refuses_to_share_production_database(env, no_external):
    env.write_text(env.read_text().replace("DATABASE_PATH=data/yt_report.db", "DATABASE_PATH=data/demo/demo.db"))
    assert run(env, "demo", "--no-dashboard") == 2


def test_demo_uses_real_pipeline_consistently(env, no_external):
    from yt_report.config import load_config
    from yt_report.demo import build_demo_reports
    reps = build_demo_reports(load_config(env), weeks=3)
    for r in reps:
        fmt_views = sum(f.views for f in r.formats_current.values())
        assert fmt_views == r.current.views                       # formats add up to the channel total
        assert all(f.source == "api" for f in r.formats_current.values())
        assert r.weakest is None or r.weakest.video.content_type is not None
        assert 2 <= len(r.recommendations) <= 3


# ------------------------------------------------------------------- dashboard ---
def _serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_dashboard_reports_mode(tmp_path):
    httpd, url = _serve(make_handler(tmp_path / "missing.db", mode="demo"))
    try:
        data = json.loads(urllib.request.urlopen(url + "/api/data", timeout=5).read())
        assert data == {"reports": [], "daily": [], "mode": "demo"}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_dashboard_unreadable_database_returns_500(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    httpd, url = _serve(make_handler(bad))
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url + "/api/data", timeout=5)
        assert exc.value.code == 500
        assert "could not be read" in json.loads(exc.value.read())["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
