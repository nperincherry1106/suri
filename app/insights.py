"""Today aggregator — composes the iOS Today tab's "what should you act on
right now" view from inbox + finances + reminders + pending actions + the
agent_actions audit log.

Why a dedicated aggregator (instead of the iOS app calling 5 endpoints):
  - Sections are computed in parallel server-side; the iOS app gets a single
    payload it can render in one pass without orchestrating fan-out.
  - Each section degrades gracefully (return_exceptions=True). If Outlook is
    down or Plaid isn't configured, the rest still renders. The iOS app
    branches on the per-section `ok` flag, never on a top-level error.
  - The shape is the contract for the iOS Today tab — keeps the swift codable
    structs simple and stable as we add sources later.

This module is read-only. It never mutates state, never sends pushes.
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from app import db
from app.tools import gmail as gmail_tool
from app.tools import outlook as outlook_tool
from app.tools import plaid as plaid_tool


# ---------------------------------------------------------------------------
# section helpers (each returns a dict with at least {"ok": bool})
# ---------------------------------------------------------------------------


def _today_pt() -> str:
    """Current date in America/Los_Angeles as ISO YYYY-MM-DD. The Today tab
    is anchored to her local day, not UTC."""
    # We don't pull pytz; APScheduler and the rest already rely on the
    # `tzdata` package providing IANA tz to the stdlib. Use zoneinfo.
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def _call_provider(name: str, fn) -> dict:
    """Run a single mailbox provider call, swallowing the auth-required
    exception classes. Returns either {"ok": True, "items": list} or
    {"ok": False, "code": "..."}. Used by the section helpers to fan out
    across outlook + gmail without mixing exception types."""
    try:
        items = fn()
    except outlook_tool.OutlookAuthRequired:
        return {"ok": False, "code": "outlook_auth_required", "items": []}
    except gmail_tool.GmailAuthRequired:
        return {"ok": False, "code": "gmail_auth_required", "items": []}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "items": []}
    if isinstance(items, dict) and not items.get("ok", True):
        return {"ok": False, "error": items.get("error"), "items": []}
    return {"ok": True, "items": items if isinstance(items, list) else []}


def _section_owed_replies() -> dict:
    """Threads waiting > 2 days for a reply, merged across both inboxes.
    Each item carries a `source` field. Section is OK if at least one
    provider returned data; per-provider status surfaces under
    `providers` so the iOS app can render a 'connect gmail / outlook'
    inline CTA when one side is missing."""
    out = _call_provider("outlook", lambda: outlook_tool.find_owed_replies(days_threshold=2, lookback_days=14))
    gma = (
        _call_provider("gmail", lambda: gmail_tool.find_owed_replies(days_threshold=2, lookback_days=14))
        if gmail_tool.is_configured() else {"ok": False, "code": "gmail_not_configured", "items": []}
    )
    merged = list(out["items"]) + list(gma["items"])
    merged.sort(key=lambda r: r.get("days_waiting") or 0, reverse=True)
    return {
        "ok": out["ok"] or gma["ok"],
        "owed_count": len(merged),
        "top": merged[:3],
        "providers": {
            "outlook": {k: v for k, v in out.items() if k != "items"},
            "gmail": {k: v for k, v in gma.items() if k != "items"},
        },
    }


def _section_urgent_inbox() -> dict:
    """Last 12h, unread, non-marketing — merged across providers. Same
    'best-effort, partial-success' contract as _section_owed_replies."""
    out = _call_provider("outlook", lambda: outlook_tool.triage_inbox(hours_back=12, max_results=20))
    gma = (
        _call_provider("gmail", lambda: gmail_tool.triage_inbox(hours_back=12, max_results=20))
        if gmail_tool.is_configured() else {"ok": False, "code": "gmail_not_configured", "items": []}
    )
    all_items = list(out["items"]) + list(gma["items"])
    real = [i for i in all_items if i.get("is_unread") and not i.get("is_marketing")]
    real.sort(key=lambda i: i.get("received_at") or "", reverse=True)
    return {
        "ok": out["ok"] or gma["ok"],
        "urgent_count": len(real),
        "marketing_noise_count": sum(1 for i in all_items if i.get("is_marketing")),
        "top": real[:3],
        "providers": {
            "outlook": {k: v for k, v in out.items() if k != "items"},
            "gmail": {k: v for k, v in gma.items() if k != "items"},
        },
    }


def _section_pending_actions() -> dict:
    """Cancellations / approvals awaiting her yes/no. These should be the
    first thing she sees — they're blocked on her, not on Suri."""
    pending = db.pending_actions()
    return {
        "ok": True,
        "count": len(pending),
        "items": pending,
    }


