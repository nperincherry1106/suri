"""Single shared place for code that needs to send a proactive (unprompted)
message to the user.

The transport callback is registered at process start (see app/main.py).
Two transports today:
  - stderr stub: dev / no APNs envs configured
  - APNs (aioapns):  every active device row in `devices` gets the alert

Anything that needs to push — scheduler firing a reminder, Outlook auth
confirmation, Plaid webhook acknowledgements, future proactive nudges —
calls push.push() / push.push_async() and stays decoupled from the transport.

Threading model:
  - `set_callback(cb)` accepts a SYNC callable; `cb(text)` may be invoked
    from any thread (scheduler thread, FastAPI worker thread via
    asyncio.to_thread).
  - For the APNs transport the callback bridges sync -> async by spinning
    a one-shot event loop with `asyncio.run`. Both the scheduler thread
    and `asyncio.to_thread`'s worker have no current event loop, so this
    is safe. We don't try to keep the aioapns HTTP/2 connection warm
    across calls — push volume in a single-user app is so low (a handful
    per day) that connection reuse buys nothing and complicates lifecycle.
"""
import asyncio
import os
import sys
from typing import Callable, Optional

_callback: Optional[Callable[[str], None]] = None


def set_callback(cb: Callable[[str], None]) -> None:
    """Register the function that delivers a string to the user.
    Must be safe to call from any thread."""
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


async def push_async(message: str) -> bool:
    """Async wrapper for callers on an asyncio event loop (FastAPI handlers).
    Bridges to the sync callback through a worker thread so the request loop
    isn't blocked while aioapns talks HTTP/2 to Apple."""
    return await asyncio.to_thread(push, message)


# ---------------------------------------------------------------------------
# APNs transport
# ---------------------------------------------------------------------------


APNS_REQUIRED_ENV = ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_BUNDLE_ID", "APNS_AUTH_KEY_P8")


def apns_env_configured() -> tuple[bool, list[str]]:
    """Return (ok, missing_keys). ok==True means we can build APNs clients."""
    missing = [k for k in APNS_REQUIRED_ENV if not (os.environ.get(k) or "").strip()]
    return (not missing, missing)


def _normalize_p8(raw: str) -> str:
    """Fly secrets sometimes come in with the literal characters '\\n' instead
    of real newlines (depending on how the secret was set). aioapns / pyjwt
    will reject the key if BEGIN/END markers aren't on their own lines, so
    normalize defensively."""
    s = (raw or "").strip()
    if "\\n" in s and "\n" not in s:
        s = s.replace("\\n", "\n")
    return s


async def _send_to_apns(message: str) -> None:
    """Fan `message` out to every active device row. Best-effort — failures
    are logged per-device, never raised. APNs returns 410 Unregistered when
    the app has been uninstalled or notifications were revoked; we mark
    those tokens inactive so we stop wasting Apple's bandwidth."""
    from aioapns import APNs, NotificationRequest, PushType  # lazy: skip when stub

    from app import db

    devices = db.list_active_devices()
    if not devices:
        print(
            f"[push:apns] no active devices, dropping: {message[:80]!r}",
            file=sys.stderr,
            flush=True,
        )
        return

    key_id = os.environ["APNS_KEY_ID"].strip()
    team_id = os.environ["APNS_TEAM_ID"].strip()
    bundle = os.environ["APNS_BUNDLE_ID"].strip()
    p8 = _normalize_p8(os.environ["APNS_AUTH_KEY_P8"])

    by_env: dict[str, list[dict]] = {}
    for d in devices:
        by_env.setdefault((d.get("apns_env") or "production").lower(), []).append(d)

    for env, ds in by_env.items():
        use_sandbox = env == "sandbox"
        try:
            client = APNs(
                key=p8,
                key_id=key_id,
                team_id=team_id,
                topic=bundle,
                use_sandbox=use_sandbox,
            )
        except Exception as e:
            print(
                f"[push:apns] failed to build client (env={env}): "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
                flush=True,
            )
            continue

        for d in ds:
            tok = d["apns_token"]
            req = NotificationRequest(
                device_token=tok,
                message={"aps": {"alert": message, "sound": "default"}},
                push_type=PushType.ALERT,
            )
            try:
                resp = await client.send_notification(req)
            except Exception as e:
                print(
                    f"[push:apns] send raised env={env} token={tok[:12]}…: "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            ok = bool(getattr(resp, "is_successful", False))
            status = getattr(resp, "status", None)
            desc = getattr(resp, "description", None)
            if ok:
                print(
                    f"[push:apns] delivered env={env} token={tok[:12]}…",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            # 410 Unregistered = uninstalled or notifications revoked.
            # BadDeviceToken can also happen if a sandbox token was sent to
            # production (or vice versa) — we deactivate either way; the
            # device will re-register with the right env when she reopens
            # the app.
            if str(status) == "410" or desc in ("Unregistered", "BadDeviceToken"):
                db.deactivate_device(tok)
                print(
                    f"[push:apns] deactivated env={env} token={tok[:12]}… "
                    f"status={status} desc={desc}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"[push:apns] failed env={env} token={tok[:12]}… "
                    f"status={status} desc={desc}",
                    file=sys.stderr,
                    flush=True,
                )


def apns_sync_callback(message: str) -> None:
    """Sync callback that bridges into the async APNs send. Spawns a one-shot
    event loop per call — fine for the few-pushes-per-day volume of a
    single-user app, and avoids any cross-thread loop bookkeeping."""
    try:
        asyncio.run(_send_to_apns(message))
    except Exception as e:
        print(
            f"[push:apns] sync bridge raised: {type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )
