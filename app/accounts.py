"""Derived view of which integrations Suri currently has access to.

This is intentionally NOT a separate database table — the source of truth is
each provider's token cache on disk. Adding a parallel DB row would create
a sync invariant ("row says connected, token is gone" = lying to the user).
We probe the actual sources every turn, the same way _ground_truth_block()
queries the messages/reminders/etc. tables fresh.

Promote to a real `connected_accounts` table the moment any of these is
true:
  - we add a 2nd provider (uniform "list connections" view becomes useful)
  - we go multi-user (need per-user account ownership)
  - we want a real disconnect / revoke / connection-history audit log

Until then, this 30-line module is the right shape.
"""
from app.tools import outlook


def connected() -> list[dict]:
    """Return one dict per connected provider, with account_email + scopes."""
    out = []
    if outlook.has_valid_token():
        out.append(
            {
                "provider": "outlook",
                "account_email": outlook.cached_account_email(),
                "scopes": list(outlook.SCOPES),
            }
        )
    return out


def status_block() -> str:
    """Plain-text block injected into the system prompt every turn so Suri
    knows what she has access to before she says she does (or doesn't)."""
    accts = connected()
    if not accts:
        return (
            "Connected accounts: NONE.\n\n"
            "You have no integrations connected yet. Email, calendar, etc. "
            "are unavailable until she connects them. The connect flow is "
            "automatic — the first time you call a provider tool, Suri's "
            "OAuth handler pushes a one-tap link to her chat. Don't claim "
            "you can do anything that needs a connection she hasn't done."
        )
    lines = "\n".join(
        f"  - {a['provider']} ({a['account_email'] or 'unknown account'}) "
        f"— scopes: {', '.join(a['scopes'])}"
        for a in accts
    )
    return (
        "Connected accounts (this is REALITY — defer to it over your memory):\n"
        f"{lines}"
    )
