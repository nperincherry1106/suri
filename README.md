# SAI

Personal SMS-first agent. Single user (the founder). v0's only job: cancel one subscription end-to-end via SMS approval.

Full plan and constraints: see [`PLAN.md`](./PLAN.md). Cursor working rules: [`.cursor/rules/sai.mdc`](./.cursor/rules/sai.mdc).

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# fill in ANTHROPIC_API_KEY, TWILIO_*, USER_PHONE
```

Gmail OAuth credentials: download `gmail_credentials.json` from Google Cloud Console (Desktop app, Gmail API enabled, scope `gmail.readonly`) and place at the repo root. First run will produce `gmail_token.json`. Both files are gitignored.

## Run

```bash
# init the SQLite schema once
python -c "from app.db import init; init()"

# start the webhook server
uvicorn app.main:app --reload --port 8000

# in another shell, expose it for Twilio
ngrok http 8000
# point the Twilio number's "A MESSAGE COMES IN" webhook at https://<ngrok>/sms
```

## Layout

- `app/db.py` — SQLite schema + helpers (done)
- `app/twilio_client.py` — outbound SMS (done)
- `app/main.py` — FastAPI webhook + agent loop (TODO)
- `app/tools/` — Gmail scan + Playwright cancellation (TODO)
- `data/sai.db` — created on first run, gitignored

## Build order

Saturday hours 1–6 and Sunday afternoon are spelled out in `PLAN.md`. Stop after Sunday — anything beyond is v0.1.
