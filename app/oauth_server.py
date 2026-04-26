"""HTTP server for OAuth magic-link flows. Runs in the same process as the
Telegram bot (see telegram_bot._serve).

Plaid: hosted Link at GET /plaid/link/{session_id}, JSON POST /plaid/exchange,
and POST /plaid/webhook for Plaid server-to-server (logged; sync optional later).

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
import html
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import db
from app.tools import outlook
from app.tools import plaid as plaid_tool

app = FastAPI(title="suri-oauth", openapi_url=None, docs_url=None, redoc_url=None)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Last-resort handler so a route bug renders a useful page instead of
    starlette's bare 'Internal Server Error', AND dumps a traceback to stderr
    so we can actually debug from `fly logs`."""
    tb = traceback.format_exc()
    print(
        f"[oauth] unhandled {type(exc).__name__} on {request.method} "
        f"{request.url.path}: {exc}\n{tb}",
        file=sys.stderr,
        flush=True,
    )
    body = (
        "<h1 class='err'>something went wrong</h1>"
        f"<p><code>{type(exc).__name__}: {exc}</code></p>"
        "<p>head back to telegram and try the connect link again. "
        "if it keeps failing, mention this to suri.</p>"
    )
    # Hand-rendered because _page is defined below this handler.
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>error</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:28rem;margin:4rem auto;padding:0 1.5rem;color:#222;line-height:1.5}}h1{{font-size:1.4rem}}.err{{color:#b91c1c}}code{{background:#f4f4f5;padding:.1rem .35rem;border-radius:.25rem}}</style>
</head><body>{body}</body></html>"""
    return HTMLResponse(content=html, status_code=500)

# How long a pending OAuth row stays valid. Microsoft's auth-code itself
# is good for ~10 min; we give the user a bit longer to tap the magic link.
_LINK_TTL = timedelta(minutes=15)


def _html_escape(s: str) -> str:
    return html.escape(s, quote=True)


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
  .tip  {{ background: #f0fdf4; border: 1px solid #86efac; border-radius: 0.4rem; padding: 0.75rem 1rem; font-size: 0.95rem; }}
  .tip ul {{ margin: 0.4rem 0 0 1rem; padding: 0; }}
  .tip li {{ margin: 0.35rem 0; }}
</style>
</head><body>{body_html}</body></html>"""
    return HTMLResponse(content=html, status_code=status)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Plaid Link (public browser flow — session id is unguessable, short-lived)
# ---------------------------------------------------------------------------


@app.get("/plaid/link/{session_id}")
async def plaid_link_page(session_id: str):
    db.prune_stale_plaid_link_sessions(older_than_minutes=30)
    row = db.get_plaid_link_session(session_id)
    if row is None:
        return _page(
            "link expired",
            "<h1>link expired</h1><p>ask suri in telegram for a fresh bank link.</p>",
            status=404,
        )
    if _is_expired(row["created_at"]):
        db.delete_plaid_link_session(session_id)
        return _page(
            "link expired",
            "<h1>link expired</h1><p>this plaid link is too old. message suri for a new one.</p>",
            status=410,
        )
    token_json = json.dumps(row["link_token"])
    env = (os.environ.get("PLAID_ENV") or "sandbox").lower().strip() or "sandbox"
    lines = "".join(
        f"<li>{_html_escape(t)}</li>" for t in plaid_tool.user_facing_steps()
    )
    ro = _html_escape(plaid_tool.read_only_promise())
    body = f"""<h1>connect your bank (read-only)</h1>
<p class="ok"><strong>What this does:</strong> {ro}</p>
<div class="tip"><strong>How to do it ({env})</strong><ul>{lines}</ul></div>
<p><button type="button" id="plaidBtn" style="font-size:1rem;padding:0.5rem 1rem">open plaid</button></p>
<p id="status"></p>
<script id="plaid-link-token" type="application/json">{token_json}</script>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
const linkToken = JSON.parse(document.getElementById("plaid-link-token").textContent);
const btn = document.getElementById("plaidBtn");
const st = document.getElementById("status");
if (!window.Plaid) {{
  st.textContent = "failed to load plaid script. check your network.";
}} else {{
  const handler = Plaid.create({{
    token: linkToken,
    onSuccess: function(publicToken, meta) {{
      st.textContent = "finishing up…";
      fetch("/plaid/exchange", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ public_token: publicToken }})
      }})
        .then(r => r.json())
        .then(d => {{
          if (d.ok) {{
            st.innerHTML = "<span class='ok'>connected &mdash; you can close this tab. suri is ready in telegram.</span>";
            btn.remove();
          }} else {{
            st.innerHTML = "<span class='err'>couldn't save link: " + (d.error || "unknown") + "</span>";
          }}
        }})
        .catch(e => {{
          st.innerHTML = "<span class='err'>network error: " + e + "</span>";
        }});
    }},
    onExit: function() {{
      st.textContent = "plaid closed without finishing. tap the button to try again.";
    }},
    onEvent: function() {{}}
  }});
  btn.addEventListener("click", function() {{ handler.open(); }});
  st.textContent = "tap open plaid to begin.";
}}
</script>"""
    return _page("connect bank", body)


@app.post("/plaid/exchange")
async def plaid_exchange(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected JSON")
    public_token = body.get("public_token") if isinstance(body, dict) else None
    if not public_token or not isinstance(public_token, str):
        raise HTTPException(status_code=400, detail="missing public_token")
    out = await asyncio.to_thread(plaid_tool.exchange_public_token, public_token)
    if out.get("ok"):
        print(
            f"[plaid] item linked item_id={out.get('item_id')}",
            file=sys.stderr,
            flush=True,
        )
    return JSONResponse(content=out)


@app.post("/plaid/webhook")
async def plaid_webhook(request: Request):
    """Plaid webhooks: log and return 2xx. Sync-on-webhook can be added later."""
    try:
        body = await request.json()
    except Exception:
        body = {"raw": (await request.body())[:2000].decode("utf-8", errors="replace")}
    t = body.get("webhook_type") or body.get("type") or "unknown"
    print(f"[plaid] webhook {t} keys={list(body.keys())[:12]}", file=sys.stderr, flush=True)
    return {"received": True}


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
