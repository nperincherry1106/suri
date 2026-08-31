"""Gmail tools — peer to app/tools/outlook.py.

Required GCP setup (per README / iOS plan): an OAuth 2.0 Web client with
`https://suri.fly.dev/connect/gmail/callback` as an authorized redirect URI,
`GMAIL_CLIENT_ID` + `GMAIL_CLIENT_SECRET` set as fly secrets. The OAuth
consent screen must list Namrita's gmail address as a test user (the app
stays in Testing mode in v0; no Google verification needed).

Functions exposed:
  - `_token()` returns a Credentials object, kicking off magic-link auth
    via push.push() and raising GmailAuthRequired if no cached token.
  - `triage_inbox(hours_back, max_results)` — recent mail, marketing flag.
  - `find_owed_replies(days_threshold, lookback_days)` — threads where she
    was addressed and the latest message isn't from her.
  - `find_marketing_senders()` — aggregated by sender domain via
    List-Unsubscribe header.
  - `complete_auth_code_flow(flow_state, auth_response_url)` — invoked by
    the OAuth callback in app/oauth_server.py.
  - `has_valid_token()`, `account_username()` — used by the
    'connected accounts' block in the system prompt.

Like outlook.py we deliberately don't request `gmail.send` — we use
`gmail.modify` (covers read, create drafts, label, trash) so Namrita
opens drafts in Gmail and clicks Send herself. Highest-risk action
stays out of the autonomous loop.
"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app import db, push


SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    # gmail.modify includes the read + label + trash + drafts surface we
    # need; settings.basic is gmail's equivalent of MailboxSettings.ReadWrite
    # (used for managing filters — gmail's analog of inbox rules).
]

# Cached credentials — google-auth's Credentials is JSON-serializable.
ROOT = Path(__file__).parent.parent.parent
_DATA_DIR = Path(os.environ.get("SURI_DATA_DIR", ROOT))
TOKEN_PATH = _DATA_DIR / "gmail_token.json"


class GmailAuthRequired(Exception):
    """Raised by _token() when there's no cached creds and we've handed the
    user a magic link to complete OAuth. Mirrors OutlookAuthRequired so
    insights / api can catch a single .auth_url-bearing exception type
    per provider and translate to a structured 409."""

    def __init__(self, state: str, auth_url: str):
        super().__init__(f"gmail auth required (state={state})")
        self.state = state
        self.auth_url = auth_url


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------


def _client_config() -> dict | None:
    """Read GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET from env. Returns None if
    not configured — callers translate to a 503 / "not connected" hint."""
    cid = (os.environ.get("GMAIL_CLIENT_ID") or "").strip()
    sec = (os.environ.get("GMAIL_CLIENT_SECRET") or "").strip()
    if not cid or not sec:
        return None
    return {
        "web": {
            "client_id": cid,
            "client_secret": sec,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        }
    }


def is_configured() -> bool:
    return _client_config() is not None


def _redirect_uri() -> str | None:
    base = os.environ.get("SURI_PUBLIC_URL")
    if not base:
        return None
    return base.rstrip("/") + "/connect/gmail/callback"


def _persist_creds(creds: Credentials) -> None:
    """google-auth's Credentials.to_json() round-trips cleanly back through
    Credentials.from_authorized_user_info()."""
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())


def _load_creds() -> Credentials | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        info = json.loads(TOKEN_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    try:
        return Credentials.from_authorized_user_info(info, SCOPES)
    except Exception as e:
        print(f"[gmail] failed to load cached creds: {e}", file=sys.stderr, flush=True)
        return None


def _start_magic_link_flow() -> None:
    """Initiate the OAuth web flow, persist the state, and push a one-tap
    magic link. Then raise GmailAuthRequired so the caller (insights /
    api) bails out cleanly."""
    cfg = _client_config()
    if cfg is None:
        raise RuntimeError(
            "missing GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET. set them as fly "
            "secrets after creating an OAuth Web client in Google Cloud Console."
        )
    redirect_uri = _redirect_uri()
    if not redirect_uri:
        raise RuntimeError(
            "SURI_PUBLIC_URL not set; can't build the gmail OAuth callback URL."
        )

    flow = Flow.from_client_config(cfg, scopes=SCOPES, redirect_uri=redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",         # required for refresh_token
        prompt="consent",              # force the consent screen so we ALWAYS
                                       # get a refresh_token (Google omits it
                                       # on subsequent grants without prompt=consent)
        include_granted_scopes="true", # incremental consent: don't re-ask for
                                       # already-granted scopes
    )

    recent = db.recent_messages(1)
    original_prompt = recent[0][1] if recent and recent[0][0] == "inbound" else None

    # Persist auth_url (so /connect/gmail/{state} can 302 to the exact
    # consent URL — recreating the Flow in oauth_server would generate
    # a fresh PKCE pair and break code_challenge matching) and the
    # code_verifier (so fetch_token in the callback can prove possession
    # of the verifier whose SHA256 was sent as code_challenge here).
    db.create_pending_oauth(
        state=state,
        provider="gmail",
        flow_json=json.dumps({
            "scopes": SCOPES,
            "redirect_uri": redirect_uri,
            "auth_url": auth_url,
            "code_verifier": flow.code_verifier,
        }),
        telegram_user_id=0,  # vestigial column, see outlook.py
        original_prompt=original_prompt,
    )

    base = (os.environ.get("SURI_PUBLIC_URL") or "").rstrip("/")
    short_link = f"{base}/connect/gmail/{state}" if base else auth_url

    push.push(
        "first time on gmail — i need your google sign-in to read your inbox.\n\n"
        f"tap to connect: {short_link}\n\n"
        "one tap, ~30 seconds. i can read mail and save drafts — i never send "
        "as you. once you're done i'll pick up where we left off."
    )
    print(
        f"[gmail-auth] magic link issued, state={state}",
        file=sys.stderr,
        flush=True,
    )
    raise GmailAuthRequired(state=state, auth_url=short_link)


def complete_auth_code_flow(flow_state: dict, auth_response_url: str) -> dict:
    """Exchange the authorization_response (full callback URL with ?code=...)
    for tokens. Returns a small status dict; persists creds to disk on
    success. Called by the /connect/gmail/callback handler.

    Critical: must restore the SAME code_verifier that was used to mint
    the code_challenge in the authorize step (PKCE) — otherwise google
    returns invalid_grant: 'Missing code verifier'. The start handler
    persists it into flow_state for us."""
    cfg = _client_config()
    if cfg is None:
        return {"ok": False, "error": "gmail not configured server-side"}
    redirect_uri = flow_state.get("redirect_uri") or _redirect_uri()
    flow = Flow.from_client_config(
        cfg,
        scopes=flow_state.get("scopes") or SCOPES,
        redirect_uri=redirect_uri,
    )
    saved_verifier = flow_state.get("code_verifier")
    if saved_verifier:
        flow.code_verifier = saved_verifier
    flow.fetch_token(authorization_response=auth_response_url)
    creds = flow.credentials
    _persist_creds(creds)
    print(
        f"[gmail-auth] token exchanged + persisted (refresh={'yes' if creds.refresh_token else 'no'})",
        file=sys.stderr,
        flush=True,
    )
    return {"ok": True, "has_refresh_token": bool(creds.refresh_token)}


def _token() -> Credentials:
    """Return valid Credentials, refreshing if needed, kicking off OAuth
    via magic link if we have no cached token. Raises GmailAuthRequired
    in that case (callers translate to 409 outlook_auth_required-style)."""
    creds = _load_creds()
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            _persist_creds(creds)
            return creds
        except Exception as e:
            print(
                f"[gmail-auth] refresh failed, falling through to re-auth: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
                flush=True,
            )
    _start_magic_link_flow()
    raise GmailAuthRequired(state="<unreachable>", auth_url="")


def _service():
    """Build a Gmail API client. cache_discovery=False to avoid the
    file-cache deprecation warning + write to /tmp on read-only FS."""
    creds = _token()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def has_valid_token() -> bool:
    """Used by the system-prompt 'connected accounts' block. Avoids
    triggering a refresh — just checks the on-disk JSON for a refresh_token
    (which means we can re-auth silently when needed)."""
    if not TOKEN_PATH.exists():
        return False
    try:
        info = json.loads(TOKEN_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return bool(info.get("refresh_token"))


def account_username() -> str | None:
    """Cached after first successful API call so the system-prompt path
    doesn't pay a network round-trip per turn."""
    global _username_cache
    if _username_cache:
        return _username_cache
    if not has_valid_token():
        return None
    try:
        svc = _service()
        prof = svc.users().getProfile(userId="me").execute()
        _username_cache = prof.get("emailAddress")
        return _username_cache
    except Exception:
        return None


