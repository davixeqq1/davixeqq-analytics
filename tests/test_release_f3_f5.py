"""Release fixes: F3 (Discord Data notes within 1,024 characters), F4 (overlapping reporting periods
after the switch to Sunday–Saturday weeks), F5 (cross-platform single-run lock)."""

import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import date

import pytest

from test_cli_and_demo import WEBHOOK, Posted  # noqa: F401 - fixtures/helpers reused
from test_clients_and_pipeline import FakeAnalyticsService, FakeYouTubeService
from yt_report import __main__ as cli
from yt_report import analysis
from yt_report import discord_notifier as dn
from yt_report.analytics_client import AnalyticsClient
from yt_report.config import load_config
from yt_report.locking import LockBusy, RunLock, lock_path_for
from yt_report.models import ChannelTotals
from yt_report.periods import Period
from yt_report.pipeline import build_report
from yt_report.storage import Storage, read_periods_readonly
from yt_report.youtube_client import YouTubeClient

REPORT_1 = Period(date(2026, 10, 29), date(2026, 11, 4))      # historical report #1 (Thursday–Wednesday)
NOV_1_7 = Period(date(2026, 11, 1), date(2026, 11, 7))
OCT_25_31 = Period(date(2026, 10, 25), date(2026, 10, 31))
NOV_8_14 = Period(date(2026, 11, 8), date(2026, 11, 14))


