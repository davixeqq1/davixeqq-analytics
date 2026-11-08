# Setup guide

## 0. Try it first without credentials (demo mode)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m yt_report demo
```

Demo mode runs the real pipeline on synthetic data: no Google or Discord calls, a separate
database in `data/demo/`, and a Discord payload preview written to `data/demo/discord_preview.json`.

## 1. Google Cloud project

1. Open <https://console.cloud.google.com/> → project picker → **New project** (e.g. `yt-report`).
2. **APIs & Services → Library** → enable:
   - **YouTube Data API v3**
   - **YouTube Analytics API**
3. **APIs & Services → OAuth consent screen** (Google Auth Platform):
   - User type: **External**. App name: `yt-report`. Support/developer email: yours.
   - Scopes: you can skip — the app requests them at login.
   - Audience → **Test users**: add the Google account that owns your channel.
   - **Publish app → In production.** Without this, refresh tokens expire after 7 days and the
     weekly job fails with `invalid_grant`. Verification is *not* needed for personal use; at login
     you will see "Google hasn't verified this app" → **Advanced → Go to yt-report (unsafe)**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app** → Create → **Download JSON**.
   - Save it as `credentials.json` in the project root. Never commit or share it.

## 2. Local configuration (Windows)

```powershell
cd C:\Users\<USERNAME>\Projects\youtube-report-bot
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
notepad .env          # set DISCORD_WEBHOOK_URL
python -m yt_report auth
python -m yt_report check
python -m yt_report run --dry-run
```

If the brand account / channel picker appears during login, choose **your** channel.

## 3. Discord webhook

Discord server → **Server Settings → Integrations → Webhooks → New Webhook** → name it, pick the
channel → **Copy Webhook URL** → put it in `.env`. Then `python -m yt_report test-discord`.

## 4. Optional: Claude recommendations

Requires an Anthropic API account with credits (a Claude Pro/Max subscription does **not**
include API credits). In `.env`:

```
AI_RECOMMENDATIONS=true
ANTHROPIC_API_KEY=<your key>
```

and `pip install -e ".[ai]"`. Any failure falls back to the free rule-based engine.

## 5. Windows Task Scheduler (optional)

Action: *Start a program*
- Program: `C:\Users\<USERNAME>\Projects\youtube-report-bot\.venv\Scripts\python.exe`
- Arguments: `-m yt_report run`
- Start in: `C:\Users\<USERNAME>\Projects\youtube-report-bot`
- Trigger: weekly, Tuesday 10:00 (reports the previous Sunday–Saturday week; earlier runs in the week select the week before).

## 6. Raspberry Pi

See README → *Raspberry Pi deployment*. Authorize on Windows (it needs a browser), then copy
`credentials.json` and `token.json` to the Pi with `scp`. Both are `chmod 600` by `install.sh`.

## 7. GitHub

```powershell
git add .
git status            # verify: no .env, credentials.json, token.json, data/, logs/
git commit -m "Initial commit"
git remote add origin https://github.com/<you>/youtube-report-bot.git
git push -u origin main
```

Never put `token.json`, `credentials.json` or the webhook URL into a public repository or its
Actions secrets for a scheduled workflow — Actions logs of public repositories are public. If you
automate the report on GitHub, do it in a private repository (`YT_TOKEN_JSON` = contents of
`token.json`). See [SECURITY.md](../SECURITY.md).
