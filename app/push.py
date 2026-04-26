"""Single shared place for code that needs to send a proactive (unprompted)
message to the user.

The transport layer (telegram_bot.py) registers a callback. Anything that needs
to push to the user — scheduler firing a reminder, Outlook auth prompt, future
proactive nudges — calls push.push() instead of importing telegram_bot
directly. This avoids circular imports and keeps the transport swappable
(e.g. if we later add SMS or a web UI).
"""
import sys
from typing import Callable, Optional

_callback: Optional[Callable[[str], None]] = None


def set_callback(cb: Callable[[str], None]) -> None:
    """Register the function that delivers a string to the user.
    Must be safe to call from any thread (telegram_bot's push_threadsafe is)."""
    global _callback
    _callback = cb
    print("[push] callback registered", file=sys.stderr, flush=True)


def push(message: str) -> bool:
    """Deliver `message` to the user. Returns True if delivered, False if no
    callback was registered or the callback raised. Errors are logged, not
    raised — caller should always have a fallback (typically stderr)."""
    if _callback is None:
        print(
            f"[push] no callback registered; dropping message: {message[:80]!r}",
            file=sys.stderr,
            flush=True,
        )
        return False
    try:
        _callback(message)
        return True
    except Exception as e:
        print(
            f"[push] callback raised: {type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )
        return False
