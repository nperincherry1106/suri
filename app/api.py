"""FastAPI router mounted at /api/v1 — the only surface the iOS app talks to.

Endpoints today:
  GET  /api/v1/healthz                  no auth, liveness probe
  POST /api/v1/auth/apple               exchange Apple identity_token for session jwt
  GET  /api/v1/me                       auth-gated identity probe

  GET  /api/v1/finances/items           linked plaid items (banks)
  GET  /api/v1/finances/transactions    recent transactions (local DB, sync-fed)
  GET  /api/v1/finances/recurring       plaid's recurring streams (in + out)
  GET  /api/v1/finances/subscriptions   filtered active outflow subscriptions
  POST /api/v1/finances/sync            manual refresh: hits plaid + persists tx

  Inbox is multi-provider: Outlook + Gmail are peers. The /inbox/* family
  is the *unified* surface (iOS default), merged + sorted across whatever
  providers are connected. /outlook/* and /gmail/* are source-specific
  shortcuts. Pass ?source=outlook|gmail to /inbox/* to filter.

  GET  /api/v1/inbox/triage             unified recent slice (both providers)
  GET  /api/v1/inbox/owed-replies       unified threads-owed (both providers)
  GET  /api/v1/inbox/marketing-senders  unified senders (de-duped across providers)

  GET  /api/v1/outlook/{triage,owed-replies,marketing-senders}  outlook-only
  GET  /api/v1/gmail/{triage,owed-replies,marketing-senders}    gmail-only

  GET  /api/v1/today                    composite snapshot for iOS Today tab

Every auth-gated endpoint uses Depends(auth.current_apple_sub). Source-
specific endpoints return a structured 409 with `code: "outlook_auth_required"`
or `code: "gmail_auth_required"` when that provider isn't connected. The
unified /inbox/* endpoints succeed if at least one provider is connected
and surface per-provider status under the `providers` key.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import auth, db, insights
from app.tools import gmail as gmail_tool
from app.tools import outlook as outlook_tool
from app.tools import plaid as plaid_tool


router = APIRouter(prefix="/api/v1", tags=["v1"])


class AppleSignInRequest(BaseModel):
    identity_token: str = Field(
        ...,
        description="The `identityToken` from ASAuthorizationAppleIDCredential, "
        "decoded from Data to UTF-8 string before sending.",
    )


class AppleSignInResponse(BaseModel):
    session_jwt: str
    expires_at: str
    sub: str


@router.get("/healthz")
def api_healthz():
    """Liveness probe for the iOS app. No auth — used pre-sign-in to confirm
    the backend is reachable and the build is recent enough."""
    return {"ok": True, "api_version": "v1"}


@router.post("/auth/apple", response_model=AppleSignInResponse)
def auth_apple(req: AppleSignInRequest, request: Request):
    """Exchange Apple's identity_token for a long-lived Suri session JWT.
    Single-tenant gate: the apple_sub must match SURI_OWNER_APPLE_SUB.
    First-ever sign-in returns 403 with a useful detail; the apple_sub is
    logged so Namrita can pin it via `fly secrets set`."""
    payload = auth.verify_apple_identity_token(req.identity_token)
    apple_sub = payload["sub"]
    allowed, reason = auth.is_owner(apple_sub)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    issued = auth.issue_session_jwt(
        apple_sub=apple_sub,
        user_agent=request.headers.get("User-Agent"),
    )
    return AppleSignInResponse(
        session_jwt=issued["session_jwt"],
        expires_at=issued["expires_at"],
        sub=apple_sub,
    )


@router.get("/me")
def me(sub: str = Depends(auth.current_apple_sub)):
    """Auth-gated identity probe. The iOS app calls this on launch to test
    whether its keychain-stored session is still valid; on 401 it triggers
    Sign in with Apple again."""
    return {"sub": sub}


# ---------------------------------------------------------------------------
# /api/v1/finances/*
# ---------------------------------------------------------------------------


def _plaid_configured_or_503() -> None:
    """Most finance endpoints depend on Plaid creds being set server-side.
    If they're not, return a 503 the iOS app can render as 'finance features
    not ready' instead of an opaque 500."""
    if not plaid_tool.is_configured():
        raise HTTPException(
            status_code=503,
            detail="plaid not configured server-side (missing PLAID_CLIENT_ID / PLAID_SECRET)",
        )


