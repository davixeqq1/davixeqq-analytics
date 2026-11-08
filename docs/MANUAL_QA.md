# Manual QA protocol

A test protocol for an external reviewer who has only the public repository. Every command
below exists in the project; every expected result was observed on the release version. Part A
needs **no credentials and no network access to YouTube or Discord**. Part B is optional: it
needs *your own* Google account with a YouTube channel (and optionally your own Discord
webhook). Nothing in this protocol needs access to the author's accounts.

Record each step as PASS / FAIL / SKIPPED. Commands are shown for Linux/macOS (`bash`); Windows
PowerShell differences are noted where they matter.

## Prerequisites

| Item | Needed for | Check |
|---|---|---|
| Python 3.11 or newer | everything | `python --version` (Windows: `py -3 --version`) |
| Git | cloning | `git --version` |
| Node.js (optional) | the 3 JavaScript render tests; skipped without it | `node --version` |
| A modern browser | dashboard checks | — |
| `curl` | HTTP checks (A5, A9) | `curl --version` (built into Windows 10+ as `curl.exe`) |
| SQLite CLI or Python | data integrity checks (A7) | Python's `sqlite3` module is enough |

Use a fresh terminal without `DISCORD_WEBHOOK_URL`, `YT_TOKEN_JSON` or `ANTHROPIC_API_KEY` set in
the environment (`env | grep -E 'DISCORD|YT_|ANTHROPIC'` prints nothing).

---

## Part A — without credentials

### A1. Clean installation

```bash
git clone https://github.com/<owner>/<repository>.git yt-report-qa
cd yt-report-qa
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Expected:
- Installation finishes without errors; `pip show yt-report` reports version `1.2.0`.
- `git status --short` prints nothing (installation creates only git-ignored files such as
  `src/yt_report.egg-info/` and `.venv/`).
- No `.env`, `credentials.json`, `token.json`, `data/` or `logs/` exists in the clone
  (`ls -a`).

### A2. Automated tests

```bash
pytest -q
```

Expected:
- Linux/macOS with Node.js: `337 passed`.
- Windows with Node.js: `336 passed, 1 skipped` (the skipped test checks POSIX file modes).
- Without Node.js: 3 more tests are reported as skipped (`node is not installed`).
- No test needs network access; the run takes well under a minute.

`pytest -q -rs` lists the reason for every skipped test.

### A3. Application startup and CLI

```bash
python -m yt_report --help
yt-report --help                     # console script installed by pip
```

Expected: both print the usage with the commands `run, auth, dashboard, demo, test-discord, check`
and exit with code 0.

### A4. Demo mode without credentials

```bash
python -m yt_report demo --no-dashboard --print
echo $?                              # Windows: $LASTEXITCODE
```

Expected:
- First line: `DEMO MODE — synthetic data, not real channel analytics. No API calls, nothing sent to Discord.`
- `8 synthetic weekly reports stored in …/data/demo/demo.db` and
  `Discord payload preview (not sent): …/data/demo/discord_preview.json`.
- A summary of the latest synthetic week: channel name `Demo Channel (synthetic data)`, seven
  KPIs with previous values and % change, one line per format (`[Shorts]`, `[Long-form]`,
  `[Livestream]`), top videos titled `Demo Short #…` / `Demo Video #…`, a weakest video,
  coverage and views-by-upload-age lines, and recommendations marked `[rules]`.
- Exit code `0`.
- Only `data/demo/` was created — no `data/yt_report.db`, no `token.json`, nothing outside the
  project directory.
- `data/demo/discord_preview.json` is a JSON object with the keys `_note` and `messages`.

Variants:

```bash
python -m yt_report demo --weeks 3 --no-dashboard      # 3 synthetic reports
python -m yt_report demo --weeks 0 --no-dashboard      # rejected
```

Expected: the first stores `3 synthetic weekly reports`; the second prints
`argument --weeks: must be between 1 and 104, got 0` and exits with code 2.

### A5. Dashboard (demo data)

