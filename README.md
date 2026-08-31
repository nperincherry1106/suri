# Suri (SAI)

A personal AI assistant that handles small life-logistics tasks through Telegram. Built for one user (myself); currently provate repo but might share publicly at some point so others can fork, learn from, or improve.

This started as an SMS-first subscription canceller (see [`PLAN.md`](./PLAN.md) for the original plan) and pivoted into a Telegram bot with Outlook integration after Twilio compliance wouldn't let me send messages without registering as a business.

## What Suri can actually do today

Verified end-to-end:

- **Triage your Outlook inbox** — pulls recent mail, groups it into urgent / waiting on you / FYI / noise.
- **Summarize email threads** — fetches the full conversation and gives you the gist.
- **Draft replies** — writes a draft into your Outlook Drafts folder. You open Outlook and click Send. Suri never auto-sends mail.
- **Soft-delete emails** — single or batch (with explicit confirmation), recoverable from Deleted Items for ~30 days.
- **Find marketing senders + unsubscribe** — scans your inbox for `List-Unsubscribe` headers, then tries three methods in order: RFC 8058 one-click POST → headless-browser confirm-button click (Playwright) → hand the URL back to you.
- **Block senders + create inbox rules** — `block_sender` is a one-line wrapper around the more general `create_inbox_rule`, which can move/copy/flag/mark-read/delete/forward by sender, subject, body, attachments, importance, etc.
- **Find paid subscriptions** — scans your inbox for receipts/renewals/invoices to estimate what you're paying for. Detection only; doesn't cancel.
- **Outlook escape hatch** — `outlook_graph` lets the agent compose any Microsoft Graph call within the granted scopes, so new Outlook capabilities don't always need new code.
- **Long-term memory** — `remember_fact` / `forget_fact` so you can teach Suri your preferences ("I hate phone calls", "never unsubscribe me from USPS") and they stick across conversations.
- **Scheduled reminders** — `set_reminder` + APScheduler. Suri proactively pushes the reminder to your Telegram at the right time.
- **Honesty infrastructure** — every system prompt includes a "ground truth" block read from the SQLite DB (what's actually been unsubscribed, what reminders are pending) so the agent can't fabricate past actions.

## What Suri can't do yet

- Cancel paid subscriptions (the original v0 ask — needs per-service browser flows)
- Calendar (would need `Calendars.ReadWrite` scope and tools)
- Voice notes (Telegram → Whisper → Suri)
- Multi-user / sharing
- Cloud deploy / always-on (currently runs from your laptop)

## Architecture

```
┌──────────────────┐
│  Telegram (you)  │   <-- you message Suri from your phone
└────────┬─────────┘
         │  long-poll
┌────────▼─────────────────────────────────┐
│  app/telegram_bot.py                     │
│   - allowlist (your user_id only)        │
│   - markdown stripper for plain text     │
│   - registers push callback for reminders│
└────────┬─────────────────────────────────┘
         │
┌────────▼──────────────────────────────────────┐
│  app/agent.py — Claude Sonnet 4.5 + tools     │
│   - persona (slim, general principles)        │
│   - ground-truth injection per turn           │
│   - tool dispatcher                           │
└────────┬──────────────────────────────────────┘
         │
   ┌─────┴────┬──────────┬───────────┬─────────────┐
   ▼          ▼          ▼           ▼             ▼
outlook.py  memory.py  reminders.py  db.py    scheduler.py
(MS Graph)  (SQLite)   (SQLite +     (SQLite)  (APScheduler)
            user_facts  scheduler)              + push callback
```

**Stack:** Python 3.11, Anthropic SDK (Sonnet 4.5), `python-telegram-bot`, MSAL (Outlook OAuth), Microsoft Graph API, Playwright (headless Chromium for unsubscribe-page automation), APScheduler, SQLite (stdlib).

**Storage (gitignored):**
- `.env` — all secrets
- `outlook_token.json` — MSAL token cache
- `data/sai.db` — messages, user_facts, reminders, marketing_senders, paid_subscriptions, pending_actions

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# fill in: ANTHROPIC_API_KEY, MS_CLIENT_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID
```

### Telegram bot

1. DM `@BotFather` on Telegram → `/newbot` → follow prompts. You get a token. Put it in `TELEGRAM_BOT_TOKEN`.
2. DM `@userinfobot` on Telegram. It tells you your numeric user id. Put it in `TELEGRAM_USER_ID`. Suri will only respond to messages from this id.

### Microsoft Outlook (Azure app registration)

1. https://portal.azure.com → Microsoft Entra ID → App registrations → New registration.
2. **Name**: anything (e.g. "Suri").
3. **Supported account types**: "Personal Microsoft accounts only" (or both, if you have a work account too).
4. **Redirect URI**: Public client/native → `http://localhost`.
5. After creating, open the app → **Manifest** → set `"accessTokenAcceptedVersion": 2` (instead of `null`).
6. Open **API permissions** → Add a permission → Microsoft Graph → Delegated → check:
   - `Mail.ReadWrite`
   - `MailboxSettings.ReadWrite` (required for inbox rules)
7. Grant admin consent (or just consent on first auth).
8. Copy the **Application (client) ID** into `MS_CLIENT_ID` in `.env`.

The first time Suri makes an Outlook call, MSAL will pop a browser tab for you to authorize. After that, the token cache (`outlook_token.json`) persists.

## Run

```bash
python -m app.telegram_bot
```

Now message your bot on Telegram. Suri only responds to your allowlisted user id; everyone else is silently ignored.

A CLI interface for development is also available:

```bash
python -m app.cli
```

## Project layout

```
app/
  agent.py           # persona, tools list, agent loop with Claude
  telegram_bot.py    # Telegram transport, markdown stripper, push channel
  cli.py             # terminal REPL for dev
  scheduler.py       # APScheduler wrapper, push callback registration
  db.py              # SQLite schema + helpers
  tools/
    outlook.py       # all Microsoft Graph calls (~1100 lines)
    memory.py        # remember_fact / forget_fact
    reminders.py     # set/list/cancel reminders
data/
  sai.db             # gitignored
PLAN.md              # original SMS-first plan, kept for historical context
README.md            # this file
.env.example         # copy to .env and fill in
```

## Honesty design notes

A recurring problem with LLM agents is they'll *say* they did something without actually calling the tool. Suri has three layers of defense:

1. **Hard rules in the persona** — "if you SAY an action happened, you MUST have called the matching tool in this same turn and seen `ok:true`."
2. **Ground-truth injection** — every system prompt includes a freshly-queried block of "what has actually happened" (which senders are unsubscribed, what reminders are pending, what facts are remembered). This overrides Suri's recollection if conversation history disagrees with the database.
3. **Verbose tool logging to stderr** — every tool call and its result is logged so the user can verify when in doubt.

The `unsubscribe_from` tool is also explicitly honest about uncertainty: it distinguishes "request sent, server ack'd" (RFC 8058 one-click POST) from "page text confirmed" (browser saw success language). The persona instructs Suri to say "request sent, should stop in 1-7 days" rather than "unsubscribed" for the unverified case.

## Contributing

This is a personal weekend project, not a production system. If you want to fork it as a starting point or send a PR with an improvement, go for it. Things that would be especially useful:

- **Cloud deployment** — Dockerfile + fly.io config, plus switching MSAL to device-code flow so Outlook auth works on a headless server.
- **Calendar tools** — `Calendars.ReadWrite` scope plus list/find/create/cancel events.
- **Skills system** — Anthropic-style markdown-skills the agent can read on-demand and (carefully) author. Was scoped but punted as too much complexity for now.
- **Subscription cancellation** — the original v0 goal. Per-service Playwright flows for Netflix, Spotify, NYT, etc.
- **Better email triage** — sender-importance signals (CRM-style), conversation-thread state ("you owe a reply"), VIP rules.

## License

MIT (do whatever you want with it).