@router.get("/finances/items")
def finances_items(_sub: str = Depends(auth.current_apple_sub)):
    """List linked banks (no access tokens)."""
    _plaid_configured_or_503()
    out = plaid_tool.list_items()
    return {
        "ok": bool(out.get("ok")),
        "items": out.get("items") or [],
        "count": len(out.get("items") or []),
    }


@router.get("/finances/transactions")
def finances_transactions(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(200, ge=1, le=1000),
    item_id: str | None = Query(None, description="Filter to a single linked bank"),
    _sub: str = Depends(auth.current_apple_sub),
):
    """Read transactions from the local DB (populated by Plaid webhook-driven
    sync). Sorted by date desc. If the table is empty, the iOS app should
    POST to /finances/sync — it usually means historical_update hasn't
    fired yet (or this is a fresh DB after schema changes)."""
    _plaid_configured_or_503()
    txs = db.list_plaid_transactions(days=days, limit=limit, item_id=item_id)
    return {
        "ok": True,
        "transactions": txs,
        "count": len(txs),
        "total_in_db": db.count_plaid_transactions(),
        "window_days": days,
    }


@router.get("/finances/recurring")
async def finances_recurring(_sub: str = Depends(auth.current_apple_sub)):
    """Plaid's recurring stream signals (both inflows and outflows). Lives
    behind a network call to Plaid — wrapped in to_thread so the request
    loop isn't blocked while Plaid responds."""
    _plaid_configured_or_503()
    out = await asyncio.to_thread(plaid_tool.fetch_recurring)
    return out


@router.get("/finances/subscriptions")
async def finances_subscriptions(_sub: str = Depends(auth.current_apple_sub)):
    """Filtered view: only outflow streams that look like an active monthly /
    weekly subscription. This is what the iOS Finances tab shows under
    'subscriptions you're paying for'."""
    _plaid_configured_or_503()
    r = await asyncio.to_thread(plaid_tool.fetch_recurring)
    if not r.get("ok"):
        return r
    keep_status = {"MATURE", "ACTIVE"}
    keep_freq = {"WEEKLY", "BIWEEKLY", "MONTHLY", "SEMI_MONTHLY", "ANNUALLY"}
    subs: list[dict] = []
    for s in r.get("outflow_recurring") or []:
        st = s.get("stream", {}) or {}
        status = (st.get("status") or "").upper()
        freq = (st.get("frequency") or "").upper()
        if status in keep_status and freq in keep_freq:
            subs.append(s)
    return {
        "ok": True,
        "subscriptions": subs,
        "count": len(subs),
    }


class SyncRequest(BaseModel):
    item_id: str | None = Field(None, description="Sync only this item; default = all")
    reset_cursor: bool = Field(
        False,
        description="Wipe cursor first and re-pull entire history. Use once "
        "after this DB schema change to backfill the local plaid_transactions "
        "table from a previously-linked item.",
    )


@router.post("/finances/sync")
async def finances_sync(
    req: SyncRequest | None = None,
    _sub: str = Depends(auth.current_apple_sub),
):
    """Trigger a Plaid sync now (instead of waiting for the next webhook).
    iOS app calls this on pull-to-refresh in the Finances tab."""
    _plaid_configured_or_503()
    body = req or SyncRequest()
    out = await asyncio.to_thread(
        plaid_tool.sync_transactions,
        body.item_id,
        body.reset_cursor,
    )
    return out


# ---------------------------------------------------------------------------
# Mailbox helpers — outlook + gmail are peers
# ---------------------------------------------------------------------------


_PROVIDER_REGEX = "^(all|outlook|gmail)$"


