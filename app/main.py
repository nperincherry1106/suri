"""Process entrypoint for Suri (replaces app.telegram_bot).

Suri is now an iOS-app-first product:
  - inbound user messages: arrive via /api/v1/chat/send (added in step ~10
    of the iOS plan; until then there's no inbound surface other than
    OAuth callbacks and Plaid webhooks)
  - outbound proactive pushes: APNs (aioapns) when APNS_* env vars are set,
    otherwise a stderr stub
  - OAuth callbacks + Plaid webhooks: same FastAPI surface as before
    (oauth_server.app)
  - scheduled jobs: APScheduler in its own thread (proactive briefs,
    reminders) — unchanged

There is no long-poll loop, no Telegram dependency, no in-memory chat state.
"""
import os
import sys

import uvicorn

from app import db, oauth_server, push, scheduler


def _stub_push_callback(text: str) -> None:
    """Stderr-only fallback for dev / not-yet-configured deploys. Prints
    every proactive push so fly logs remain the source of truth for
    'did the scheduler fire'."""
    print(f"[push:stub] {text}", file=sys.stderr, flush=True)


def _select_push_callback() -> None:
    """Pick the best transport at boot. APNs if all four required env vars
    are present (the .p8 isn't validated here — we let the first send
    surface a malformed key in fly logs rather than crashing the boot)."""
    ok, missing = push.apns_env_configured()
    if ok:
        push.set_callback(push.apns_sync_callback)
        print(
            f"[push] apns transport active "
            f"(bundle={os.environ['APNS_BUNDLE_ID']}, "
            f"team={os.environ['APNS_TEAM_ID']}, key={os.environ['APNS_KEY_ID']})",
            file=sys.stderr,
            flush=True,
        )
    else:
        push.set_callback(_stub_push_callback)
        print(
            f"[push] apns env not configured (missing: {', '.join(missing)}); "
            f"using stderr stub",
            file=sys.stderr,
            flush=True,
        )


def main() -> None:
    db.init()
    scheduler.start()
    _select_push_callback()
    scheduler.restore_pending()

    port = int(os.environ.get("PORT", "8080"))
    print(
        f"[suri] starting fastapi on 0.0.0.0:{port}",
        file=sys.stderr,
        flush=True,
    )
    try:
        uvicorn.run(
            oauth_server.app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            access_log=True,
            # Fly.io terminates TLS at the edge and forwards plain HTTP to the
            # app machine. Without these, request.url.scheme reports "http"
            # for every request, which breaks anything that round-trips an
            # absolute URL — including Google's OAuth callback (oauthlib
            # raises InsecureTransportError if the auth-response URL isn't
            # https). proxy_headers is on by default in newer uvicorn but
            # forwarded_allow_ips defaults to 127.0.0.1 — fly's proxy is on
            # a different IP, so its X-Forwarded-Proto header gets dropped.
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
