"""Audit tool: surface what Suri actually did, sourced from the agent_actions
table. Reinforces the honesty design — when the user asks 'what did you do
today?' the answer comes from logged tool calls, not from chat recollection."""

from app import db


_WINDOWS = {
    "1h": 1,
    "today": 24,
    "24h": 24,
    "yesterday": 48,
    "7d": 24 * 7,
    "week": 24 * 7,
}


def what_did_you_do(since: str = "24h"):
    """Return tool calls made in the recent window. The agent should
    summarize the result for the user — don't just dump the raw rows."""
    key = (since or "24h").strip().lower()
    hours = _WINDOWS.get(key)
    if hours is None:
        try:
            hours = float(key.rstrip("h"))
        except ValueError:
            return {
                "ok": False,
                "error": (
                    f"unknown window {since!r}. use one of "
                    f"{sorted(_WINDOWS)} or '<N>h' (e.g. '6h')."
                ),
            }
    actions = db.recent_agent_actions(hours_back=hours, limit=200)
    return {
        "ok": True,
        "window": since,
        "hours_back": hours,
        "count": len(actions),
        "actions": actions,
    }