```bash
python -m yt_report demo             # builds the demo data and opens http://localhost:8050/
```

If no browser opens, open `http://localhost:8050/` manually. Keep the server running for A5 and
A9; stop it with Ctrl+C.

Check in the browser:

| # | Check | Expected |
|---|---|---|
| 1 | Header | Yellow **DEMO** banner "Synthetic data generated locally — not real channel analytics"; browser tab title starts with `DEMO` |
| 2 | Week selector | Lists 8 weeks (Sunday–Saturday); switching week updates every section |
| 3 | Section navigation | Overview · Formats · Trends · Videos · Data quality · History; each link scrolls to its section |
| 4 | Overview | 7 KPI cards (views, watch time, net / gained / lost subscribers, avg view duration, avg % viewed) with change and previous value; values equal the `--print` summary of the same week |
| 5 | Daily views | Bars for this week and the previous 7 days |
| 6 | Formats | Share of views and watch time per format; per-format table including **Published** |
| 7 | Weekly trends | Three line charts with one point per stored week |
| 8 | Videos | Top 3, weakest video with its format median, recommendations, and the video explorer: format filter chips, sortable columns, first 10 rows plus a "Show all" button |
| 9 | Data quality | Coverage table ("Complete", 100.0 %), views by upload age (new uploads vs older catalog), data notes |
| 10 | History | One row per stored week |
| 11 | Mobile layout | Narrow the window to ~390 px (or use the browser's device toolbar): no horizontal scrolling, sections stack |
| 12 | Keyboard | Tab moves through navigation, week selector, filter chips and sort buttons with a visible focus ring |
| 13 | Offline charts | With the network disabled (Chart.js comes from a CDN) the page still shows tables and a notice instead of charts |

HTTP checks while the server runs (second terminal):

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8050/                 # 200
curl -s http://localhost:8050/api/data | head -c 120; echo                        # JSON with "reports"
curl -s -D - -o /dev/null http://localhost:8050/ | grep -iE "x-frame-options|x-content-type|referrer|cache-control"
```

(The dashboard serves GET only; `curl -I` sends HEAD and gets `501`, which is expected.)

Expected: `200`; JSON that contains `"mode": "demo"`; headers `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.

### A6. Report generation and error scenarios (no credentials)

Run each command and compare the exit code (`echo $?`, Windows `$LASTEXITCODE`):

| # | Command | Expected message (stderr / log) | Exit |
|---|---|---|---|
| 1 | `python -m yt_report check` | Checklist; `[!!] OAuth client file`, `[!!] OAuth token`, `[!!] Discord webhook`, `[OK] Database directory writable`; no secret values | 1 |
| 2 | `python -m yt_report run --dry-run` | `YouTube authorization required: No valid Google credentials. Run python -m yt_report auth …` | 3 |
| 3 | `python -m yt_report run --no-discord` | Same authorization message; nothing stored (`data/yt_report.db` is not created; only the empty run-lock file `data/yt_report.lock` appears) | 3 |
| 4 | `python -m yt_report test-discord` | `DISCORD_WEBHOOK_URL is not configured in .env` | 2 |
| 5 | `DISCORD_WEBHOOK_URL=https://example.com/api/webhooks/1/x python -m yt_report test-discord` | Same message — non-Discord hosts are rejected, nothing is sent | 2 |
| 6 | `DATA_LAG_DAYS=abc python -m yt_report check` | `Configuration error: DATA_LAG_DAYS must be an integer, got 'abc'` | 2 |
| 7 | `REPORT_TIMEZONE=Mars/Base python -m yt_report check` | `Configuration error: REPORT_TIMEZONE 'Mars/Base' is not a valid IANA timezone` | 2 |
| 8 | `REPORT_WEEK_END=someday python -m yt_report check` | `Configuration error: REPORT_WEEK_END must be a weekday name …` | 2 |
| 9 | `DASHBOARD_PORT=70000 python -m yt_report check` | `Configuration error: DASHBOARD_PORT must be <= 65535` | 2 |
| 10 | `YT_TOKEN_JSON='{bad' python -m yt_report run --dry-run` | `YouTube authorization required: YT_TOKEN_JSON is not valid JSON.` | 3 |
| 11 | `python -m yt_report run --send-partial --dry-run` | `--send-partial requires --allow-incomplete.` | 2 |
| 12 | `python -m yt_report dashboard --port 70000` | `argument --port: must be between 1 and 65535, got 70000` | 2 |
| 13 | Start `python -m yt_report demo` in one terminal, then `python -m yt_report dashboard --port 8050 --no-browser` in a second | `Dashboard could not listen on 127.0.0.1:8050 (…). Is the port already in use? …` — no traceback | 2 |

On Windows PowerShell set a variable for one command like this:
`$env:DATA_LAG_DAYS='abc'; python -m yt_report check; Remove-Item Env:DATA_LAG_DAYS`.

The partial-report logic, missing API data, API failures (HTTP 403 / 429 / 5xx, timeouts) and
authentication failures (expired or revoked tokens) cannot be triggered without a Google
account; they are covered by automated tests: `pytest -q tests/test_partial_report.py
tests/test_clients_and_pipeline.py tests/test_auth.py tests/test_final_regressions.py`.

### A7. Data integrity

```bash
python -m yt_report demo --no-dashboard          # run twice: snapshots are upserted, not duplicated
python -m yt_report demo --no-dashboard
python - <<'EOF'
import sqlite3
c = sqlite3.connect("data/demo/demo.db")
print("reports:", c.execute("SELECT COUNT(*) FROM reports").fetchone()[0])
print("integrity:", c.execute("PRAGMA integrity_check").fetchone()[0])
print("schema:", c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
print("duplicates:", c.execute("SELECT COUNT(*) FROM (SELECT period_start, period_end FROM reports "
                               "GROUP BY channel_id, period_start, period_end HAVING COUNT(*) > 1)").fetchone()[0])
EOF
```

Expected: `reports: 8`, `integrity: ok`, `schema: 2`, `duplicates: 0`.
(Windows PowerShell has no heredoc: save the Python lines to `check_db.py` and run `python check_db.py`.)

Also check:
- The dashboard's KPI values for a week equal the `--print` summary for the same week.
- Per-format views add up to the channel views in the demo week (Formats table vs Overview).
- Missing values are shown as `n/a`, never as `0`.

### A8. Database recovery

```bash
python -m yt_report demo --no-dashboard
cp data/demo/demo.db demo-backup.db                       # Windows: copy
printf 'not a database' > data/demo/demo.db               # simulate corruption (PowerShell: Set-Content data/demo/demo.db 'not a database')
python -m yt_report demo --no-dashboard                    # expected to fail on the corrupt file
```

Expected: `Demo database could not be written (DatabaseError: file is not a database). Delete or
restore data/demo/demo.db and run the demo again.`, exit code `7`, no traceback, and the corrupt
file is left as it was (not silently replaced). Then restore and verify:

```bash
cp demo-backup.db data/demo/demo.db
python -m yt_report demo --no-dashboard                    # works again, 8 reports
```

Dashboard on a corrupt database: point `DATABASE_PATH` at a corrupt file and start
`python -m yt_report dashboard --no-browser`; `http://localhost:8050/api/data` returns HTTP 500
with `The local database could not be read. Check logs/yt_report.log.`, and the page shows its
error state. Replacing or deleting the file while the server keeps running must work (it is not
held open); the next request then succeeds.

### A9. Security checks

```bash
git status --short --ignored                               # only git-ignored local files are listed with !!
git check-ignore -v .env credentials.json token.json data/yt_report.db logs/yt_report.log
grep -nE "REPLACE_ME|=$" .env.example                      # placeholders / empty values only
```

Expected:
- Every path in the `check-ignore` command is reported with the `.gitignore` rule that ignores it.
- `.env.example` contains only placeholders (`REPLACE_ME`) or empty values for secrets.

Dashboard (with `python -m yt_report demo` running):

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: evil.example" http://127.0.0.1:8050/api/data   # 403
curl -s -o /dev/null -w "%{http_code}\n" --path-as-is http://localhost:8050/static/../../config.py  # 404
```

Expected: `403` (foreign Host header — DNS-rebinding protection) and `404` (path traversal blocked).
In the browser, open the developer tools → Network: the only external request is Chart.js from
`cdn.jsdelivr.net`; there are no requests to YouTube, Google or Discord in demo mode.

Optional secret scan of the clone (if [Gitleaks](https://github.com/gitleaks/gitleaks) is installed):

```bash
gitleaks dir . --redact
```

Expected: `no leaks found`. The tests contain deliberately fake credential-like strings (for
example `GOCSPX-…`, `ya29.…` in `tests/test_clients_and_pipeline.py`) used to test log redaction;
they are not real secrets.

---

## Part B — with your own Google account (optional)

These steps use **your own** YouTube channel and Google Cloud project. Follow
[SETUP.md](SETUP.md) for steps 1–2.

| # | Step | Expected |
|---|---|---|
| B1 | Create a Google Cloud project, enable *YouTube Data API v3* and *YouTube Analytics API*, create an OAuth client of type **Desktop app**, save it as `credentials.json` in the project root | `git status --short` still prints nothing (the file is ignored) |
| B2 | `python -m yt_report auth` | Browser consent for read-only scopes; `Authorization successful; token saved to token.json`; on Linux/macOS `ls -l token.json` shows `-rw-------` |
| B3 | `python -m yt_report check` | `[OK] OAuth client file`, `[OK] OAuth token`; Discord `[!!]` until B6 |
| B4 | `python -m yt_report run --dry-run --print` | Summary of the last complete Sunday–Saturday week vs the week before; coverage lines (normally `via by_format`); nothing stored (`data/yt_report.db` not created), nothing sent, `token.json` unchanged |
| B5 | `python -m yt_report run --no-discord --print`, then `python -m yt_report dashboard` | Snapshot stored in `data/yt_report.db`; the dashboard shows it without the DEMO banner. Running the same command again does not create a second row for the same week |
| B6 | Optional: create a webhook in a private Discord test channel, `cp .env.example .env`, set `DISCORD_WEBHOOK_URL`, run `python -m yt_report test-discord`, then `python -m yt_report run` | A test message, then the weekly report (overview, formats, videos, recommendations, data notes); a second `run` for the same week does not post again (use `--force-send` to resend) |
| B7 | `python -m yt_report run --dry-run --allow-incomplete --print --end-date <yesterday as YYYY-MM-DD>` | If the last days have no Analytics data yet: a `PARTIAL Weekly Report (N/7 days)` with the exact data range and no comparisons; without `--allow-incomplete` the run stops with exit 9 |
| B8 | Revoke access at <https://myaccount.google.com/permissions>, then `python -m yt_report run --dry-run` | `OAuth refresh failed …` then exit 3 with the re-authorization hint; no traceback, no secret in the output or in `logs/yt_report.log` |
| B9 | `grep -iE "ya29|1//|GOCSPX|webhooks/[0-9]" logs/yt_report.log` | No matches (only `[REDACTED…]` placeholders) |

After Part B: delete the webhook and remove the app's access in your Google account if you no
longer need them.

---

## Acceptance criteria

The project passes the manual review when:

1. A1–A4 succeed on a clean clone without any credentials, and `pytest -q` shows no failures.
2. Every row of A5 and A6 matches the expected result; no command prints a Python traceback.
3. A7 shows `integrity: ok`, no duplicates, and dashboard values equal the console summary.
4. A8 shows that a corrupt database is reported, not silently replaced, and can be restored.
5. A9 shows that secrets and local data are git-ignored, `.env.example` holds placeholders only,
   the dashboard rejects foreign Host headers and path traversal, and demo mode makes no calls to
   YouTube, Google or Discord.
6. Optional Part B works with the reviewer's own account; no secret appears in logs or output.
