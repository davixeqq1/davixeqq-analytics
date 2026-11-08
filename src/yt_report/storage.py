"""SQLite persistence for weekly snapshots.

One row per (channel, period) in ``reports`` — re-running a period updates
it in place instead of creating a duplicate, and keeps ``discord_sent_at``
so a cron retry does not post the same report twice.

Schema versions (``meta.schema_version``):

* 1 — v1.0–v1.1 tables.
* 2 — v1.2.0 adds ``publication_stats``, ``coverage`` and ``views_by_age``.
  The migration is additive and idempotent: it only creates the new tables
  and bumps the version; existing tables and rows are never altered, so old
  reports stay readable and simply have no rows (null) in the new tables.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .models import REPORTED_FORMATS, LIVE, LONG_FORM, SHORTS, WeeklyReport
from .periods import Period

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS reports (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id       TEXT NOT NULL,
    channel_title    TEXT,
    subscriber_count INTEGER,
    period_start     TEXT NOT NULL,
    period_end       TEXT NOT NULL,
    prev_start       TEXT NOT NULL,
    prev_end         TEXT NOT NULL,
    generated_at     TEXT NOT NULL,
    notes            TEXT NOT NULL DEFAULT '[]',
    weakest_note     TEXT,
    discord_sent_at  TEXT,
    UNIQUE (channel_id, period_start, period_end)
);

CREATE TABLE IF NOT EXISTS totals (
    report_id        INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    which            TEXT NOT NULL CHECK (which IN ('current', 'previous')),
    views            INTEGER,
    minutes_watched  REAL,
    avg_view_duration_s REAL,
    avg_view_pct     REAL,
    subs_gained      INTEGER,
    subs_lost        INTEGER,
    PRIMARY KEY (report_id, which)
);

CREATE TABLE IF NOT EXISTS format_stats (
    report_id        INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    which            TEXT NOT NULL CHECK (which IN ('current', 'previous')),
    content_type     TEXT NOT NULL,
    views            INTEGER,
    minutes_watched  REAL,
    avg_view_duration_s REAL,
    avg_view_pct     REAL,
    source           TEXT NOT NULL,
    complete         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (report_id, which, content_type)
);

CREATE TABLE IF NOT EXISTS video_stats (
    report_id        INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    video_id         TEXT NOT NULL,
    rank             INTEGER NOT NULL,
    title            TEXT,
    thumbnail_url    TEXT,
    url              TEXT,
    content_type     TEXT,
    views            INTEGER NOT NULL,
    minutes_watched  REAL NOT NULL,
    avg_view_duration_s REAL,
    avg_view_pct     REAL,
    subs_gained      INTEGER,
    is_top           INTEGER NOT NULL DEFAULT 0,
    is_weakest       INTEGER NOT NULL DEFAULT 0,
    format_median_pct REAL,
    PRIMARY KEY (report_id, video_id)
);

CREATE TABLE IF NOT EXISTS recommendations (
    report_id        INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,
    source           TEXT NOT NULL,
    title            TEXT NOT NULL,
    body             TEXT NOT NULL,
    PRIMARY KEY (report_id, position)
);

CREATE TABLE IF NOT EXISTS daily_stats (
    channel_id       TEXT NOT NULL,
    day              TEXT NOT NULL,
    views            INTEGER,
    minutes_watched  REAL,
    subs_gained      INTEGER,
    subs_lost        INTEGER,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (channel_id, day)
);

CREATE INDEX IF NOT EXISTS idx_reports_period ON reports (channel_id, period_end);
"""

# v2 (v1.2.0): additive only. Executed on every open; CREATE ... IF NOT EXISTS keeps it idempotent.
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS publication_stats (
    report_id        INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    which            TEXT NOT NULL CHECK (which IN ('current', 'previous')),
    status           TEXT NOT NULL,
    complete         INTEGER NOT NULL,
    shorts           INTEGER,
    long_form        INTEGER,
    livestream       INTEGER,
    other_types      INTEGER,
    unclassified     INTEGER,
    private_excluded INTEGER,
    unlisted_excluded INTEGER,
    upcoming_excluded INTEGER,
    incomplete_reason TEXT,
    PRIMARY KEY (report_id, which)
);

CREATE TABLE IF NOT EXISTS coverage (
    report_id        INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    which            TEXT NOT NULL CHECK (which IN ('current', 'previous')),
    method           TEXT NOT NULL,
    rows             INTEGER NOT NULL,
    views_rows       INTEGER NOT NULL,
    views_reference  INTEGER,
    capped           INTEGER NOT NULL,
    capped_min_views INTEGER,
    complete         INTEGER NOT NULL,
    fallback_reason  TEXT,
    PRIMARY KEY (report_id, which)
);