_username_cache: str | None = None


# ---------------------------------------------------------------------------
# header parsing helpers (Gmail returns headers as list of {name, value})
# ---------------------------------------------------------------------------


def _headers_to_dict(headers: list) -> dict[str, str]:
    """Lowercase header-name keyed dict. Gmail's metadata format gives us
    one entry per header even for multi-value cases; we keep the LAST one
    when there are dupes (matches typical mail header semantics)."""
    out: dict[str, str] = {}
    for h in headers or []:
        name = (h.get("name") or "").lower()
        if name:
            out[name] = h.get("value") or ""
    return out


def _parse_address_list(s: str) -> list[tuple[str, str]]:
    """Returns list of (display_name, email) tuples. Handles RFC 5322
    address lists like 'Foo <foo@x.com>, Bar <bar@y.com>' tolerantly."""
    if not s:
        return []
    out: list[tuple[str, str]] = []
    # Split on commas not inside quoted strings — good enough for v0
    # since pathological cases are vanishingly rare in personal email.
    for chunk in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", s):
        name, addr = parseaddr(chunk.strip())
        if addr:
            out.append((name or "", addr.lower()))
    return out


def _extract_unsubscribe(headers: dict[str, str]) -> tuple[str | None, bool]:
    """Returns (url, post_supported). Mirrors outlook._extract_unsubscribe."""
    raw = headers.get("list-unsubscribe") or ""
    if not raw:
        return None, False
    post_supported = (
        (headers.get("list-unsubscribe-post") or "").strip().lower()
        == "list-unsubscribe=one-click"
    )
    # Pull the first https URL out of the angle-bracket list.
    for m in re.finditer(r"<\s*([^>]+?)\s*>", raw):
        candidate = m.group(1).strip()
        if candidate.lower().startswith("https://"):
            return candidate, post_supported
        if candidate.lower().startswith("mailto:"):
            # Keep mailto: as a fallback if no http url appears later.
            mailto = candidate
            for m2 in re.finditer(r"<\s*([^>]+?)\s*>", raw):
                c2 = m2.group(1).strip()
                if c2.lower().startswith("https://"):
                    return c2, post_supported
            return mailto, False
    return None, post_supported


