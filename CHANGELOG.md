# Changelog

## 1.2.0 — 2026-11-08

Verified with 337 automated tests (no network) and read-only dry-runs against the live YouTube
APIs.

### Added
- **Publication statistics**: public uploads per format for both periods (Pacific dates,
  livestreams by `actualStartTime`), private / unlisted / scheduled uploads excluded and counted
  separately, unclassified uploads reported explicitly. Uploads playlist scan capped at 40 pages
  with duplicate-entry and page-token safeguards. Deleted uploads are never counted.
- **Per-video rows per content type** (`creatorContentType` filter) with measured **coverage**:
  shortfalls are attributed as *not assigned to any video by the API*, *confirmed 200-row cap*,
  *suspected truncation* or *cause not identified*; excess rows are flagged as inconsistent.
  Non-video content (e.g. posts) counts for coverage but is never ranked as a video — on the
  primary path and in the full single-query fallback.
- **Views by upload age** (new uploads vs. older catalog, like-for-like) and a deterministic
  **views-trend decision tree** (upload spike / publishing change / performance / not separable),
  symmetric for growth, worded as "coincided with".
- Discord overview fields **Published** and **Data completeness**; dashboard "Published" row and
  completeness-aware quality badge.
- **Calendar reporting weeks** (`REPORT_WEEK_END`, default `saturday`): every run between two
  Tuesdays (Pacific) selects the same Sunday–Saturday week; explicit `--end-date` is used as given.
- **Run lock** (`locking.py`, stdlib only): a concurrent `run` exits with code **8** before any API
  call, database write or Discord post; released by the OS after success, failure or a crash.
- **Overlap warning**: a report whose weeks overlap an earlier stored report (the first
  Sunday–Saturday week vs. report #1) says so in Discord, the data notes and the dashboard history.
  Missing previous data is shown as "No valid previous comparison".
- **Partial-week protection**: if the last days of the week have no Analytics data yet, `run`
  stops with exit code 9. `--allow-incomplete` builds a labelled partial report (N/7 days, exact
  data range, no comparisons, no recommendations) that is never stored and is sent only with
  `--send-partial`.
- **Dashboard**: section navigation, loading skeleton, sortable video explorer with format filter
  (all stored videos, first 10 shown), coverage and views-by-upload-age panels, clearer DEMO label,
  keyboard focus styles and a compact mobile layout. Front-end checks: template/JS consistency and
  a Node-based render test (no browser needed).
- SQLite **schema v2** (`publication_stats`, `coverage`, `views_by_age`); additive, idempotent
  migration; old reports remain readable with `null` in the new fields.

### Changed
- Coverage reference = sum of API format totals over all content types; views-by-age uses the same
  base as the per-video rows.
- A format query that falls short of its total is kept (never replaced by the weaker single query).
  The single query is used only when format totals are unavailable or every format query fails;
  failed formats in a partial failure are filled from it.
- Discord data notes are shortened by whole notes only ("… +N more notes on the dashboard").
- Schedule: Raspberry Pi cron Tuesday 10:00 with a 14:00 retry (Europe/Bratislava). The Monday
  12:00 retry, which selected a different period than the 08:00 run, was removed. (The private
  GitHub Actions reporting workflow is not part of the public repository.)
- Daily series: every day of the 14-day window is checked, not only the last one.

### Security
- Dashboard: on a loopback bind, requests whose `Host` header is not `localhost` / `127.0.0.1` /
  `[::1]` get HTTP 403 (DNS-rebinding protection); every response sends `X-Frame-Options: DENY`.
- The OAuth token file is created owner-only (`0600`) from the first byte on Linux/macOS instead
  of being restricted after writing.

### Fixed
- `dashboard` / `demo` crashed with a traceback when the port was in use or out of range; they now
  exit with code 2 and a clear message. `--port` and `DASHBOARD_PORT` are validated (1–65535).
- On Windows a second dashboard on a port already in use started silently (SO_REUSEADDR) while the
  first one kept answering; the dashboard now binds exclusively there and reports the conflict.
- `demo --weeks 0 --print` crashed with `IndexError`; `--weeks` is validated (1–104).
- `demo` on a corrupt demo database ended with a traceback; it now exits with code 7 and a
  message, leaving the file untouched.
- A corrupt or too-new database left its file open after the error, so on Windows it could not be
  replaced or restored while the dashboard was running; the connection is now closed on failure.
- "Recover the drop in views" recommended new titles/thumbnails when the drop coincided with fewer
  uploads after an upload spike (first production report).
- Derived format totals were flagged incomplete purely from the row count (`rows == 200`).
- Week boundaries depended on the exact run time, so the Monday retry and DST-mismatch weeks
  produced different, overlapping or gapped periods.
- Posts could rank as top videos in the full single-query fallback.
- Ambiguously classified videos in a partial fallback were blamed on truncation / unassigned views.

### Deployment requirements
- Python ≥ 3.11; no new dependencies.
- Run on **Tuesday from 10:00 CET**; use **one** scheduler (e.g. Pi cron).
- First run: `run --dry-run --print --end-date 2026-11-07`, check that both coverage lines say
  `via by_format`, then `run --print --end-date 2026-11-07`.
- An existing database migrates to schema v2 on first open (additive, idempotent; back it up first).

### Known limitations
- Some weeks stay *incomplete* by design (e.g. a week in which a handful of views were assigned to
  no video by the API).
- The `creatorContentType` filter on the top-videos report is undocumented by Google.
- The run lock covers one machine; separate databases on different machines are not coordinated.
- The first Sunday–Saturday report overlaps report #1 once (flagged, not compared).
- Coverage diagnostics are stored in the report notes, not in dedicated database columns.

## 1.1.0

Verified metric definitions (AVD weighted by engaged views, format-mix note), per-format
retention comparison, livestream as its own format, derived fallback, demo mode, dashboard.
