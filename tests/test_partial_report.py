"""Partial reports (--allow-incomplete): the last days of the week have no Analytics data yet.

Modelled on the live dry-run of 2026-11-08: week Nov 1–7 had data only for Nov 1–5.
No network; Discord and Google are mocked; nothing is written outside tmp dirs.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from test_cli_and_demo import WEBHOOK, Posted
from test_clients_and_pipeline import FakeAnalyticsService, FakeYouTubeService
from yt_report import __main__ as cli
from yt_report import analysis
from yt_report import discord_notifier as dn
from yt_report.analytics_client import AnalyticsClient
from yt_report.config import load_config
from yt_report.periods import Period
from yt_report.pipeline import IncompletePeriodError, build_report
from yt_report.youtube_client import YouTubeClient

NOV_1_7 = Period(date(2026, 11, 1), date(2026, 11, 7))
TODAY = date(2026, 11, 8)          # Sunday (Pacific) — Nov 6 and 7 not processed yet
LAST = "2026-11-05"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for k in ("AI_RECOMMENDATIONS", "ANTHROPIC_API_KEY", "YT_TOKEN_JSON", "DISCORD_WEBHOOK_URL", "REPORT_WEEK_END",
              "REPORT_TIMEZONE"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("DATA_LAG_DAYS=3\nLOG_DIR=\nMIN_VIEWS_WEAKEST=10\nMIN_VIDEOS_PER_FORMAT=2\n")
    return load_config(env)


def _build(cfg, last_data_day=LAST, allow=False, today=TODAY, cfg_override=None):
    svc = FakeAnalyticsService(last_data_day=last_data_day)
    rep = build_report(cfg_override or cfg, YouTubeClient(FakeYouTubeService()), AnalyticsClient(svc),
                       end=NOV_1_7.end, today=today, allow_incomplete=allow)
    return rep, svc


# ---------------------------------------------------------------- 1. complete ---
def test_complete_week_is_a_normal_report(cfg):
    rep, _ = _build(cfg, last_data_day=None, today=date(2026, 11, 10))
    assert not rep.partial and rep.data_through is None and rep.days_with_data == 7
    overview = dn.build_embeds(rep)[0]
    assert "PARTIAL" not in overview["title"] and "prev" in overview["fields"][0]["value"]
    assert not any(r.title.startswith("Recommendations paused") for r in rep.recommendations)
    assert not any(n.startswith("PARTIAL REPORT") for n in rep.notes)


# ------------------------------------------------- 2. incomplete, no flag ---
def test_incomplete_week_without_flag_is_refused_before_video_queries(cfg):
    svc = FakeAnalyticsService(last_data_day=LAST)
    with pytest.raises(IncompletePeriodError) as exc:
        build_report(cfg, YouTubeClient(FakeYouTubeService()), AnalyticsClient(svc), end=NOV_1_7.end, today=TODAY)
    assert "only through Nov 5 (5 of 7 days)" in str(exc.value) and "--allow-incomplete" in str(exc.value)
    assert not any(c.get("dimensions") == "video" for c in svc.calls)        # fails fast, no per-video queries


def test_week_without_any_daily_row_is_refused_even_with_flag(cfg):
    with pytest.raises(IncompletePeriodError):
        _build(cfg, last_data_day="2026-10-31", allow=True)


# --------------------------------------------------- 3./4. flag + labelling ---
def test_incomplete_week_with_flag_is_a_partial_report(cfg):
    rep, _ = _build(cfg, allow=True)
    assert rep.partial and rep.period == NOV_1_7 and rep.data_through == date(2026, 11, 5)
    assert rep.days_with_data == 5


def test_partial_report_is_labelled_5_of_7_days(cfg):
    rep, _ = _build(cfg, allow=True)
    overview = dn.build_embeds(rep)[0]
    assert overview["title"] == "📊 Example Channel — PARTIAL Weekly Report (5/7 days)"
    assert "Data available: Nov 1 – Nov 5, 2026 (5 of 7 days)" in overview["description"]
    assert "Requested week: Nov 1 – Nov 7, 2026" in overview["description"]
    assert overview["color"] == dn.COLOR_WARN
    assert overview["fields"][-1]["value"].startswith("⚠ Partial period: data Nov 1 – Nov 5 only (5/7 days)")
    assert rep.notes[0].startswith("PARTIAL REPORT: YouTube Analytics data covers 5 of 7 days of Nov 1 – Nov 7, 2026")
    assert analysis.completeness_issues(rep)[0] == "Partial period: data Nov 1 – Nov 5 only (5/7 days)"


# ------------------------------------------------------------ 5. comparisons ---
def test_partial_report_has_no_week_over_week_comparison(cfg):
    rep, _ = _build(cfg, allow=True)
    overview, formats = dn.build_embeds(rep)[:2]
    kpis = overview["fields"][:7]
    assert [f["name"] for f in kpis][:2] == ["Views", "Watch time"]
    for f in kpis:
        assert not any(sym in f["value"] for sym in ("▲", "▼", "🟢", "🔴", "prev", "%" if f["name"] != "Avg % viewed" else "▲"))
        assert f["value"].endswith("_5/7 days_")
    assert kpis[0]["value"].startswith("**1,000**")                           # informative current value kept
    assert "vs. previous" not in overview["description"]
    for f in formats["fields"]:
        assert "▲" not in f["value"] and "▼" not in f["value"]
    assert formats["footer"]["text"].startswith("⚠ Partial period (5/7 days): values only")


# -------------------------------------------------------- 6. recommendations ---
def test_partial_report_pauses_automatic_recommendations(cfg, monkeypatch):
    import dataclasses

    def no_ai(*a, **k):
        raise AssertionError("Claude must not be asked for recommendations on a partial week")

    monkeypatch.setattr("yt_report.ai_recommendations.claude_recommendations", no_ai)
    ai_cfg = dataclasses.replace(cfg, ai_enabled=True, anthropic_api_key="test-key")
    rep, _ = _build(cfg, allow=True, cfg_override=ai_cfg)
    assert [r.title for r in rep.recommendations] == ["Recommendations paused — partial week"]
    assert "covers 5 of 7 days (Nov 1 – Nov 5)" in rep.recommendations[0].body
    body = dn.build_embeds(rep)[-1]["description"]
    assert "Publishing" not in body and "Views fell" not in body


# ------------------------------------------------------------ 7. metrics kept ---
def test_partial_report_keeps_all_metrics_and_sources(cfg):
    partial, _ = _build(cfg, allow=True)
    complete, _ = _build(cfg, last_data_day=None, today=date(2026, 11, 10))
    assert partial.current == complete.current and partial.previous == complete.previous
    assert {k: (f.views, f.minutes_watched, f.source) for k, f in partial.formats_current.items()} == \
        {k: (f.views, f.minutes_watched, f.source) for k, f in complete.formats_current.items()}
    assert [v.video_id for v in partial.top_videos] == [v.video_id for v in complete.top_videos]
    assert (partial.coverage_current.views_rows, partial.coverage_current.views_reference) == \
        (complete.coverage_current.views_rows, complete.coverage_current.views_reference)
    assert partial.publications_current.published == complete.publications_current.published
    assert partial.missing_days == [date(2026, 11, 6), date(2026, 11, 7)]
    names = [f["name"] for f in dn.build_embeds(partial)[0]["fields"]]
    assert names == ["Views", "Watch time", "Net subscribers", "Subscribers gained", "Subscribers lost",
                     "Avg view duration", "Avg % viewed", "📅 Published", "Data completeness"]
    for msg in dn.split_messages(dn.build_embeds(partial)):
        assert len(msg) <= 10 and sum(dn.embed_chars(e) for e in msg) <= 6000


# ----------------------------------------------------- 8. Discord protection ---
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
def google(monkeypatch):
    state = {"last": LAST, "save_refreshed": []}

    def creds(cfg, interactive=False, open_browser=True, save_refreshed=True):
        state["save_refreshed"].append(save_refreshed)
        return object()

    monkeypatch.setattr("yt_report.auth.get_credentials", creds)
    monkeypatch.setattr("yt_report.auth.build_services",
                        lambda c: (FakeYouTubeService(), FakeAnalyticsService(last_data_day=state["last"])))
    return state


def run(env, *args):
    return cli.main(["--env-file", str(env), "run", "--end-date", "2026-11-07", *args])


def _db(env):
    return env.parent / "data" / "yt_report.db"


def test_cli_incomplete_without_flag_exits_9_and_sends_no_report(env, posted, google):
    assert run(env) == cli.EXIT_INCOMPLETE == 9
    titles = [e["title"] for p in posted.payloads for e in p["embeds"]]
    assert titles == ["❌ Reporting period not complete yet"]                # failure alert only, no report
    assert not _db(env).exists()


def test_cli_dry_run_incomplete_without_flag_posts_nothing(env, posted, google):
    assert run(env, "--dry-run") == 9
    assert posted.payloads == [] and not _db(env).exists()


def test_cli_allow_incomplete_alone_never_sends_or_stores(env, posted, google, capsys):
    assert run(env, "--allow-incomplete", "--print") == 0
    assert posted.payloads == [] and not _db(env).exists()
    assert "PARTIAL REPORT" in capsys.readouterr().out


def test_cli_send_partial_requires_allow_incomplete(env, posted, google):
    assert run(env, "--send-partial") == 2
    assert posted.payloads == [] and google["save_refreshed"] == []


def test_cli_dry_run_wins_over_send_partial(env, posted, google):
    assert run(env, "--dry-run", "--allow-incomplete", "--send-partial") == 0
    assert posted.payloads == [] and not _db(env).exists()


def test_cli_send_partial_posts_without_storing_and_full_report_still_follows(env, posted, google):
    assert run(env, "--allow-incomplete", "--send-partial") == 0
    assert "PARTIAL Weekly Report (5/7 days)" in posted.payloads[0]["embeds"][0]["title"]
    assert not _db(env).exists()                                              # never stored, no sent-flag
    sent_partial = len(posted.payloads)
    google["last"] = None                                                     # Tuesday: all 7 days processed
    assert run(env) == 0
    assert len(posted.payloads) > sent_partial                                # the complete report is sent
    full = posted.payloads[sent_partial]["embeds"][0]
    assert "PARTIAL" not in full["title"] and "vs. previous 7 days" in full["description"]


def test_cli_send_partial_with_no_discord_sends_nothing(env, posted, google):
    assert run(env, "--allow-incomplete", "--send-partial", "--no-discord") == 0
    assert posted.payloads == [] and not _db(env).exists()


# ------------------------------------------------- 9. dry-run and token.json ---
def test_cli_dry_run_keeps_refreshed_token_in_memory(env, posted, google):
    assert run(env, "--dry-run", "--allow-incomplete") == 0
    assert google["save_refreshed"] == [False]


def test_dry_run_refresh_never_writes_token_file(tmp_path, monkeypatch):
    from google.oauth2.credentials import Credentials

    from yt_report import auth

    for k in ("YT_TOKEN_JSON", "YT_TOKEN_FILE"):
        monkeypatch.delenv(k, raising=False)
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token": "expired-access", "refresh_token": "refresh", "client_id": "cid",
                                 "client_secret": "secret", "token_uri": "https://oauth2.googleapis.com/token",
                                 "scopes": auth.SCOPES, "expiry": "2020-01-01T00:00:00Z"}))
    env = tmp_path / ".env"
    env.write_text(f"YT_TOKEN_FILE={token.as_posix()}\nLOG_DIR=\n")
    cfg = load_config(env)

    def fake_refresh(self, request):
        self.token = "new-access"
        self.expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    before = token.read_bytes()
    creds = auth.get_credentials(cfg, interactive=False, save_refreshed=False)          # dry-run path
    assert creds.token == "new-access" and token.read_bytes() == before                # refreshed in memory only
    assert not (tmp_path / "token.json.tmp").exists()
    auth.get_credentials(cfg, interactive=False, save_refreshed=True)                  # control: normal run
    assert token.read_bytes() != before