def _section_today_spend() -> dict:
    """Today's transactions from the local plaid_transactions DB, totals
    + top 3 outflows. Reads from cache (no Plaid network call) so this
    is fast even on large transaction histories."""
    if not plaid_tool.is_configured():
        return {"ok": False, "code": "plaid_not_configured"}
    today = _today_pt()
    txs = db.list_plaid_transactions(days=2, limit=500)
    today_txs = [t for t in txs if (t.get("date") or "").startswith(today)]
    out_txs = [t for t in today_txs if (t.get("amount") or 0) > 0]
    in_txs = [t for t in today_txs if (t.get("amount") or 0) < 0]
    total_out = round(sum(t.get("amount") or 0 for t in out_txs), 2)
    total_in = round(sum(-(t.get("amount") or 0) for t in in_txs), 2)
    top_out = sorted(out_txs, key=lambda t: -(t.get("amount") or 0))[:3]
    return {
        "ok": True,
        "date": today,
        "transaction_count": len(today_txs),
        "total_outflow": total_out,
        "total_inflow": total_in,
        "top_outflows": top_out,
    }


def _section_reminders_today() -> dict:
    """Reminders firing today (PT). Sorted by fire_at ascending."""
    today = _today_pt()
    pending = db.list_pending_reminders()
    todays = [r for r in pending if (r.get("fire_at") or "")[:10] == today]
    todays.sort(key=lambda r: r.get("fire_at") or "")
    return {
        "ok": True,
        "count": len(todays),
        "items": todays,
    }


def _section_what_suri_did() -> dict:
    """Audit log of tool calls in the last 24h. This is the trust layer —
    the iOS app's 'Your Suri' tab will render the full feed; on Today we
    show just the count + the last 3 with a one-line summary."""
    actions = db.recent_agent_actions(hours_back=24, limit=200)
    last_3 = []
    for a in actions[:3]:
        last_3.append({
            "tool_name": a.get("tool_name"),
            "ok": a.get("ok"),
            "created_at": a.get("created_at"),
        })
    return {
        "ok": True,
        "count_24h": len(actions),
        "successes_24h": sum(1 for a in actions if a.get("ok")),
        "failures_24h": sum(1 for a in actions if not a.get("ok")),
        "last_3": last_3,
    }


# ---------------------------------------------------------------------------
# top-level snapshot (run all sections in parallel)
# ---------------------------------------------------------------------------


_SECTIONS: dict[str, callable] = {
    "owed_replies": _section_owed_replies,
    "urgent_inbox": _section_urgent_inbox,
    "pending_actions": _section_pending_actions,
    "today_spend": _section_today_spend,
    "reminders_today": _section_reminders_today,
    "what_suri_did": _section_what_suri_did,
}


async def today_snapshot() -> dict:
    """Run every section in parallel via asyncio.to_thread + gather. One
    section blowing up never blocks the others — the offending section
    just appears with `ok: false` in the response and the iOS app hides
    or grays it."""
    async def _run(name: str, fn) -> tuple[str, Any]:
        try:
            return name, await asyncio.to_thread(fn)
        except Exception as e:
            print(
                f"[insights] section {name!r} crashed: {type(e).__name__}: {e}",
                file=sys.stderr,
                flush=True,
            )
            return name, {"ok": False, "error": f"{type(e).__name__}: {e}"}

    results = await asyncio.gather(*[_run(n, fn) for n, fn in _SECTIONS.items()])
    sections: dict[str, Any] = dict(results)

    return {
        "ok": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "today_pt": _today_pt(),
        **sections,
    }
