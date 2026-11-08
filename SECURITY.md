# Security

## What is never in this repository

| Item | Where it lives | Protection |
|---|---|---|
| Google OAuth client (`credentials.json`) | your machine only | `.gitignore` (`credentials*.json`, `client_secret*.json`) |
| OAuth refresh/access token (`token.json`, `token.json.tmp`) | your machine only (`chmod 600` on Linux) | `.gitignore` (`token*.json`, `*.tmp`) |
| Discord webhook URL, Anthropic API key | `.env` on your machine | `.gitignore` (`.env`, `.env.*`); `.env.example` has placeholders only |
| Analytics history (`data/*.db`), logs, backups | your machine only | `.gitignore` (`data/`, `logs/`, `backups/`, `*.db*`) |

Numbers in the documentation and tests are synthetic, relative, or describe API behaviour (such as
row limits); no channel metrics are published. The screenshots come from demo mode (synthetic data).

## How the application handles secrets

- Read-only OAuth scopes (`youtube.readonly`, `yt-analytics.readonly`).
- Tokens are written atomically and created owner-only (`0600`) on Linux/macOS; `run --dry-run`
  never writes the token file.
- Every log line passes a redaction filter (webhook URLs, Google tokens, client secrets, API keys).
- Only real Discord webhook hosts are accepted (`https://discord.com/api/webhooks/…`).
- Discord failure alerts contain only the error type and a short, redacted message.
- The dashboard binds to `127.0.0.1`, escapes all API text, sends `nosniff` / `no-referrer` /
  `X-Frame-Options: DENY`, and on a loopback bind rejects requests whose `Host` header is not
  `localhost` / `127.0.0.1` / `[::1]` (HTTP 403, DNS-rebinding protection).
- The dashboard has **no authentication**. Binding it to another address (`DASHBOARD_HOST`,
  `--host`) makes the analytics readable by anyone on that network, and the Host check is then
  not applied — do this only on a trusted network.

## Rules for contributors and forks

1. Never commit `.env`, `credentials.json`, `token.json`, `data/`, `logs/` or `backups/` — check
   with `git status` and `git check-ignore -v <file>` before every commit.
2. Publish only with `git push`; never upload a ZIP of a working directory (it contains ignored files).
3. Use a GitHub *noreply* e-mail for commits (`git config user.email <id>+<user>@users.noreply.github.com`).
4. Do not run a scheduled reporting workflow in a public repository: Actions logs are public. This
   repository's CI only runs the test suite and needs no secrets.
5. If a secret was ever exposed (commit, log, chat, screenshot): revoke it immediately —
   delete/recreate the Discord webhook, reset the OAuth client secret in Google Cloud and remove
   the app's access at <https://myaccount.google.com/permissions>, then run `python -m yt_report auth` again.

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security → Report a vulnerability) instead of a
public issue. Do not include real tokens or webhook URLs in reports.