async def _run_outlook(fn, *args, **kwargs):
    """Run a blocking Outlook tool in a worker thread, translating
    OutlookAuthRequired into a structured HTTP 409 the iOS app can
    render as a 'Connect Outlook' CTA."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except outlook_tool.OutlookAuthRequired as e:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "outlook_auth_required",
                "provider": "outlook",
                "message": "outlook isn't connected on this account yet",
                "connect_url": getattr(e, "auth_url", None),
            },
        )


async def _run_gmail(fn, *args, **kwargs):
    """Run a blocking Gmail tool in a worker thread. Translates
    GmailAuthRequired into a 409 mirroring the Outlook variant; if the
    server doesn't have GMAIL_CLIENT_ID/SECRET configured at all,
    returns a 503 so the iOS app can render 'gmail unavailable on this
    server' instead of the connect CTA."""
    if not gmail_tool.is_configured():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "gmail_not_configured",
                "provider": "gmail",
                "message": "GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET not set on the server",
            },
        )
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except gmail_tool.GmailAuthRequired as e:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "gmail_auth_required",
                "provider": "gmail",
                "message": "gmail isn't connected on this account yet",
                "connect_url": getattr(e, "auth_url", None),
            },
        )


async def _gather_providers(outlook_call, gmail_call, source: str) -> dict:
    """Helper for the unified /inbox/* endpoints. Runs whichever
    providers are requested in parallel, catches per-provider auth
    errors, returns a (results, status) tuple ready for the response.

    Source semantics:
      - "all":     run both, succeed if at least one returns data
      - "outlook": only outlook; behaves like /outlook/* (raises 409 if unauthed)
      - "gmail":   only gmail; behaves like /gmail/* (raises 409 if unauthed)

    Returns: {
        "results": {"outlook": list_or_None, "gmail": list_or_None},
        "status":  {"outlook": {ok, ...}, "gmail": {ok, ...}},
    }
    """
    tasks = {}
    if source in ("all", "outlook"):
        tasks["outlook"] = asyncio.create_task(_safe_provider("outlook", outlook_call))
    if source in ("all", "gmail"):
        if gmail_tool.is_configured():
            tasks["gmail"] = asyncio.create_task(_safe_provider("gmail", gmail_call))
        elif source == "gmail":
            # Explicit gmail request but server has no creds → propagate 503.
            await _run_gmail(lambda: None)  # raises 503
        else:
            # source=all, gmail not configured → silently skip.
            pass

    results, status = {}, {}
    for prov, task in tasks.items():
        items, prov_status = await task
        results[prov] = items
        status[prov] = prov_status

    if source != "all":
        # Source-specific call — propagate the auth error as a 409 directly
        # so the existing iOS error-handling code path stays simple.
        prov_status = status.get(source) or {"ok": False}
        if not prov_status.get("ok"):
            code = prov_status.get("code") or f"{source}_unknown_error"
            raise HTTPException(
                status_code=409 if code.endswith("_auth_required") else 502,
                detail={
                    "code": code,
                    "provider": source,
                    "message": prov_status.get("message") or "provider call failed",
                    "connect_url": prov_status.get("connect_url"),
                },
            )

    return {"results": results, "status": status}


async def _safe_provider(name: str, call):
    """Run a provider tool, catching its auth-required exception and
    returning a structured status instead of raising. Used by the
    unified endpoints so one provider failing doesn't poison the other."""
    try:
        items = await asyncio.to_thread(call)
        return items, {"ok": True, "count": len(items) if isinstance(items, list) else None}
    except outlook_tool.OutlookAuthRequired as e:
        return None, {
            "ok": False,
            "code": "outlook_auth_required",
            "message": "outlook not connected",
            "connect_url": getattr(e, "auth_url", None),
        }
    except gmail_tool.GmailAuthRequired as e:
        return None, {
            "ok": False,
            "code": "gmail_auth_required",
            "message": "gmail not connected",
            "connect_url": getattr(e, "auth_url", None),
        }
    except Exception as e:
        return None, {
            "ok": False,
            "code": f"{name}_error",
            "message": f"{type(e).__name__}: {e}",
        }


# ---------------------------------------------------------------------------
# /api/v1/inbox/*  — unified across outlook + gmail
# ---------------------------------------------------------------------------


