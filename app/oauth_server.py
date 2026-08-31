"""HTTP server for OAuth magic-link flows + Plaid endpoints + Plaid webhooks.

Mounted by app.main as the only public surface; serves on PORT (8080 on fly).

Plaid: hosted Link at GET /plaid/link/{session_id}, JSON POST /plaid/exchange,
and POST /plaid/webhook for Plaid (LINK: exchange public_token(s) for multi-item sessions).

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

from app import api as api_v1
from app import db, push
from app.tools import gmail as gmail_tool
from app.tools import outlook
from app.tools import plaid as plaid_tool

app = FastAPI(title="suri-oauth", openapi_url=None, docs_url=None, redoc_url=None)
app.include_router(api_v1.router)


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
      if (!publicToken) {{
        st.innerHTML = "<span class='ok'>session finished. multi-bank link sends tokens to the server (not the browser) &mdash; your bank is saving now. check telegram in a few seconds, or ask suri for plaid_list_items.</span>";
        btn.remove();
        return;
      }}
      st.textContent = "finishing up…";
      fetch("/plaid/exchange", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ public_token: publicToken }})
      }})
        .then(async (r) => {{
          const data = await r.json().catch(() => ({{ parse_error: true, status: r.status }}));
          if (!r.ok) {{
            const msg = data.detail || data.error || (data.parse_error ? "bad json from server" : JSON.stringify(data));
            st.innerHTML = "<span class='err'>couldn't save link (HTTP " + r.status + "): " + msg + "</span>";
            return;
          }}
          if (data.ok) {{
            st.innerHTML = "<span class='ok'>connected &mdash; you can close this tab. suri is ready in telegram.</span>";
            btn.remove();
          }} else {{
            st.innerHTML = "<span class='err'>couldn't save link: " + (data.error || data.detail || JSON.stringify(data)) + "</span>";
          }}
        }})
        .catch((e) => {{
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


async def _notify(text: str) -> None:
    """Best-effort proactive push; never raises. Goes through the registered
    transport (today: stderr stub via app.main; step 2: APNs)."""
    try:
        await push.push_async(text)
    except Exception as e:
        print(f"[plaid] push notify: {e}", file=sys.stderr, flush=True)


def _summarize_sync_results(out: dict) -> str:
    parts: list[str] = []
    for p in out.get("per_item") or []:
        nm = p.get("institution_name") or p.get("item_id") or "bank"
        if p.get("ok") is False:
            parts.append(f"{nm}: sync failed ({p.get('error') or 'unknown'})")
            continue
        n = p.get("new_transactions_fetched", 0)
        parts.append(f"{nm}: {n} new tx")
    return "; ".join(parts) if parts else "(no items)"


async def _sync_after_link(item_ids: list[str]) -> None:
    if not item_ids:
        return
    for iid in item_ids:
        try:
            out = await asyncio.to_thread(plaid_tool.sync_transactions, iid)
            print(
                f"[plaid] auto-sync after link item={iid} ok={out.get('ok')} "
                f"summary={_summarize_sync_results(out)}",
                file=sys.stderr,
                flush=True,
            )
        except Exception as e:
            print(
                f"[plaid] auto-sync after link item={iid} crashed: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
                flush=True,
            )


async def _sync_after_transactions_webhook(item_id: str) -> None:
    try:
        out = await asyncio.to_thread(plaid_tool.sync_transactions, item_id)
        print(
            f"[plaid] auto-sync after TRANSACTIONS webhook item={item_id} "
            f"ok={out.get('ok')} summary={_summarize_sync_results(out)}",
            file=sys.stderr,
            flush=True,
        )
        added = 0
        for p in out.get("per_item") or []:
            added += int(p.get("new_transactions_fetched") or 0)
        if added > 0:
            await _notify(
                f"plaid: pulled {added} new transaction(s). ask for plaid_recurring "
                f"if you want me to pick out subscriptions."
            )
    except Exception as e:
        print(
            f"[plaid] auto-sync after TRANSACTIONS webhook item={item_id} crashed: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )


@app.post("/plaid/webhook")
async def plaid_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {"raw": (await request.body())[:2000].decode("utf-8", errors="replace")}
    if not isinstance(body, dict):
        print("[plaid] webhook non-object", file=sys.stderr, flush=True)
        return {"received": True}

    wtype = (body.get("webhook_type") or body.get("type") or "unknown")
    wcode = body.get("webhook_code")
    print(
        f"[plaid] webhook {wtype} code={wcode} keys={list(body.keys())[:12]}",
        file=sys.stderr,
        flush=True,
    )

    out = await asyncio.to_thread(plaid_tool.process_link_webhook, body)
    if not (out.get("ignored") or out.get("skipped")):
        errs = out.get("errors") or []
        item_ids = out.get("exchanged_item_ids") or []
        n = len(item_ids)
        if n and not errs:
            await _notify(
                f"plaid: linked {n} bank item(s). pulling your transactions now…"
            )
            asyncio.create_task(_sync_after_link(item_ids))
        elif n and errs:
            await _notify(
                f"plaid: linked {n} item(s) but some exchanges failed. "
                f"i'll keep what i got — try plaid_list_items."
            )
            asyncio.create_task(_sync_after_link(item_ids))
        elif errs and not n:
            await _notify(
                "plaid: couldn't save a bank from the link session. "
                "check suri logs and try a new bank link."
            )

    if (wtype or "").upper() == "TRANSACTIONS" and wcode in (
        "HISTORICAL_UPDATE",
        "SYNC_UPDATES_AVAILABLE",
        "DEFAULT_UPDATE",
    ):
        iid = body.get("item_id")
        if isinstance(iid, str) and iid:
            asyncio.create_task(_sync_after_transactions_webhook(iid))

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

    original_prompt = row["original_prompt"]

    async def _resume():
        try:
            await push.push_async("outlook connected — picking up where we left off.")
        except Exception as e:
            print(f"[oauth] confirmation push failed: {e}", file=sys.stderr, flush=True)
        # The auto-resume of `original_prompt` used to run an agent turn back
        # into Telegram. With Telegram removed and chat-in-app not yet built
        # (lands in step ~10 of the iOS plan), there's no surface to render
        # the agent's reply, so we just log it. Once /api/v1/chat/send is
        # live we'll rewire this to enqueue the prompt for the next session.
        if original_prompt:
            print(
                f"[oauth] would re-trigger prompt (deferred until chat-in-app): "
                f"{original_prompt[:120]!r}",
                file=sys.stderr,
                flush=True,
            )

    asyncio.create_task(_resume())

    return _page(
        "outlook connected",
        "<h1 class='ok'>outlook connected</h1>"
        "<p>you can close this tab. open suri to keep going.</p>",
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


# ---------------------------------------------------------------------------
# Gmail magic-link OAuth (peer to outlook)
# ---------------------------------------------------------------------------


@app.get("/connect/gmail/callback")
async def gmail_callback(request: Request):
    """Google's redirect target after the user consents. Exchange the
    auth code for tokens, persist creds, and one-shot the pending row."""
    params = dict(request.query_params)
    if "error" in params:
        err = params.get("error_description") or params.get("error")
        return _page(
            "couldn't connect gmail",
            f"<h1 class='err'>couldn't connect gmail</h1>"
            f"<p>google said: <code>{err}</code></p>"
            "<p>try the connect link again from suri.</p>",
            status=400,
        )

    state = params.get("state")
    if not state:
        raise HTTPException(status_code=400, detail="missing state")

    row = db.get_pending_oauth(state)
    if row is None or row["provider"] != "gmail":
        return _page(
            "link expired",
            "<h1>link expired</h1>"
            "<p>this gmail connect link was already used or has expired. "
            "ask suri for a fresh one.</p>",
            status=410,
        )

    flow_state = json.loads(row["flow_json"])
    # Force https on the auth-response URL even if proxy headers were
    # somehow stripped. oauthlib raises InsecureTransportError on http://
    # and we know we're behind fly's TLS-terminating proxy in prod.
    auth_response_url = str(request.url)
    if auth_response_url.startswith("http://") and (os.environ.get("SURI_PUBLIC_URL") or "").startswith("https://"):
        auth_response_url = "https://" + auth_response_url[len("http://"):]
    try:
        result = await asyncio.to_thread(
            gmail_tool.complete_auth_code_flow,
            flow_state,
            auth_response_url,
        )
    except Exception as e:
        print(
            f"[gmail-oauth] token exchange raised: {type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )
        return _page(
            "couldn't connect gmail",
            f"<h1 class='err'>couldn't connect gmail</h1>"
            f"<p><code>{type(e).__name__}: {e}</code></p>",
            status=500,
        )

    if not result.get("ok"):
        err = result.get("error") or str(result)
        return _page(
            "couldn't connect gmail",
            f"<h1 class='err'>couldn't connect gmail</h1>"
            f"<p>{err}</p>",
            status=400,
        )

    db.delete_pending_oauth(state)
    print(f"[gmail-oauth] connected for state={state}", file=sys.stderr, flush=True)

    original_prompt = row["original_prompt"]

    async def _resume():
        try:
            await push.push_async(
                "gmail connected — i can see your inbox now."
            )
        except Exception as e:
            print(f"[gmail-oauth] confirmation push failed: {e}", file=sys.stderr, flush=True)
        if original_prompt:
            print(
                f"[gmail-oauth] would re-trigger prompt (deferred until chat-in-app): "
                f"{original_prompt[:120]!r}",
                file=sys.stderr,
                flush=True,
            )

    asyncio.create_task(_resume())

    note = "" if result.get("has_refresh_token") else (
        "<p class='warn'>note: google didn't issue a refresh token. you may "
        "have to re-authorize when the access token expires (~1h). this "
        "usually means the consent screen wasn't fresh — try revoking "
        "Suri at <a href='https://myaccount.google.com/permissions'>"
        "myaccount.google.com/permissions</a> and tap the link again.</p>"
    )
    return _page(
        "gmail connected",
        "<h1 class='ok'>gmail connected</h1>"
        "<p>you can close this tab. open suri to keep going.</p>"
        + note,
    )


@app.get("/connect/gmail/{state}")
async def start_gmail_auth(state: str):
    """Short link Suri sends in chat. Builds the live Google OAuth URL
    on demand (we didn't store auth_uri at flow-init time because the
    google-auth-oauthlib Flow returns it inline) and 302s the user to
    Google's consent screen."""
    db.prune_expired_oauth(older_than_minutes=int(_LINK_TTL.total_seconds() / 60))
    row = db.get_pending_oauth(state)
    if row is None or row["provider"] != "gmail":
        return _page(
            "link expired",
            "<h1>link expired</h1>"
            "<p>this connect link isn't valid anymore. ask suri for a fresh one.</p>",
            status=404,
        )
    if _is_expired(row["created_at"]):
        db.delete_pending_oauth(state)
        return _page(
            "link expired",
            "<h1>link expired</h1>"
            "<p>this connect link sat too long. ask suri for a new one.</p>",
            status=410,
        )

    flow_state = json.loads(row["flow_json"])
    auth_url = flow_state.get("auth_url")
    if not auth_url:
        # Pre-PKCE-fix rows won't have a stored auth_url. Tell her to
        # re-issue. (Once any cached row from before this fix expires,
        # this branch is dead — but keep it so we don't 500 on the
        # transition.)
        return _page(
            "link expired",
            "<h1>link expired</h1>"
            "<p>this link was issued before the PKCE fix. ask suri for a fresh one.</p>",
            status=410,
        )
    return RedirectResponse(url=auth_url, status_code=302)