CREATE TABLE IF NOT EXISTS views_by_age (
    report_id        INTEGER PRIMARY KEY REFERENCES reports(id) ON DELETE CASCADE,
    total_cur        INTEGER NOT NULL,
    total_prev       INTEGER NOT NULL,
    new_cur          INTEGER NOT NULL,
    prev_uploads_cur INTEGER NOT NULL,
    new_prev         INTEGER NOT NULL,
    older_cur        INTEGER NOT NULL,
    older_prev       INTEGER NOT NULL,
    complete         INTEGER NOT NULL
);
"""

V2_TABLES = ("publication_stats", "coverage", "views_by_age")

STORED_VIDEOS = 50  # per report; enough for history without bloating the DB


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=30)
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")  # safe on SD cards & concurrent dashboard reads
            self._conn.executescript(SCHEMA)
            self._migrate()
        except BaseException:
            # A corrupt or too-new database must not stay open: on Windows an open handle blocks
            # replacing or restoring the file while the dashboard keeps running.
            self._conn.close()
            raise

    def _migrate(self) -> None:
        """Bring the schema to SCHEMA_VERSION. Idempotent, additive, one transaction."""
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(
                f"Database schema version {version} is newer than this program supports ({SCHEMA_VERSION}).")
        try:
            self._conn.execute("BEGIN")
            for stmt in SCHEMA_V2.split(";"):
                if stmt.strip():
                    self._conn.execute(stmt)
            self._conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self._conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version' AND CAST(value AS INTEGER) < ?",
                               (str(SCHEMA_VERSION), SCHEMA_VERSION))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def schema_version(self) -> int:
        return int(self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def _tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------ write
    def save_report(self, report: WeeklyReport) -> int:
        """Insert or update the report for its period. Returns report id."""
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO reports (channel_id, channel_title, subscriber_count, period_start, period_end,
                                     prev_start, prev_end, generated_at, notes, weakest_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (channel_id, period_start, period_end) DO UPDATE SET
                    channel_title = excluded.channel_title,
                    subscriber_count = excluded.subscriber_count,
                    prev_start = excluded.prev_start,
                    prev_end = excluded.prev_end,
                    generated_at = excluded.generated_at,
                    notes = excluded.notes,
                    weakest_note = excluded.weakest_note
                """,
                (
                    report.channel_id, report.channel_title, report.subscriber_count,
                    report.period.start.isoformat(), report.period.end.isoformat(),
                    report.previous_period.start.isoformat(), report.previous_period.end.isoformat(),
                    report.generated_at.isoformat(timespec="seconds"), json.dumps(report.notes), report.weakest_note,
                ),
            )
            report_id = c.execute(
                "SELECT id FROM reports WHERE channel_id = ? AND period_start = ? AND period_end = ?",
                (report.channel_id, report.period.start.isoformat(), report.period.end.isoformat()),
            ).fetchone()["id"]

            for table in ("totals", "format_stats", "video_stats", "recommendations", *V2_TABLES):
                c.execute(f"DELETE FROM {table} WHERE report_id = ?", (report_id,))

            for which, t in (("current", report.current), ("previous", report.previous)):
                c.execute(
                    "INSERT INTO totals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (report_id, which, t.views, t.minutes_watched, t.avg_view_duration_s, t.avg_view_pct, t.subs_gained, t.subs_lost),
                )
            for which, formats in (("current", report.formats_current), ("previous", report.formats_previous)):
                for f in formats.values():
                    c.execute(
                        "INSERT INTO format_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (report_id, which, f.content_type, f.views, f.minutes_watched, f.avg_view_duration_s,
                         f.avg_view_pct, f.source, int(f.complete)),
                    )

            top_ids = {v.video_id for v in report.top_videos}
            weak_id = report.weakest.video.video_id if report.weakest else None
            stored = report.videos[:STORED_VIDEOS]
            if weak_id and weak_id not in {v.video_id for v in stored}:
                stored.append(report.weakest.video)
            for rank, v in enumerate(stored, start=1):
                c.execute(
                    "INSERT INTO video_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (report_id, v.video_id, rank, v.title, v.thumbnail_url, v.url, v.content_type, v.views,
                     v.minutes_watched, v.avg_view_duration_s, v.avg_view_pct, v.subs_gained,
                     int(v.video_id in top_ids), int(v.video_id == weak_id),
                     report.weakest.format_median_pct if v.video_id == weak_id else None),
                )
            for pos, r in enumerate(report.recommendations):
                c.execute("INSERT INTO recommendations VALUES (?, ?, ?, ?, ?)", (report_id, pos, r.source, r.title, r.body))

            for which, pub in (("current", report.publications_current), ("previous", report.publications_previous)):
                if pub is None:
                    continue
                other = sum(n for ct, n in pub.published.items() if ct not in REPORTED_FORMATS)
                c.execute(
                    "INSERT INTO publication_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (report_id, which, pub.status, int(pub.complete), pub.count(SHORTS), pub.count(LONG_FORM),
                     pub.count(LIVE), other, pub.unclassified, pub.private, pub.unlisted, pub.upcoming,
                     pub.incomplete_reason),
                )
            for which, cov in (("current", report.coverage_current), ("previous", report.coverage_previous)):
                if cov is None:
                    continue
                c.execute(
                    "INSERT INTO coverage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (report_id, which, cov.method, cov.rows, cov.views_rows, cov.views_reference, int(cov.capped),
                     cov.capped_min_views, int(cov.complete), cov.fallback_reason),
                )
            a = report.views_by_age
            if a is not None:
                c.execute(
                    "INSERT INTO views_by_age VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (report_id, a.total_cur, a.total_prev, a.new_cur, a.prev_uploads_cur, a.new_prev,
                     a.older_cur, a.older_prev, int(a.complete)),
                )

            now = _now()
            for d in report.daily:
                c.execute(
                    """
                    INSERT INTO daily_stats VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (channel_id, day) DO UPDATE SET views = excluded.views,
                        minutes_watched = excluded.minutes_watched, subs_gained = excluded.subs_gained,
                        subs_lost = excluded.subs_lost, updated_at = excluded.updated_at
                    """,
                    (report.channel_id, d.day.isoformat(), d.views, d.minutes_watched, d.subs_gained, d.subs_lost, now),
                )
        return report_id

    def mark_sent(self, report_id: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE reports SET discord_sent_at = ? WHERE id = ?", (_now(), report_id))

    def sent_at(self, report_id: int) -> str | None:
        row = self._conn.execute("SELECT discord_sent_at FROM reports WHERE id = ?", (report_id,)).fetchone()
        return row["discord_sent_at"] if row else None

    # ------------------------------------------------------------------- read
    def dashboard_data(self) -> dict:
        """All stored history as JSON-serialisable dicts (newest report last)."""
        c = self._conn
        reports = [dict(r) for r in c.execute("SELECT * FROM reports ORDER BY period_end ASC")]
        for rep in reports:
            rid = rep["id"]
            rep["notes"] = json.loads(rep["notes"] or "[]")
            rep["totals"] = {r["which"]: dict(r) for r in c.execute("SELECT * FROM totals WHERE report_id = ?", (rid,))}
            fmts: dict[str, dict] = {"current": {}, "previous": {}}
            for r in c.execute("SELECT * FROM format_stats WHERE report_id = ?", (rid,)):
                fmts[r["which"]][r["content_type"]] = dict(r)
            rep["formats"] = fmts
            rep["videos"] = [dict(r) for r in c.execute("SELECT * FROM video_stats WHERE report_id = ? ORDER BY rank", (rid,))]
            rep["recommendations"] = [dict(r) for r in c.execute(
                "SELECT source, title, body FROM recommendations WHERE report_id = ? ORDER BY position", (rid,))]
            # v2 fields: None for reports stored before v1.2.0 (no rows in the new tables).
            for key, table in (("publications", "publication_stats"), ("coverage", "coverage")):
                rows = {r["which"]: dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE report_id = ?", (rid,))}
                rep[key] = {"current": rows.get("current"), "previous": rows.get("previous")} if rows else None
            age = c.execute("SELECT * FROM views_by_age WHERE report_id = ?", (rid,)).fetchone()
            rep["views_by_age"] = dict(age) if age else None
        channel = reports[-1]["channel_id"] if reports else None
        daily = [dict(r) for r in c.execute(
            "SELECT day, views, minutes_watched, subs_gained, subs_lost FROM daily_stats WHERE channel_id = ? ORDER BY day",
            (channel,),
        )] if channel else []
        return {"reports": reports, "daily": daily}

    def report_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]

    def periods(self) -> list[tuple[str, Period]]:
        """(channel_id, period) of every stored report — used to flag overlapping periods."""
        return _periods(self._conn)


_PERIODS_SQL = "SELECT channel_id, period_start, period_end FROM reports"


def _periods(conn: sqlite3.Connection) -> list[tuple[str, Period]]:
    return [(r[0], Period(date.fromisoformat(r[1]), date.fromisoformat(r[2]))) for r in conn.execute(_PERIODS_SQL)]


def read_periods_readonly(path: Path | str) -> list[tuple[str, Period]]:
    """Stored report periods without any write: no schema setup, no migration, no -wal/-shm files.

    Used by ``run --dry-run``. Returns [] when the database does not exist yet.
    """
    p = Path(path)
    if not p.exists():
        return []
    conn = sqlite3.connect(f"file:{p.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        return _periods(conn)
    finally:
        conn.close()