# ===================================================================== F3 ===
def _notes_of_length(total: int, count: int = 4) -> list[str]:
    """``count`` notes whose bullet-joined text is exactly ``total`` characters."""
    overhead = count * 2 + (count - 1)               # "• " per note + "\n" separators
    body = total - overhead
    sizes = [body // count] * count
    sizes[-1] += body - sum(sizes)
    return [f"N{i}" + "x" * (s - 2) for i, s in enumerate(sizes)]


def test_f3_notes_exactly_at_limit_are_shown_complete():
    notes = _notes_of_length(1024)
    value = dn.fit_notes(notes)
    assert len(value) == 1024 and value == "\n".join(f"• {n}" for n in notes) and "more note" not in value


def test_f3_one_character_over_limit_drops_whole_notes_only():
    notes = _notes_of_length(1025)
    value = dn.fit_notes(notes)
    assert len(value) <= 1024
    lines = value.split("\n")
    shown, suffix = lines[:-1], lines[-1]
    assert all(line[2:] in notes for line in shown)            # every shown note is complete
    hidden = len(notes) - len(shown)
    assert hidden >= 1 and suffix == f"… +{hidden} more note{'s' if hidden != 1 else ''} on the dashboard"


@pytest.mark.parametrize("total", [1000, 1023, 1024, 1025, 1030, 1100, 2089, 5000])
def test_f3_any_size_stays_within_limit_without_cutting_a_note(total):
    notes = _notes_of_length(total, count=13)
    value = dn.fit_notes(notes)
    assert len(value) <= 1024
    for line in value.split("\n"):
        assert line.startswith("… +") or line[2:] in notes


def test_f3_single_note_longer_than_limit_is_never_cut():
    assert dn.fit_notes(["y" * 2000]) == "… +1 more note on the dashboard"


def test_f3_embed_and_database_keep_all_notes(report):
    report.notes = [f"Note {i}: " + "detail " * 30 for i in range(13)]          # ~2,900 characters
    embed = dn.build_embeds(report)[-1]
    value = embed["fields"][0]["value"]
    assert len(value) <= 1024 and value.endswith("on the dashboard")
    shown = [line for line in value.split("\n") if line.startswith("• ")]
    assert [line[2:] for line in shown] == report.notes[:len(shown)]
    assert value.split("\n")[-1] == f"… +{13 - len(shown)} more notes on the dashboard"
    with Storage(":memory:") as db:
        db.save_report(report)
        assert db.dashboard_data()["reports"][0]["notes"] == report.notes     # complete in the database
    for msg in dn.split_messages(dn.build_embeds(report)):
        assert len(msg) <= 10 and sum(dn.embed_chars(e) for e in msg) <= 6000


# ===================================================================== F4 ===
def test_f4_first_saturday_week_overlaps_report_1():
    found = analysis.period_overlaps([("UC1", REPORT_1)], "UC1", NOV_1_7, OCT_25_31)
    assert found == [REPORT_1]


def test_f4_following_week_is_a_normal_comparison():
    history = [("UC1", REPORT_1), ("UC1", NOV_1_7)]
    assert analysis.period_overlaps(history, "UC1", NOV_8_14, NOV_1_7) == []


def test_f4_aligned_history_and_other_channels_never_flag():
    history = [("UC1", OCT_25_31.shift(-7)), ("UC1", OCT_25_31), ("UC1", NOV_1_7), ("OTHER", REPORT_1)]
    assert analysis.period_overlaps(history, "UC1", NOV_1_7, OCT_25_31) == []    # re-run of the same week
    assert analysis.period_overlaps(history, "UC1", NOV_8_14, NOV_1_7) == []


def test_f4_report_flags_overlap_in_notes_and_discord(cfg_anchor):
    rep = build_report(cfg_anchor, YouTubeClient(FakeYouTubeService()), AnalyticsClient(FakeAnalyticsService()),
                       end=date(2026, 11, 7), today=date(2026, 11, 10), history=[("UC1", REPORT_1)])
    assert rep.period == NOV_1_7 and rep.period_overlaps == [REPORT_1]
    assert rep.notes[0].startswith("This report's periods overlap earlier stored report(s) for Oct 29 – Nov 4, 2026")
    desc = dn.build_embeds(rep)[0]["description"]
    assert "Reporting week realigned" in desc and "Oct 29 – Nov 4, 2026" in desc
    assert "compare Nov 1 – Nov 7, 2026 with Oct 25 – Oct 31, 2026" in desc


def test_f4_next_week_has_no_overlap_warning(cfg_anchor):
    rep = build_report(cfg_anchor, YouTubeClient(FakeYouTubeService()), AnalyticsClient(FakeAnalyticsService()),
                       end=date(2026, 11, 14), today=date(2026, 11, 17),
                       history=[("UC1", REPORT_1), ("UC1", NOV_1_7)])
    assert rep.period_overlaps == [] and not any("overlap" in n for n in rep.notes)
    assert "realigned" not in dn.build_embeds(rep)[0]["description"]


def test_f4_missing_previous_period_is_shown_as_no_valid_comparison(report):
    report.previous = ChannelTotals()
    assert "No valid previous comparison" in dn.build_embeds(report)[0]["description"]


def test_f4_readonly_history_read_creates_nothing(tmp_path, report):
    db = tmp_path / "prod.db"
    with Storage(db) as s:
        s.save_report(report)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert read_periods_readonly(db) == [(report.channel_id, report.period)]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before   # no -wal/-shm, no change
    assert read_periods_readonly(tmp_path / "missing.db") == []


# ---------------------------------------------------------------- CLI e2e ---
@pytest.fixture
def cfg_anchor(tmp_path, monkeypatch):
    for k in ("AI_RECOMMENDATIONS", "ANTHROPIC_API_KEY", "YT_TOKEN_JSON", "DISCORD_WEBHOOK_URL", "REPORT_WEEK_END",
              "REPORT_TIMEZONE"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("DATA_LAG_DAYS=3\nLOG_DIR=\nMIN_VIEWS_WEAKEST=10\nMIN_VIDEOS_PER_FORMAT=2\n")
    return load_config(env)


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
    calls = []

    def creds(*a, **k):
        calls.append(1)
        return object()

    monkeypatch.setattr("yt_report.auth.get_credentials", creds)
    monkeypatch.setattr("yt_report.auth.build_services", lambda c: (FakeYouTubeService(), FakeAnalyticsService()))
    return calls


def _dump_report_1(db):
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True)
    out = {t: c.execute(f"SELECT * FROM {t} WHERE report_id = 1 ORDER BY 2").fetchall()
           for t in ("totals", "format_stats", "video_stats", "recommendations")}
    out["reports"] = c.execute("SELECT * FROM reports WHERE id = 1").fetchall()
    c.close()
    return out


def test_f4_cli_first_week_warns_and_keeps_report_1_unchanged(env, posted, google, report):
    db = env.parent / "data" / "yt_report.db"
    report.channel_id = "UC1"                                   # same channel as the fake API
    with Storage(db) as s:                                      # stored report #1: Oct 29 – Nov 4
        s.mark_sent(s.save_report(report))
    before = _dump_report_1(db)
    assert cli.main(["--env-file", str(env), "run", "--end-date", "2026-11-07"]) == 0
    overview = posted.payloads[0]["embeds"][0]
    assert "Reporting week realigned" in overview["description"]
    assert _dump_report_1(db) == before                         # historical report #1 untouched
    with Storage(db) as s:
        data = s.dashboard_data()
    new = next(r for r in data["reports"] if r["period_start"] == "2026-11-01")
    assert new["notes"][0].startswith("This report's periods overlap")


def test_f4_cli_dry_run_reads_history_without_writing(env, posted, google, report, capsys):
    db = env.parent / "data" / "yt_report.db"
    report.channel_id = "UC1"
    with Storage(db) as s:
        s.save_report(report)
    files_before = {p.name: p.read_bytes() for p in db.parent.iterdir()}
    assert cli.main(["--env-file", str(env), "run", "--dry-run", "--end-date", "2026-11-07"]) == 0
    assert "This report's periods overlap" in capsys.readouterr().out
    assert {p.name: p.read_bytes() for p in db.parent.iterdir()} == files_before
    assert posted.payloads == []


# ===================================================================== F5 ===
def test_f5_lock_acquire_contention_and_release(tmp_path):
    path = tmp_path / "run.lock"
    a, b = RunLock(path), RunLock(path)
    assert a.acquire() and a.held
    assert not b.acquire() and not b.held                       # contention
    a.release()
    assert not a.held and b.acquire()                           # released -> available
    b.release()


def test_f5_context_manager_releases_after_failure(tmp_path):
    path = tmp_path / "run.lock"
    with pytest.raises(ValueError):
        with RunLock(path):
            with pytest.raises(LockBusy):
                with RunLock(path):
                    pass
            raise ValueError("run failed")
    assert RunLock(path).acquire()                              # released despite the exception


def test_f5_lock_released_when_holder_process_is_killed(tmp_path):
    path = tmp_path / "run.lock"
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str((__import__('pathlib').Path(cli.__file__).parents[1])).__repr__()})
        from yt_report.locking import RunLock
        lock = RunLock({str(path).__repr__()})
        print(lock.acquire(), flush=True)
        time.sleep(60)
    """)
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "True"
        assert not RunLock(path).acquire()                      # held by another process
    finally:
        child.kill()
        child.wait()
    # The OS releases the lock of a killed process; on Windows this happens asynchronously, shortly after exit.
    lock, deadline = RunLock(path), time.monotonic() + 5
    while not lock.acquire() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert lock.held                                            # released on interruption
    lock.release()


def test_f5_concurrent_run_exits_8_without_api_db_or_discord(env, posted, google):
    db = env.parent / "data" / "yt_report.db"
    holder = RunLock(lock_path_for(db))
    assert holder.acquire()                                     # another run is in progress
    try:
        assert cli.main(["--env-file", str(env), "run", "--end-date", "2026-11-07"]) == cli.EXIT_LOCKED == 8
    finally:
        holder.release()
    assert posted.payloads == [] and google == [] and not db.exists()


def test_f5_lock_is_released_after_run_so_retry_is_not_duplicated(env, posted, google):
    assert cli.main(["--env-file", str(env), "run", "--end-date", "2026-11-07"]) == 0
    first = len(posted.payloads)
    assert first >= 1
    assert cli.main(["--env-file", str(env), "run", "--end-date", "2026-11-07"]) == 0   # retry: lock free
    assert len(posted.payloads) == first                        # duplicate guard: nothing re-sent
    lock = RunLock(lock_path_for(env.parent / "data" / "yt_report.db"))
    assert lock.acquire()
    lock.release()


def test_f5_lock_released_after_failed_run(env, posted, monkeypatch):
    from yt_report.auth import AuthError

    def fail(*a, **k):
        raise AuthError("token revoked")

    monkeypatch.setattr("yt_report.auth.get_credentials", fail)
    assert cli.main(["--env-file", str(env), "run", "--end-date", "2026-11-07"]) == 3
    lock = RunLock(lock_path_for(env.parent / "data" / "yt_report.db"))
    assert lock.acquire()
    lock.release()


def test_f5_dry_run_is_not_blocked_by_a_running_report(env, posted, google):
    holder = RunLock(lock_path_for(env.parent / "data" / "yt_report.db"))
    assert holder.acquire()
    try:
        assert cli.main(["--env-file", str(env), "run", "--dry-run", "--end-date", "2026-11-07"]) == 0
    finally:
        holder.release()
    assert posted.payloads == []
