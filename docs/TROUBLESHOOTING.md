# Troubleshooting

Start with `python -m yt_report check` — it validates the configuration without printing secrets.

## Exit codes

| Code | Meaning | First thing to check |
|---|---|---|
| 0 | OK (also: period already sent, nothing to do) | — |
| 1 | Unexpected error (incl. network timeouts after retries) | `logs/yt_report.log` |
| 2 | Configuration invalid (webhook, timezone, numbers) / demo DB would equal production DB | `.env` |
| 3 | Google authorization missing, expired or revoked | `python -m yt_report auth` |
| 4 | YouTube API error (quota, API disabled, permissions) | message in the Discord alert / log |
| 5 | Discord delivery failed (snapshot stored, will be sent on the next run) | webhook |
| 6 | YouTube returned no data for the period | wait or increase `DATA_LAG_DAYS` |
| 7 | SQLite error (nothing was sent) | disk space, permissions, `database is locked` |
| 8 | Another run is in progress — this run did nothing | Wait for the other run; if none is running, the OS has released the lock already, just run again |
| 9 | The last day(s) of the period have no Analytics data yet — nothing stored or sent | Run again later (Tuesday from 10:00 CET), or `--allow-incomplete` for a labelled partial report |

## Common problems

| Symptom | Cause | Fix |
|---|---|---|
| `No valid Google credentials` / exit 3 | `token.json` missing, revoked or expired | `python -m yt_report auth` on Windows; copy `token.json` to the Pi / update the `YT_TOKEN_JSON` secret. |
| Works for a week, then `invalid_grant` | OAuth consent screen still in **Testing** (7-day refresh tokens) | Publish the app to **In production**, then re-run `auth`. |
| `access_denied` / "app not verified" blocked | Account not in test users | Add your Google account as a test user or publish the app. |
| `HTTP 403: ... has not been used in project` | API not enabled | Enable YouTube Data API v3 **and** YouTube Analytics API in the same project as `credentials.json`. |
| `HTTP 403: quotaExceeded` | Daily quota spent | Not retried. Wait until midnight Pacific time; one run uses ~13 Analytics requests and ~2 Data API units per 50 uploads. |
| `HTTP 403 rateLimitExceeded` in the log | Short burst limit | Retried automatically with backoff. |
| `HTTP 403: Forbidden` on analytics | Account doesn't own/manage the channel, or wrong brand account | Re-run `auth` and pick the right channel. |
| Exit 6 "No analytics data for the reporting period" | Data not processed yet | Run later or set `DATA_LAG_DAYS=4`. Nothing was stored or sent. |
| Note "No data for the previous period" | New channel or missing history | Comparisons show `n/a`; expected. |
| Note "No daily rows returned for N day(s): …" | Days without rows in the 14-day series (not processed yet, or no activity) | Normal for the last day on early runs; re-run later with `--force-send` if needed. |
| Formats labelled **derived** | The per-format API query failed | Values come from per-video rows: views exact, AVD weighted by engaged views, avg % viewed `n/a`, watch time may be a few % low (per-video minute rounding). |
| Note "Per-video rows … account for X of Y views (…): N views not assigned to any video by the API" / Discord "N unassigned by API" | YouTube counts these views in the format total but returns them for no video (verified with per-day queries) | Nothing to fix; the week stays incomplete. Format totals are exact. |
| Note "… missing from a query that hit the API limit of 200 rows" / "API cap 200" | A format had more than 200 videos with views (confirmed cap) | Rows are the top 200 of that format; totals stay exact. |
| Note "… possibly truncated (suspected, not confirmed)" / "truncation suspected" | The single-query fallback returned ≥ 190 rows while views are missing | The fallback was used because format queries failed; check the log for the HTTP error. |
| Note "… cause not identified" | Coverage is incomplete and no measured cause applies | Re-run later; if it persists, compare format totals with the per-format queries. |
| Note "Per-video rows … exceed the API format totals" / Discord "exceed the format totals" | A format query returned more views than its format total (inconsistent API data) | Rows are kept, the week is not marked complete. Usually resolves after YouTube finishes processing. |
| Note "Coverage … includes N views of posts" | Views of community posts are part of the format totals | Counted for coverage only; posts never appear in top/weakest videos. |
| Note "format query failed for …" / "single-query fallback" | One or more `creatorContentType` queries failed | Successful formats are kept; failed ones come from the single query when possible. If it happens every week, Google may have changed the filter. |
| Note "weakest-video check may have missed candidates" | A capped (or suspected-truncated) per-video query still had videos with ≥ `MIN_VIEWS_WEAKEST` views | Rare; the weakest video is chosen among the returned rows only. |
| Discord "Publication counts unavailable" | Uploads playlist not readable (Data API error) or the channel has no uploads | Check the log for the HTTP error; the rest of the report is unaffected and the views recommendation stays neutral. |
| "Publication counts incomplete (upload scan stopped at the 40-page limit)" | More than 2,000 uploads | Counts may be too low; the limit is `MAX_UPLOAD_PAGES` in `youtube_client.py`. |
| "Publication counts incomplete (some uploads returned no metadata)" | A playlist entry without metadata (deleted video) dated inside the period, or with no date | Deleted videos are never counted; re-run later. Deleted videos dated outside the period do not affect completeness. |
| "N new upload(s) not classified by format" | New upload without views yet, so Analytics has no `creatorContentType` | Expected right after publishing; the format is never guessed from duration. |
| Published count differs from YouTube Studio | Only **public** uploads are counted, by Pacific date; livestreams by actual start; private/unlisted/scheduled excluded | See `docs/ARCHITECTURE.md` → Publication statistics. |
| `Database schema version N is newer than this program supports` | Database written by a newer version | Update the program; the database is not modified. |
| Formats labelled **unavailable** | Neither the per-format nor the per-video format query worked | The rest of the report is unaffected. |
| Note "Format breakdown sums to X views but the channel total is Y" | YouTube still processing some content types | Re-run later. |
| Format trends missing, note "different method" | One period was API, the other derived | Intended: different methods are not compared. |
| HTTP 400 for `video,creatorContentType` | The API rejects this combination without a video filter | The current version always adds a `video==…` filter. |
| "Weakest video: Not enough data" | < 3 videos per format with ≥ 30 views | Expected for small weeks; adjust `MIN_VIEWS_WEAKEST` / `MIN_VIDEOS_PER_FORMAT` (min 2). |
| Exit 2 "DISCORD_WEBHOOK_URL is not configured" | URL missing, placeholder, or not a Discord webhook | Only `https://discord.com/api/webhooks/…` (or discordapp.com / canary / ptb) is accepted. |
| Discord `HTTP 401/404` | Webhook deleted or URL wrong | Create a new webhook, update `.env` / secret. |
| Discord `HTTP 400` | Embed rejected | Check `logs/yt_report.log`. |
| "already sent … use --force-send" | Duplicate protection | Intended. Use `--force-send` to post again. |
| Exit 7 / `database is locked` | Disk problem, or a non-`run` process writing the DB | Check free space; concurrent `run`s are prevented by the run lock (exit 8). |
| Exit 8 "Another yt-report run is in progress" | A scheduled or manual `run` holds `data/yt_report.lock` | Normal protection against duplicate reports. The lock file may stay on disk; it is free when no run is active. |
| Discord overview "⚠ Reporting week realigned" / note "This report's periods overlap …" | A stored report (report #1, Oct 29 – Nov 4) overlaps this report's weeks after the switch to Sunday–Saturday | Expected once. The comparison uses the API's previous 7 days, not the earlier report. |
| Dashboard history "⚠ overlaps …" | Two stored reports are not consecutive weeks | Informational; each row's "vs prev" still compares with its own previous 7 days. |
| Discord data notes end with "… +N more notes on the dashboard" | More notes than fit in Discord's 1,024-character field | Open the dashboard → Data notes; all notes are stored. |
| Exit 9 "Reporting period not complete yet" | Run before YouTube processed the last days of the week (e.g. Sunday with `--end-date` of the day before) | Wait; scheduled Tuesday runs have 3 days of processing. For an interim view: `run --dry-run --allow-incomplete --print`. |
| "PARTIAL Weekly Report (N/7 days)" | Built with `--allow-incomplete` | Values cover only the stated days; no comparisons or recommendations. Not stored; the complete report follows normally. |
| `--send-partial requires --allow-incomplete` (exit 2) | `--send-partial` used alone | Add `--allow-incomplete`; a partial report is only ever sent when both flags are given (and not with `--dry-run` / `--no-discord`). |
| Discord overview "No valid previous comparison" | YouTube returned no data for the previous period | Comparisons show `n/a`; expected for new channels or after long gaps. |
| Dashboard "No reports yet" | No snapshot stored | `python -m yt_report run --no-discord`, or `python -m yt_report demo`. |
| Dashboard "Could not load data" | Server stopped, or DB unreadable (HTTP 500) | Restart `python -m yt_report dashboard`; check the log. |
| Dashboard "could not listen on 127.0.0.1:8050" (exit 2) | Port in use or not permitted | `--port 8060` or set `DASHBOARD_PORT`. |
| `argument --port: must be between 1 and 65535` / `DASHBOARD_PORT must be <= 65535` (exit 2) | Invalid port | Use a port from 1 to 65535. |
| Dashboard answers `403 Forbidden: unexpected Host header` | Opened through a host name other than `localhost` / `127.0.0.1` while bound to loopback | Use `http://localhost:8050/`. For LAN access set `DASHBOARD_HOST` explicitly (no login — trusted networks only). |
| "Charts unavailable" in dashboard | No internet (Chart.js loads from a CDN) | Values and tables still render. |
| Demo shows old weeks | Demo builds the 8 weeks before today | Re-run `python -m yt_report demo`. |
| Report covers the week before the one expected | Weeks end on Saturday (Pacific) and need `DATA_LAG_DAYS` (3) days of processing; before Tuesday (Pacific) the latest complete week is the previous one | Run on Tuesday from 10:00 CET, or pass `--end-date YYYY-MM-DD` explicitly. |
| Warning "Explicit --end-date … is not the configured week end" | `--end-date` is not a Saturday | Informational: the given period is used exactly; use a Saturday to stay aligned with the weekly series. |
| `REPORT_WEEK_END must be a weekday name` | Invalid value in `.env` | Use e.g. `saturday`, `sat`, or `none` for the rolling 7-day window. |
| Cron does nothing on Pi | Wrong path / permissions / timezone | `bash deploy/pi/run_report.sh --dry-run`; check `logs/cron.log`; `chmod +x deploy/pi/*.sh`; `timedatectl`. |
| PowerShell blocks `Activate.ps1` | Execution policy | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or call `.venv\Scripts\python.exe` directly. |

Logs: `logs/yt_report.log` (rotated, secrets redacted) and on the Pi also `logs/cron.log`.