@router.get("/inbox/triage")
async def inbox_triage(
    hours_back: int = Query(24, ge=1, le=168),
    max_results: int = Query(30, ge=1, le=100),
    source: str = Query("all", pattern=_PROVIDER_REGEX),
    _sub: str = Depends(auth.current_apple_sub),
):
    """Unified recent inbox slice across whichever providers are connected.
    Items carry a `source: "outlook" | "gmail"` field. Sorted by
    `received_at` desc. Per-provider status reported under `providers`."""
    bundle = await _gather_providers(
        lambda: outlook_tool.triage_inbox(hours_back=hours_back, max_results=max_results),
        lambda: gmail_tool.triage_inbox(hours_back=hours_back, max_results=max_results),
        source,
    )
    items: list[dict] = []
    for prov, prov_items in bundle["results"].items():
        if isinstance(prov_items, list):
            items.extend(prov_items)
    items.sort(key=lambda i: i.get("received_at") or "", reverse=True)
    items = items[:max_results] if source == "all" else items
    return {
        "ok": any(s.get("ok") for s in bundle["status"].values()),
        "items": items,
        "count": len(items),
        "marketing_count": sum(1 for i in items if i.get("is_marketing")),
        "window_hours": hours_back,
        "providers": bundle["status"],
    }


@router.get("/inbox/owed-replies")
async def inbox_owed_replies(
    days_threshold: int = Query(2, ge=0, le=30),
    lookback_days: int = Query(14, ge=1, le=90),
    source: str = Query("all", pattern=_PROVIDER_REGEX),
    _sub: str = Depends(auth.current_apple_sub),
):
    """Unified owed-replies across providers. Items carry a `source`
    field and are sorted by `days_waiting` desc."""
    bundle = await _gather_providers(
        lambda: outlook_tool.find_owed_replies(
            days_threshold=days_threshold, lookback_days=lookback_days
        ),
        lambda: gmail_tool.find_owed_replies(
            days_threshold=days_threshold, lookback_days=lookback_days
        ),
        source,
    )
    owed: list[dict] = []
    for prov, prov_items in bundle["results"].items():
        # Each tool returns either a list or {"ok": false, ...} on a soft error.
        if isinstance(prov_items, list):
            owed.extend(prov_items)
    owed.sort(key=lambda r: r.get("days_waiting") or 0, reverse=True)
    return {
        "ok": any(s.get("ok") for s in bundle["status"].values()),
        "owed": owed,
        "count": len(owed),
        "providers": bundle["status"],
    }


@router.get("/inbox/marketing-senders")
async def inbox_marketing_senders(
    source: str = Query("all", pattern=_PROVIDER_REGEX),
    _sub: str = Depends(auth.current_apple_sub),
):
    """Unified marketing-senders across providers. De-duplicates by
    `sender_domain` — if the same domain appears in both Outlook and
    Gmail, the row collapses and `email_count` is summed. Outlook's
    unsubscribe_url wins for a hybrid row (since outlook's per-message
    unsubscribe URL is generally a richer preference center)."""
    bundle = await _gather_providers(
        lambda: outlook_tool.find_marketing_senders(),
        lambda: gmail_tool.find_marketing_senders(),
        source,
    )
    by_domain: dict[str, dict] = {}
    for prov in ("outlook", "gmail"):
        prov_items = bundle["results"].get(prov)
        if not isinstance(prov_items, list):
            continue
        for s in prov_items:
            dom = s.get("sender_domain") or s.get("domain")
            if not dom:
                continue
            if dom not in by_domain:
                by_domain[dom] = dict(s)
                by_domain[dom]["sources"] = [prov]
            else:
                merged = by_domain[dom]
                merged["email_count"] = (merged.get("email_count") or 0) + (s.get("email_count") or 0)
                if prov not in merged["sources"]:
                    merged["sources"].append(prov)
                # Newer last_seen wins.
                if (s.get("last_seen") or "") > (merged.get("last_seen") or ""):
                    merged["last_seen"] = s.get("last_seen")
    senders = list(by_domain.values())
    senders.sort(key=lambda r: r.get("email_count") or 0, reverse=True)
    return {
        "ok": any(s.get("ok") for s in bundle["status"].values()),
        "senders": senders,
        "count": len(senders),
        "providers": bundle["status"],
    }


# ---------------------------------------------------------------------------
# /api/v1/outlook/* — single-provider shortcuts (raise 409 when unauthed)
# ---------------------------------------------------------------------------


