# Suri (SAI)

A personal AI assistant that handles small life-logistics tasks. Built for one user; shared publicly so others can fork, learn from, or improve.

Suri started as an SMS subscription canceller ([`PLAN.md`](./PLAN.md)), became a **Telegram bot** with Outlook integration, and is now a **Telegram-first life-admin agent** deployed on [Fly.io](https://fly.io) — with an iOS app in progress as a future surface. Chat today is Telegram; the backend also exposes OAuth/Plaid webhooks and a growing `/api/v1/*` for the iOS client.

**What's next:** [ROADMAP.md](./ROADMAP.md)

## What Suri can do today

Verified end-to-end via Telegram:

- **Triage your Outlook inbox** — urgent / waiting on you / FYI / noise
- **Summarize threads + draft replies** — drafts land in Outlook; Suri never auto-sends mail
- **Soft-delete emails** — batch deletes with Yes/No confirmation in Telegram
- **Marketing unsubscribe** — RFC 8058 one-click → Playwright confirm → hand you the URL
- **Block senders + inbox rules** — move, flag, forward, auto-delete by sender/subject/etc.
- **Find paid subscriptions (email)** — receipt/renewal detection from inbox; no cancel yet
- **Plaid (read-only)** — connect real banks via Plaid Link; sync transactions + recurring charge streams. Multi-bank in one Link session. Production-ready on Fly.
- **Spending + recurring payments (Plaid)** — `plaid_sync_transactions`, `plaid_recurring` after linking
- **Long-term memory** — `remember_fact` / `forget_fact` for preferences
- **One-shot reminders** — `set_reminder` pushes to Telegram at the scheduled time
- **Recurring schedules** — `set_recurring_reminder` (daily/weekdays/custom) stored in SQLite; no redeploy needed. Includes `email_scan` action for nightly inbox digests.
- **Proactive briefs** — 7am morning, 12pm weekday nudge (if warranted), 9pm evening wrap
- **Honesty infrastructure** — ground-truth block in every turn so Suri can't fabricate past actions

## What Suri can't do yet

- Cancel paid subscriptions (needs per-service browser flows)
- Voice notes
- Multi-user / sharing

## Architecture

```
┌──────────────────┐
│  You (Telegram)  │
└────────┬─────────┘
         │  long-poll + push
┌────────▼─────────────────────────────────────────┐
│  app/telegram_bot.py (Fly production entrypoint) │
│   - Telegram transport + inline Yes/No confirms  │
│   - oauth_server.app (FastAPI on :8080)          │
│       /healthz, /plaid/*, /connect/*, /api/v1/*  │
│   - APScheduler (briefs, reminders, recurring)   │
└────────┬─────────────────────────────────────────┘
         │
┌────────▼──────────────────────────────────────┐
│  app/agent.py — Claude Sonnet 4.5 + tools     │
└────────┬──────────────────────────────────────┘
         │
   outlook.py  plaid.py  reminders.py  recurring_reminders.py
   memory.py   db.py     scheduler.py  proactive.py
```

**Stack:** Python 3.11, Anthropic SDK, FastAPI + uvicorn, python-telegram-bot, MSAL + Graph, Playwright, APScheduler, SQLite, Plaid Python SDK.

**Storage on Fly (`/data` volume):** `sai.db`, `outlook_token.json`, Plaid item tokens.

## Setup (local dev)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# fill in: ANTHROPIC_API_KEY, MS_CLIENT_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID
```

Set `SURI_HEADLESS=1` in `.env` so Outlook auth prompts go to Telegram instead of opening a browser in the background.

### Microsoft Outlook

See Azure app registration steps below (unchanged). For deployed Suri, register redirect URI:

`https://<your-app>.fly.dev/connect/outlook/callback`

and set `SURI_PUBLIC_URL` accordingly.

### Plaid

1. [Plaid Dashboard](https://dashboard.plaid.com) → **Developers → Keys** (Production or Trial plan for real banks)
2. Set on Fly: `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV=production`
3. **No dashboard webhook needed** for Transactions — Suri registers `https://<app>.fly.dev/plaid/webhook` automatically when you connect a bank
4. In Telegram: *"connect my bank"* → open the link → sign in through Plaid

Plaid user IDs are stored per environment (`plaid_user_id_production` vs sandbox). Switching from sandbox to production clears stale IDs automatically.

### Telegram

Create a bot via [@BotFather](https://t.me/BotFather). Set `TELEGRAM_BOT_TOKEN` and your numeric `TELEGRAM_USER_ID` (only this user can talk to Suri).

## Run

**Production (Fly):** `fly deploy -a suri` — Dockerfile runs `app.telegram_bot`.

**Local Telegram bot:**

```bash
python -m app.telegram_bot
```

**CLI dev REPL:**

```bash
python -m app.cli
```

**Alternative entrypoint** (FastAPI-only, no Telegram — used for iOS backend experiments):

```bash
python -m app.main
```

## Deploy

```bash
fly secrets set -a suri ANTHROPIC_API_KEY=... TELEGRAM_BOT_TOKEN=... # etc.
fly deploy -a suri
```

Pushes to `main` auto-deploy via `.github/workflows/fly-deploy.yml` if `FLY_API_TOKEN` is set in GitHub repo secrets.

Only run **one** Telegram poller at a time (local bot OR Fly — not both).

## Azure Outlook registration (summary)

1. https://portal.azure.com → App registrations → New registration
2. Personal Microsoft accounts; redirect `http://localhost` for dev
3. Add `https://<your-app>.fly.dev/connect/outlook/callback` for deploy
4. Manifest: `"accessTokenAcceptedVersion": 2`
5. API permissions: `Mail.ReadWrite`, `MailboxSettings.ReadWrite`, `Calendars.ReadWrite`
6. Copy client ID → `MS_CLIENT_ID`

## Project layout

```
app/
  telegram_bot.py       # production entrypoint (Telegram + OAuth + scheduler)
  agent.py              # Claude agent loop + tools
  oauth_server.py       # FastAPI: OAuth, Plaid Link pages, webhooks, /api/v1
  proactive.py          # morning/evening briefs, daily email scan
  scheduler.py          # APScheduler + reminder/recurring restore
  db.py                 # SQLite schema
  tools/
    outlook.py          # Microsoft Graph
    plaid.py            # Plaid Link, sync, recurring
    reminders.py        # one-shot reminders
    recurring_reminders.py  # repeating schedules (from Telegram, no deploy)
    memory.py
fly.toml                # Fly.io config (suri.fly.dev)
.github/workflows/      # auto-deploy on push to main
ios/                    # native app (in progress)
```

## Honesty design notes

1. **Hard rules in the persona** — must call the tool and see `ok:true` before claiming an action happened
2. **Ground-truth injection** — unsubscribes, reminders, recurring schedules, Plaid items from DB every turn
3. **Tool logging to stderr** — every call logged for verification

## License

MIT
