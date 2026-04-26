"""HTTP server for OAuth magic-link flows. Runs in the same process as the
Telegram bot (see telegram_bot._serve).

Two endpoints for Outlook today:
  GET /connect/outlook/{state}        — short link Suri sends to the user.
                                        Looks up the pending flow and redirects
                                        to Microsoft's consent page.
  GET /connect/outlook/callback       — Microsoft redirects here after consent.
                                        Exchanges the code for a token,
                                        persists it, and replays the original
                                        prompt back through the agent loop.

Plus /healthz for fly.io's healthcheck.

Why HTTP at all: device-code flow is a bad UX in chat (typed codes, blocks
the agent loop, can expire). Auth-code flow with a hosted callback is the
standard pattern for chat-bot OAuth — one tap, no typed codes, non-blocking,
gracefully recoverable.
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.tools import outlook

app = FastAPI(title="suri-oauth", openapi_url=None, docs_url=None, redoc_url=None)

# How long a pending OAuth row stays valid. Microsoft's auth-code itself
# is good for ~10 min; we give the user a bit longer to tap the magic link.
_LINK_TTL = timedelta(minutes=15)


def _is_expired(created_at: str | None) -> bool:
    if not created_at:
        return False
    # SQLite CURRENT_TIMESTAMP returns naive UTC strings like '2026-04-26 02:14:22'.
    try:
        ts = datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - ts > _LINK_TTL


def _page(title: str, body_html: str, status: int = 200) -> HTMLResponse:
    """Tiny inline page. We deliberately don't ship a CSS file — keeps the
    OAuth surface to a single .py file and renders fine on a phone."""
    html = f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          max-width: 28rem; margin: 4rem auto; padding: 0 1.5rem;
          color: #222; line-height: 1.5; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.5rem; }}
  p  {{ margin: 0.5rem 0; }}
  .ok    {{ color: #1b8a3a; }}
  .warn  {{ color: #b45309; }}
  .err   {{ color: #b91c1c; }}
  code   {{ background: #f4f4f5; padding: 0.1rem 0.35rem; border-radius: 0.25rem; }}
</style>
</head><body>{body_html}</body></html>"""
    return HTMLResponse(content=html, status_code=status)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# IMPORTANT: keep this registered BEFORE /connect/outlook/{state} or the
# catch-all path param will swallow the literal "callback" segment.
@app.get("/connect/outlook/callback")
async def outlook_callback(request: Request):
    """Microsoft redirects here after consent. Exchange the code, persist the
    token, and re-trigger the agent with the original prompt."""
    params = dict(request.query_params)

    if "error" in params:
        err = params.get("error_description") or params.get("error")
        return _page(
            "couldn't connect outlook",
            f"<h1 class='err'>couldn't connect outlook</h1>"
            f"<p>microsoft said: <code>{err}</code></p>"
            "<p>head back to telegram and try again.</p>",
            status=400,
        )

    state = params.get("state")
    if not state:
        raise HTTPException(status_code=400, detail="missing state")

    row = db.get_pending_oauth(state)
    if row is None or row["provider"] != "outlook":
        return _page(
            "link expired",
            "<h1>link expired</h1>"
            "<p>this connect link was already used or has expired. "
            "message suri again to get a fresh one.</p>",
            status=410,
        )

    flow = json.loads(row["flow_json"])
    try:
        # MSAL handles state validation, code exchange, and PKCE for us.
        result = await asyncio.to_thread(
            outlook.complete_auth_code_flow, flow, params
        )
    except Exception as e:
        print(
            f"[oauth] token exchange raised: {type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )
        return _page(
            "couldn't connect outlook",
            f"<h1 class='err'>couldn't connect outlook</h1>"
            f"<p><code>{type(e).__name__}: {e}</code></p>"
            "<p>head back to telegram and try again.</p>",
            status=500,
        )

    if "access_token" not in result:
        err = result.get("error_description") or result.get("error") or str(result)
        return _page(
            "couldn't connect outlook",
            f"<h1 class='err'>couldn't connect outlook</h1>"
            f"<p>microsoft response: <code>{err}</code></p>",
            status=400,
        )

    # One-shot: consume the row so a refresh / second tap doesn't fire twice.
    db.delete_pending_oauth(state)
    print(f"[oauth] outlook connected for state={state}", file=sys.stderr, flush=True)

    # Re-trigger the original prompt on the bot's event loop. Late import
    # because telegram_bot imports this module's `app` indirectly via uvicorn.
    from app import telegram_bot

    original_prompt = row["original_prompt"]
    chat_id = int(row["telegram_user_id"])

    async def _resume():
        try:
            await telegram_bot.push("outlook connected — picking up where we left off.")
        except Exception as e:
            print(f"[oauth] confirmation push failed: {e}", file=sys.stderr, flush=True)
        if original_prompt:
            try:
                await telegram_bot.run_turn(original_prompt, chat_id=chat_id)
            except Exception as e:
                print(
                    f"[oauth] re-trigger raised: {type(e).__name__}: {e}",
                    file=sys.stderr,
                    flush=True,
                )

    telegram_bot.schedule_on_loop(_resume())

    return _page(
        "outlook connected",
        "<h1 class='ok'>outlook connected</h1>"
        "<p>you can close this tab. suri's already on it back in telegram.</p>",
    )


@app.get("/connect/outlook/{state}")
async def start_outlook_auth(state: str):
    """Short link Suri sends in chat. Looks up the pending flow and 302s
    the user to Microsoft's consent screen."""
    db.prune_expired_oauth(older_than_minutes=int(_LINK_TTL.total_seconds() / 60))
    row = db.get_pending_oauth(state)
    if row is None or row["provider"] != "outlook":
        return _page(
            "link expired",
            "<h1>link expired</h1>"
            "<p>this connect link isn't valid anymore. message suri again "
            "(any email-related question) and she'll send a fresh one.</p>",
            status=404,
        )
    if _is_expired(row["created_at"]):
        db.delete_pending_oauth(state)
        return _page(
            "link expired",
            "<h1>link expired</h1>"
            "<p>this connect link sat too long. message suri again to get "
            "a fresh one.</p>",
            status=410,
        )
    flow = json.loads(row["flow_json"])
    return RedirectResponse(url=flow["auth_uri"], status_code=302)
