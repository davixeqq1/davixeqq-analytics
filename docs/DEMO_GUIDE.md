# Demo guide (3–5 minutes)

Goal: show a working automation, explain one real engineering problem, and show that the numbers
can be trusted — without exposing credentials or private channel data.

## Before the meeting

```powershell
cd C:\Users\<USERNAME>\Projects\youtube-report-bot
.\.venv\Scripts\Activate.ps1
pytest -q                                   # Windows: "336 passed, 1 skipped"; Linux/macOS: "337 passed"
python -m yt_report demo --no-dashboard     # pre-builds the synthetic history
```

Close any window that shows `.env`, `token.json` or `credentials.json`. Use demo mode on a shared
screen; switch to real data only if you choose to show it.

Have open: the repository in the editor, a terminal, `docs/ARCHITECTURE.md`.

## Script

### 0:00 – 0:30 · The problem
> "Every Monday I wanted to know how my channel did versus the previous week, separately for
> Shorts, long videos and livestreams, and what to do next. Doing that by hand in YouTube Studio
> is repetitive and inconsistent, so I automated it."

### 0:30 – 1:30 · The dashboard (open first)
```powershell
python -m yt_report demo
```
Point at:
1. **Demo banner** — "synthetic data; the real pipeline runs, only the API transport is replaced."
2. **KPI row** with week-over-week deltas, and the note under it: channel-wide averages depend on the format mix.
3. **Formats card** — Shorts / long-form / livestream shares and the source badge (`API` / `derived` / `synthetic`).
4. **Top videos and weakest video** — "weakest is judged against the median of its own format."
5. **Recommendations** — deterministic rules that quote the numbers they use.
6. **History** and the **"What do these metrics mean?"** glossary — written for non-technical readers.

### 1:30 – 2:15 · Automated reporting
- Open `data/demo/discord_preview.json`: "this is exactly the Discord payload — in demo mode it is
  written to a file, never sent."
- Explain the production run: `python -m yt_report run` on a Raspberry Pi cron every Tuesday (Sunday–Saturday weeks);
  SQLite stores one snapshot per period and a sent-flag prevents duplicate posts; GitHub Actions
  runs the test suite.

### 2:15 – 3:15 · API integration and the fallback (the interesting part)
Open `docs/ARCHITECTURE.md` → *Fallback mechanism* / *Metric definitions*.
> "The first real run showed 0 views for Shorts. The API returns `shorts`, `videoOnDemand`,
> `liveStream` in camelCase — I normalised them and kept livestreams separate. The top-videos query
> with the format dimension returned HTTP 400; by probing the live API I found it works only with an
> explicit video filter.
> Then I audited the metrics: average view duration is not watch time divided by views — that gives
> less than half of the real value. It is weighted by engaged views. I verified it against the API and used it
> in the fallback, which I tested by forcing the fallback on real data: the derived values matched
> the API within a second per format."

### 3:15 – 4:00 · Architecture and quality
- Show the Mermaid diagram: collection → processing → validation → reporting → Discord / dashboard.
- Run `pytest -q` (or show the last result): 337 tests, no network.
- Mention security: read-only OAuth scopes, secrets only in git-ignored files, redacted logs, webhook allow-list.

### 4:00 – 5:00 · How this transfers
> "The same pattern — API extraction, validation, comparison, rules, chat notification, dashboard —
> fits internal reports like sales KPIs or support metrics. Next steps I would take: an MCP server
> over the stored history, or triggering it from n8n / Make."

## Technical decisions worth discussing

| Topic | Talking point |
|---|---|
| Source labelling | Every figure is `api`, `derived` or `unavailable`; missing is never zero. |
| Notes from final state | Warnings describe the final report, not intermediate failures. |
| Per-format retention | Channel averages move with the format mix; retention is compared format vs same format. |
| Idempotency | Upsert per period + sent-flag → cron retries are safe. |
| Demo isolation | Separate DB, synthetic services, no Discord, refuses to share the production DB. |
| Deterministic rules vs LLM | Rules are free and testable; the optional Claude mode only rephrases verified metrics. |
| Honest limits | 200-video API cap, per-row minute rounding, 2–3 day data lag. |

## If something goes wrong during the demo

| Symptom | Fix |
|---|---|
| Port 8050 busy | `python -m yt_report demo --port 8060` |
| No charts (offline) | Tables and values still show; Chart.js loads from a CDN. |
| Browser does not open | Open `http://localhost:8050` manually. |