def _parsed_received_at(date_header: str) -> datetime | None:
    if not date_header:
        return None
    try:
        return parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# main read tools
# ---------------------------------------------------------------------------


_METADATA_HEADERS = [
    "From", "To", "Cc", "Subject", "Date",
    "List-Unsubscribe", "List-Unsubscribe-Post",
    "Message-ID", "In-Reply-To", "References",
]


def _list_inbox_message_ids(svc, after_date_str: str, max_results: int) -> list[str]:
    """Gmail's list returns ids+threadIds; we then fan out to .get() per
    id to pull headers. `q` uses Gmail search syntax; `after:` takes
    YYYY/MM/DD. Caps at 500 (Gmail's per-page max)."""
    resp = (
        svc.users()
        .messages()
        .list(
            userId="me",
            q=f"in:inbox after:{after_date_str}",
            maxResults=min(max_results, 100),
        )
        .execute()
    )
    return [m["id"] for m in resp.get("messages") or []]


def _get_message_metadata(svc, msg_id: str) -> dict | None:
    """Single message fetch with metadata format — no body, just headers
    + label IDs + snippet. ~1 RTT each; we accept the latency since the
    iOS Inbox tab won't show more than 30-50 items."""
    try:
        return (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=_METADATA_HEADERS,
            )
            .execute()
        )
    except HttpError as e:
        print(f"[gmail] get message {msg_id} failed: {e}", file=sys.stderr, flush=True)
        return None


def triage_inbox(hours_back: int = 24, max_results: int = 30) -> list[dict]:
    """Recent inbox slice with marketing flag. Same return shape as
    outlook.triage_inbox so the iOS app + insights can fan-in cleanly."""
    svc = _service()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    after = cutoff.strftime("%Y/%m/%d")
    ids = _list_inbox_message_ids(svc, after, max_results)

    out: list[dict] = []
    for msg_id in ids:
        m = _get_message_metadata(svc, msg_id)
        if not m:
            continue
        headers = _headers_to_dict((m.get("payload") or {}).get("headers") or [])
        from_addr = _parse_address_list(headers.get("from", ""))
        from_name, from_email = (from_addr[0] if from_addr else ("", ""))
        url, _ = _extract_unsubscribe(headers)
        received_at = _parsed_received_at(headers.get("date", ""))
        # Filter out anything older than cutoff (Gmail's `after:` is
        # date-granular, not time-granular, so we double-check here).
        if received_at and received_at < cutoff:
            continue
        out.append({
            "message_id": m.get("id"),
            "conversation_id": m.get("threadId"),
            "from_name": from_name or None,
            "from_email": from_email or None,
            "subject": headers.get("subject"),
            "snippet": (m.get("snippet") or "")[:255],
            "received_at": received_at.isoformat() if received_at else headers.get("date"),
            "is_unread": "UNREAD" in (m.get("labelIds") or []),
            "is_marketing": url is not None,
            "source": "gmail",
        })
    return out