@router.get("/outlook/triage")
async def outlook_triage(
    hours_back: int = Query(24, ge=1, le=168),
    max_results: int = Query(30, ge=1, le=100),
    _sub: str = Depends(auth.current_apple_sub),
):
    items = await _run_outlook(
        outlook_tool.triage_inbox,
        hours_back=hours_back,
        max_results=max_results,
    )
    if not isinstance(items, list):
        raise HTTPException(status_code=502, detail="unexpected outlook response shape")
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "marketing_count": sum(1 for i in items if i.get("is_marketing")),
        "window_hours": hours_back,
        "source": "outlook",
    }


@router.get("/outlook/owed-replies")
async def outlook_owed_replies(
    days_threshold: int = Query(2, ge=0, le=30),
    lookback_days: int = Query(14, ge=1, le=90),
    _sub: str = Depends(auth.current_apple_sub),
):
    out = await _run_outlook(
        outlook_tool.find_owed_replies,
        days_threshold=days_threshold,
        lookback_days=lookback_days,
    )
    if isinstance(out, dict) and not out.get("ok", True):
        return {"ok": False, "owed": [], "count": 0, "error": out.get("error"), "source": "outlook"}
    owed = out if isinstance(out, list) else (out.get("owed") or [])
    return {"ok": True, "owed": owed, "count": len(owed), "source": "outlook"}


@router.get("/outlook/marketing-senders")
async def outlook_marketing_senders(_sub: str = Depends(auth.current_apple_sub)):
    out = await _run_outlook(outlook_tool.find_marketing_senders)
    senders = out if isinstance(out, list) else (out.get("senders") if isinstance(out, dict) else [])
    return {"ok": True, "senders": senders, "count": len(senders) if senders else 0, "source": "outlook"}


# ---------------------------------------------------------------------------
# /api/v1/gmail/* — single-provider shortcuts (raise 409 when unauthed)
# ---------------------------------------------------------------------------


@router.get("/gmail/triage")
async def gmail_triage(
    hours_back: int = Query(24, ge=1, le=168),
    max_results: int = Query(30, ge=1, le=100),
    _sub: str = Depends(auth.current_apple_sub),
):
    items = await _run_gmail(
        gmail_tool.triage_inbox,
        hours_back=hours_back,
        max_results=max_results,
    )
    if not isinstance(items, list):
        raise HTTPException(status_code=502, detail="unexpected gmail response shape")
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "marketing_count": sum(1 for i in items if i.get("is_marketing")),
        "window_hours": hours_back,
        "source": "gmail",
    }


@router.get("/gmail/owed-replies")
async def gmail_owed_replies(
    days_threshold: int = Query(2, ge=0, le=30),
    lookback_days: int = Query(14, ge=1, le=90),
    _sub: str = Depends(auth.current_apple_sub),
):
    out = await _run_gmail(
        gmail_tool.find_owed_replies,
        days_threshold=days_threshold,
        lookback_days=lookback_days,
    )
    if isinstance(out, dict) and not out.get("ok", True):
        return {"ok": False, "owed": [], "count": 0, "error": out.get("error"), "source": "gmail"}
    owed = out if isinstance(out, list) else (out.get("owed") or [])
    return {"ok": True, "owed": owed, "count": len(owed), "source": "gmail"}


@router.get("/gmail/marketing-senders")
async def gmail_marketing_senders(_sub: str = Depends(auth.current_apple_sub)):
    out = await _run_gmail(gmail_tool.find_marketing_senders)
    senders = out if isinstance(out, list) else (out.get("senders") if isinstance(out, dict) else [])
    return {"ok": True, "senders": senders, "count": len(senders) if senders else 0, "source": "gmail"}


# ---------------------------------------------------------------------------
# /api/v1/today  (the iOS Today tab's single payload)
# ---------------------------------------------------------------------------


@router.get("/today")
async def today(_sub: str = Depends(auth.current_apple_sub)):
    """One snapshot composed from inbox + finances + reminders + pending
    actions + agent_actions, run in parallel server-side. Each section
    has an `ok` flag — the iOS app branches per-section instead of
    failing the whole tab."""
    return await insights.today_snapshot()