def find_owed_replies(days_threshold: int = 2, lookback_days: int = 14) -> list[dict]:
    """Threads where Namrita was addressed (to/cc), the most recent message
    is from someone else, and it's older than `days_threshold` days. Same
    return shape as outlook.find_owed_replies for fan-in."""
    svc = _service()
    me = account_username()
    if not me:
        return {
            "ok": False,
            "error": "could not resolve own gmail address (auth or scope issue)",
        }
    me = me.lower()

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    after = cutoff.strftime("%Y/%m/%d")
    threshold = datetime.now(timezone.utc) - timedelta(days=days_threshold)

    # Fan out per-message rather than per-thread for parity with the
    # outlook implementation — we still de-dupe by threadId at the end.
    ids = _list_inbox_message_ids(svc, after, 200)
    by_thread: dict[str, list[dict]] = defaultdict(list)
    for msg_id in ids:
        m = _get_message_metadata(svc, msg_id)
        if not m:
            continue
        headers = _headers_to_dict((m.get("payload") or {}).get("headers") or [])
        if _extract_unsubscribe(headers)[0]:
            # Skip marketing — not a "thread you owe a reply on."
            continue
        from_addr = _parse_address_list(headers.get("from", ""))
        from_email = from_addr[0][1] if from_addr else ""
        if from_email == me:
            # Latest message from her — she's not blocked on a reply.
            continue
        recipients = (
            _parse_address_list(headers.get("to", ""))
            + _parse_address_list(headers.get("cc", ""))
        )
        if not any(addr == me for _, addr in recipients):
            # She wasn't directly addressed (probably a CC/BCC blast); skip.
            continue
        received_at = _parsed_received_at(headers.get("date", ""))
        if received_at is None or received_at > threshold:
            continue
        by_thread[m.get("threadId")].append({
            "message_id": m.get("id"),
            "from_name": from_addr[0][0] if from_addr else None,
            "from_email": from_email,
            "subject": headers.get("subject"),
            "received_at": received_at.isoformat(),
            "days_waiting": (datetime.now(timezone.utc) - received_at).days,
            "source": "gmail",
        })

    # Keep just the most recent message per thread; sort by days_waiting desc.
    out: list[dict] = []
    for tid, msgs in by_thread.items():
        msgs.sort(key=lambda m: m["received_at"], reverse=True)
        latest = msgs[0]
        latest["thread_id"] = tid
        out.append(latest)
    out.sort(key=lambda r: r["days_waiting"], reverse=True)
    return out


def find_marketing_senders() -> list[dict]:
    """Aggregate marketing senders across the inbox by domain. Same return
    shape as outlook.find_marketing_senders so the unified API endpoint
    can merge across both providers."""
    svc = _service()
    # No date cap here — newsletters from 6 months ago are still useful
    # signal for "should I unsubscribe." Cap at 500 message scan.
    resp = (
        svc.users()
        .messages()
        .list(userId="me", q="in:inbox", maxResults=500)
        .execute()
    )
    ids = [m["id"] for m in resp.get("messages") or []]

    by_sender: dict[str, dict] = defaultdict(lambda: {
        "name": None,
        "subjects": [],
        "dates": [],
        "url": None,
        "post_supported": False,
        "count": 0,
    })

    for msg_id in ids:
        m = _get_message_metadata(svc, msg_id)
        if not m:
            continue
        headers = _headers_to_dict((m.get("payload") or {}).get("headers") or [])
        url, post_supported = _extract_unsubscribe(headers)
        if not url:
            continue
        from_addr = _parse_address_list(headers.get("from", ""))
        if not from_addr:
            continue
        from_name, from_email = from_addr[0]
        domain = from_email.split("@")[-1] if "@" in from_email else from_email
        received_at = _parsed_received_at(headers.get("date", ""))
        bucket = by_sender[domain]
        bucket["count"] += 1
        if received_at:
            bucket["dates"].append(received_at)
        bucket["subjects"].append(headers.get("subject", ""))
        if not bucket["name"]:
            bucket["name"] = from_name or None
        # First (most recent in id-list ordering) URL wins — usually the
        # current preference-center URL for that sender.
        if bucket["url"] is None:
            bucket["url"] = url
            bucket["post_supported"] = post_supported

    out: list[dict] = []
    for domain, b in by_sender.items():
        latest = max(b["dates"]) if b["dates"] else None
        out.append({
            "sender_domain": domain,
            "name": b["name"],
            "unsubscribe_url": b["url"],
            "post_supported": b["post_supported"],
            "email_count": b["count"],
            "last_seen": latest.isoformat() if latest else None,
            "sample_subjects": b["subjects"][:3],
            "source": "gmail",
        })
    out.sort(key=lambda r: r["email_count"], reverse=True)
    return out
