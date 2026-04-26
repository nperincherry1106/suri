"""Microsoft Outlook (Graph API) tools.

Required Azure app registration delegated API permissions:
  - Mail.ReadWrite          (read inbox, create drafts, soft-delete, move)
  - MailboxSettings.ReadWrite (create/list/delete inbox rules)

We deliberately do NOT request Mail.Send. Suri prepares drafts; the user opens
them in Outlook and clicks Send herself. This keeps the highest-risk action
(sending email as her) out of the autonomous loop.
"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import msal
import requests

from app import db

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Mail.ReadWrite", "MailboxSettings.ReadWrite"]
GRAPH = "https://graph.microsoft.com/v1.0"
ROOT = Path(__file__).parent.parent.parent
# Token cache lives next to the SQLite db (so on fly it lands on the volume).
_DATA_DIR = Path(os.environ.get("SURI_DATA_DIR", ROOT))
TOKEN_PATH = _DATA_DIR / "outlook_token.json"


def _token() -> str:
    cache = msal.SerializableTokenCache()
    if TOKEN_PATH.exists():
        cache.deserialize(TOKEN_PATH.read_text())

    client_id = os.environ.get("MS_CLIENT_ID")
    if not client_id:
        raise RuntimeError(
            "missing MS_CLIENT_ID in .env. register an Azure app and put the "
            "Application (client) ID there."
        )

    app = msal.PublicClientApplication(
        client_id, authority=AUTHORITY, token_cache=cache
    )

    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result:
        # On a headless server (no display, no browser) the interactive flow
        # would crash trying to open a browser tab. Use device-code flow
        # instead: print a URL + short code to stderr, user opens it on their
        # phone/laptop and enters the code. Token then persists to the volume
        # so this only happens on first deploy + every ~90 days when refresh
        # tokens expire.
        if os.environ.get("SURI_HEADLESS") == "1":
            flow = app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise RuntimeError(
                    f"failed to start MS device-code flow: {flow}"
                )
            print(
                "\n=== OUTLOOK AUTH REQUIRED ===\n"
                f"{flow['message']}\n"
                "Suri is now blocked until you complete this. "
                "After auth, the token is cached to the persistent volume.\n",
                file=sys.stderr,
                flush=True,
            )
            result = app.acquire_token_by_device_flow(flow)
        else:
            result = app.acquire_token_interactive(SCOPES)

    if cache.has_state_changed:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(cache.serialize())

    if "access_token" not in result:
        raise RuntimeError(
            f"outlook auth failed: {result.get('error_description', result)}"
        )
    return result["access_token"]


def _extract_unsubscribe(headers_list):
    """Return (url, post_supported) from List-Unsubscribe headers, or (None, False)."""
    url = None
    post_supported = False
    for h in headers_list or []:
        name = (h.get("name") or "").lower()
        value = h.get("value") or ""
        if name == "list-unsubscribe":
            for item in value.split(","):
                item = item.strip().strip("<>").strip()
                if item.lower().startswith("http"):
                    url = item
                    break
        elif name == "list-unsubscribe-post" and "one-click" in value.lower():
            post_supported = True
    return url, post_supported


def find_marketing_senders():
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "$top": "500",
        "$select": "from,subject,receivedDateTime,id,internetMessageHeaders",
        "$orderby": "receivedDateTime desc",
    }
    r = requests.get(
        f"{GRAPH}/me/mailFolders/inbox/messages",
        headers=headers,
        params=params,
        timeout=60,
    )
    r.raise_for_status()
    messages = r.json().get("value", [])

    by_sender = defaultdict(
        lambda: {
            "name": None,
            "subjects": [],
            "dates": [],
            "url": None,
            "post_supported": False,
            "count": 0,
        }
    )
    for msg in messages:
        url, post_supported = _extract_unsubscribe(msg.get("internetMessageHeaders"))
        if not url:
            continue
        from_field = msg.get("from", {}).get("emailAddress", {})
        addr = (from_field.get("address") or "").lower()
        if not addr:
            continue
        domain = addr.split("@")[-1]
        try:
            d = datetime.fromisoformat(
                msg["receivedDateTime"].replace("Z", "+00:00")
            )
        except Exception:
            continue
        bucket = by_sender[domain]
        bucket["count"] += 1
        bucket["dates"].append(d)
        bucket["subjects"].append(msg.get("subject", ""))
        if not bucket["name"]:
            bucket["name"] = from_field.get("name")
        # most recent email's URL wins (first one we see, since we ordered desc)
        if bucket["url"] is None:
            bucket["url"] = url
            bucket["post_supported"] = post_supported

    results = []
    for domain, b in by_sender.items():
        latest = max(b["dates"])
        db.upsert_marketing_sender(
            sender_domain=domain,
            service_name=b["name"],
            unsubscribe_url=b["url"],
            post_supported=b["post_supported"],
            last_seen=latest.isoformat(),
            email_count=b["count"],
        )
        results.append(
            {
                "service_name": b["name"] or domain,
                "sender_domain": domain,
                "email_count": b["count"],
                "last_seen": latest.date().isoformat(),
                "sample_subjects": b["subjects"][:3],
                "one_click_supported": b["post_supported"],
            }
        )
    results.sort(key=lambda r: (r["email_count"], r["last_seen"]), reverse=True)
    return results[:20]


# Page-text phrases that indicate the unsubscribe already happened.
_UNSUB_SUCCESS_PHRASES = [
    "you have been unsubscribed",
    "you've been unsubscribed",
    "successfully unsubscribed",
    "unsubscribe successful",
    "you've been removed",
    "you have been removed",
    "preferences saved",
    "preferences have been saved",
    "preferences updated",
    "you will no longer receive",
    "you'll no longer receive",
    "your subscription has been cancelled",
    "your subscription has been canceled",
    "no longer receive emails",
    "removed from our mailing list",
    "removed from our list",
    "we've updated your preferences",
    "thanks, you have been unsubscribed",
]

# Button/link text we'll try clicking, in priority order. More specific wins.
_UNSUB_CLICK_PHRASES = [
    "yes, unsubscribe",
    "confirm unsubscribe",
    "confirm",
    "unsubscribe me",
    "unsubscribe from all",
    "unsubscribe from all emails",
    "unsubscribe",
    "opt out",
    "opt-out",
    "remove me",
    "remove from list",
    "i want to unsubscribe",
    "stop sending",
    "submit",
]


def _page_indicates_success(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _UNSUB_SUCCESS_PHRASES)


def _browser_unsubscribe(url: str) -> dict:
    """Open the unsubscribe URL in headless Chromium and try to click any
    confirm/unsubscribe button. Best-effort.

    Returns:
      {completed: True,  method: str, final_url: str}                — success
      {completed: False, error: str, final_url?: str, action_url?: str}  — give up
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "completed": False,
            "error": "playwright not installed (run: pip install playwright && playwright install chromium)",
            "action_url": url,
        }

    print(f"[unsub-browser] {url}", file=sys.stderr, flush=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                )
                page = ctx.new_page()
                try:
                    page.goto(url, timeout=20_000, wait_until="domcontentloaded")
                except Exception as e:
                    return {
                        "completed": False,
                        "error": f"could not load page: {type(e).__name__}: {e}",
                        "action_url": url,
                    }

                # Some pages auto-confirm on load. Wait briefly, check.
                page.wait_for_timeout(1500)
                if _page_indicates_success(page.content()):
                    return {
                        "completed": True,
                        "method": "browser-auto-confirm",
                        "final_url": page.url,
                    }

                # Try each phrase: button by role, then submit input by value, then link.
                for phrase in _UNSUB_CLICK_PHRASES:
                    clicked_via = None

                    # 1. accessible button by name
                    try:
                        btn = page.get_by_role(
                            "button", name=re.compile(phrase, re.I)
                        ).first
                        if btn.count() > 0 and btn.is_visible(timeout=300):
                            btn.click(timeout=3000)
                            clicked_via = f"button:{phrase}"
                    except Exception:
                        pass

                    # 2. submit input with matching value
                    if not clicked_via:
                        try:
                            sub = page.locator(
                                f"input[type='submit' i][value*='{phrase}' i], "
                                f"input[type='button' i][value*='{phrase}' i]"
                            ).first
                            if sub.count() > 0 and sub.is_visible(timeout=300):
                                sub.click(timeout=3000)
                                clicked_via = f"submit:{phrase}"
                        except Exception:
                            pass

                    # 3. any link with matching text (e.g. "Unsubscribe" anchor)
                    if not clicked_via:
                        try:
                            link = page.get_by_role(
                                "link", name=re.compile(phrase, re.I)
                            ).first
                            if link.count() > 0 and link.is_visible(timeout=300):
                                link.click(timeout=3000)
                                clicked_via = f"link:{phrase}"
                        except Exception:
                            pass

                    if clicked_via:
                        # Wait for navigation/JS to settle; check for success language.
                        try:
                            page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                        page.wait_for_timeout(1000)
                        final_html = page.content()
                        if _page_indicates_success(final_html):
                            return {
                                "completed": True,
                                "method": f"browser-{clicked_via}-confirmed",
                                "final_url": page.url,
                            }
                        # Clicked something but no success language — assume it
                        # worked but mark as unconfirmed so the agent surfaces
                        # the URL for manual verification.
                        return {
                            "completed": True,
                            "method": f"browser-{clicked_via}-unconfirmed",
                            "final_url": page.url,
                        }

                return {
                    "completed": False,
                    "error": "no confirm/unsubscribe button found on page",
                    "final_url": page.url,
                    "action_url": url,
                }
            finally:
                browser.close()
    except Exception as e:
        return {
            "completed": False,
            "error": f"browser launch failed: {type(e).__name__}: {e} (try: playwright install chromium)",
            "action_url": url,
        }


def unsubscribe_from(sender_domain: str):
    """Try hard to actually unsubscribe. Order:
      1. RFC 8058 one-click POST (if List-Unsubscribe-Post header was present)
      2. Headless-browser confirm-button click on the unsubscribe page
      3. Return the URL so the user can click it themselves
    """
    query = sender_domain.lower().strip()

    sender = db.get_marketing_sender(query)
    if sender is None:
        matches = db.find_marketing_senders_match(query)
        if len(matches) == 0:
            return {
                "ok": False,
                "sender_domain": query,
                "error": "no recent marketing email found matching that. run find_marketing_senders first or try a different name/domain.",
            }
        if len(matches) > 1:
            return {
                "ok": False,
                "sender_domain": query,
                "error": "ambiguous — multiple senders match. call again with one of the exact sender_domain values from `candidates`.",
                "candidates": [
                    {"sender_domain": m["sender_domain"], "service_name": m["service_name"]}
                    for m in matches
                ],
            }
        sender = matches[0]

    resolved_domain = sender["sender_domain"]

    if sender["status"] == "unsubscribed":
        return {
            "ok": True,
            "sender_domain": resolved_domain,
            "note": "already unsubscribed previously.",
        }

    url = sender["unsubscribe_url"]

    # ---- Method 1: RFC 8058 one-click POST -------------------------------
    if sender["post_supported"]:
        try:
            resp = requests.post(
                url,
                data="List-Unsubscribe=One-Click",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            if 200 <= resp.status_code < 400:
                db.set_marketing_sender_status(resolved_domain, "unsubscribed")
                return {
                    "ok": True,
                    "sender_domain": resolved_domain,
                    "method": "rfc8058-one-click-post",
                    "status_code": resp.status_code,
                    "verification": "server-ack-only",
                    "note": (
                        "Server returned 200 to a valid one-click POST, which "
                        "means the request was received and accepted. We can't "
                        "verify the sender will actually honor it until their "
                        "next batch arrives (or doesn't). Most reputable "
                        "marketers honor this within 1-7 days. If emails keep "
                        "coming in 2 weeks, suggest block_sender as a fallback."
                    ),
                }
            # one-click failed — fall through to browser
            print(
                f"[unsub] one-click POST returned {resp.status_code}, falling back to browser",
                file=sys.stderr,
                flush=True,
            )
        except Exception as e:
            print(
                f"[unsub] one-click POST raised {type(e).__name__}: {e}, falling back to browser",
                file=sys.stderr,
                flush=True,
            )

    # ---- Method 2: headless browser, click confirm button ----------------
    browser_result = _browser_unsubscribe(url)
    if browser_result.get("completed"):
        # 'unconfirmed' methods clicked a button but couldn't verify success
        # text — still mark unsubscribed since the click went through, but
        # note it so the agent can surface the URL for manual verification.
        method = browser_result.get("method", "browser")
        is_confirmed = "unconfirmed" not in method
        db.set_marketing_sender_status(resolved_domain, "unsubscribed")
        out = {
            "ok": True,
            "sender_domain": resolved_domain,
            "method": method,
            "final_url": browser_result.get("final_url"),
        }
        if not is_confirmed:
            out["note"] = (
                "clicked a button but couldn't confirm success from the page text — "
                "share final_url with Namrita so she can verify."
            )
        return out

    # ---- Method 3: give up cleanly, hand the URL back --------------------
    db.set_marketing_sender_status(resolved_domain, "failed")
    return {
        "ok": False,
        "sender_domain": resolved_domain,
        "method": "needs-manual-click",
        "error": browser_result.get("error", "auto-unsubscribe failed"),
        "action_url": url,
        "hint": (
            "couldn't auto-unsubscribe. paste the action_url into the chat so "
            "Namrita can click it herself, OR offer to block_sender as a fallback."
        ),
    }


# ---------------------------------------------------------------------------
# Inbox rules (general-purpose) — auto-actions on incoming mail
# ---------------------------------------------------------------------------
#
# Outlook inbox rules let us auto-act on FUTURE incoming emails. The Graph
# API exposes them under /me/mailFolders/inbox/messageRules. Each rule has
# `conditions` (when does it apply) and `actions` (what to do).
#
# Documented condition keys we support here:
#   senderContains, fromAddresses, subjectContains, bodyContains,
#   recipientContains, hasAttachments, importance, sensitivity, isRead
#
# Documented action keys we support here:
#   moveToFolder (well-known name OR folder id), copyToFolder,
#   markAsRead, markImportance ('low'|'normal'|'high'),
#   delete (true = soft-delete to Deleted Items),
#   permanentDelete (BLOCKED — too dangerous),
#   forwardTo, redirectTo, assignCategories, stopProcessingRules
#
# Requires the MailboxSettings.ReadWrite OAuth scope. If that scope isn't
# granted (i.e. the user hasn't re-consented since we added it), the API
# will return 403.

# Well-known mail folder names that Graph accepts directly in moveToFolder/
# copyToFolder, so the agent doesn't need to look up IDs.
_WELL_KNOWN_FOLDERS = {
    "deleteditems",
    "junkemail",
    "archive",
    "inbox",
    "drafts",
    "sentitems",
    "outbox",
}


def _resolve_folder_id(token: str, folder: str) -> tuple[str | None, str | None]:
    """Resolve a folder reference (well-known name or display name) to a
    folder ID. Inbox rules require IDs, not well-known names. Returns
    (folder_id, error)."""
    folder_norm = folder.strip()
    folder_lc = folder_norm.lower().replace(" ", "")
    headers = {"Authorization": f"Bearer {token}"}

    if folder_lc in _WELL_KNOWN_FOLDERS:
        r = requests.get(
            f"{GRAPH}/me/mailFolders/{folder_lc}",
            headers=headers,
            timeout=15,
        )
        if r.status_code >= 400:
            return None, f"lookup well-known folder '{folder_lc}' failed ({r.status_code}): {r.text[:200]}"
        return r.json().get("id"), None

    # Otherwise, try to find a folder by displayName
    r = requests.get(
        f"{GRAPH}/me/mailFolders",
        headers=headers,
        params={"$filter": f"displayName eq '{folder_norm}'", "$top": "5"},
        timeout=15,
    )
    if r.status_code >= 400:
        return None, f"list folders failed ({r.status_code}): {r.text[:200]}"
    matches = r.json().get("value", [])
    if not matches:
        return None, f"no folder named '{folder_norm}' found"
    return matches[0].get("id"), None


def _normalize_rule_actions(token: str, actions: dict) -> tuple[dict, str | None]:
    """Take a user-friendly actions dict and convert any folder names into
    folder IDs. Block dangerous actions. Returns (normalized_actions, error)."""
    out = {}
    for k, v in actions.items():
        if k == "permanentDelete" and v:
            return {}, "permanentDelete is blocked. use delete (soft-delete to Deleted Items) instead."
        if k in ("moveToFolder", "copyToFolder"):
            if not isinstance(v, str):
                return {}, f"{k} must be a string (folder name or id)"
            # If it doesn't look like a Graph ID (long base64-ish), treat as
            # a name and resolve.
            if len(v) < 30 or "=" not in v:
                folder_id, err = _resolve_folder_id(token, v)
                if err:
                    return {}, f"{k}: {err}"
                out[k] = folder_id
            else:
                out[k] = v
        else:
            out[k] = v
    return out, None


def create_inbox_rule(
    name: str,
    conditions: dict,
    actions: dict,
    sequence: int = 1,
    is_enabled: bool = True,
):
    """Create an Outlook inbox rule that auto-acts on FUTURE incoming emails.

    Args:
      name: Display name for the rule (e.g. "Suri: archive Hyatt receipts").
      conditions: dict of Graph rule conditions. Keys: senderContains,
        fromAddresses, subjectContains, bodyContains, recipientContains,
        hasAttachments (bool), importance ('low'|'normal'|'high'), isRead.
        String-list values should be lists.
      actions: dict of Graph rule actions. Keys: moveToFolder (folder name
        or id — name is auto-resolved), copyToFolder, markAsRead (bool),
        markImportance ('low'|'normal'|'high'), delete (bool, soft-delete
        to Deleted Items), forwardTo, redirectTo, assignCategories,
        stopProcessingRules (bool).
      sequence: priority (lower = higher priority, default 1).
      is_enabled: whether to enable the rule immediately.

    Requires MailboxSettings.ReadWrite scope. Returns 403 with a clear hint
    if scope is missing."""
    token = _token()

    # Auto-stop after our actions to prevent surprise interactions with
    # other rules — unless the caller explicitly opted out.
    actions = dict(actions)
    actions.setdefault("stopProcessingRules", True)

    norm_actions, err = _normalize_rule_actions(token, actions)
    if err:
        return {"ok": False, "error": err}

    body = {
        "displayName": name,
        "sequence": sequence,
        "isEnabled": is_enabled,
        "conditions": conditions,
        "actions": norm_actions,
    }

    r = requests.post(
        f"{GRAPH}/me/mailFolders/inbox/messageRules",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    if r.status_code == 403:
        return {
            "ok": False,
            "error": (
                "Graph returned 403 (access denied). The Azure app likely "
                "doesn't have MailboxSettings.ReadWrite consented. Tell "
                "Namrita to (1) add MailboxSettings.ReadWrite delegated "
                "permission in Azure Portal → her Suri AI app → API "
                "Permissions, then (2) delete outlook_token.json and let "
                "the next call re-prompt for consent."
            ),
            "graph_status": 403,
            "graph_response": r.text[:300],
        }
    if r.status_code >= 400:
        return {
            "ok": False,
            "error": f"create rule failed ({r.status_code}): {r.text[:300]}",
        }

    j = r.json()
    return {
        "ok": True,
        "rule_id": j.get("id"),
        "rule_name": j.get("displayName"),
        "is_enabled": j.get("isEnabled"),
        "note": (
            "rule created. it will run on FUTURE incoming emails. "
            "doesn't apply to mail already in your inbox. reversible from "
            "Outlook → Settings → Rules, or via list_inbox_rules + "
            "delete_inbox_rule."
        ),
    }


def list_inbox_rules():
    """List all inbox rules currently configured on the user's mailbox.
    Returns each rule's id, name, sequence, isEnabled, conditions, actions."""
    token = _token()
    r = requests.get(
        f"{GRAPH}/me/mailFolders/inbox/messageRules",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if r.status_code == 403:
        return {
            "ok": False,
            "error": (
                "403 from Graph — MailboxSettings.ReadWrite scope not "
                "consented yet. Add it in Azure and delete outlook_token.json."
            ),
        }
    if r.status_code >= 400:
        return {"ok": False, "error": f"list rules failed ({r.status_code}): {r.text[:300]}"}
    rules = r.json().get("value", [])
    return {
        "ok": True,
        "count": len(rules),
        "rules": [
            {
                "id": rule.get("id"),
                "name": rule.get("displayName"),
                "sequence": rule.get("sequence"),
                "is_enabled": rule.get("isEnabled"),
                "conditions": rule.get("conditions"),
                "actions": rule.get("actions"),
            }
            for rule in rules
        ],
    }


def delete_inbox_rule(rule_id: str):
    """Delete an inbox rule by id. Get the id from list_inbox_rules first."""
    token = _token()
    r = requests.delete(
        f"{GRAPH}/me/mailFolders/inbox/messageRules/{rule_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if r.status_code in (200, 204):
        return {"ok": True, "rule_id": rule_id, "note": "rule deleted."}
    if r.status_code == 404:
        return {"ok": False, "rule_id": rule_id, "error": "no rule with that id"}
    return {
        "ok": False,
        "rule_id": rule_id,
        "error": f"delete failed ({r.status_code}): {r.text[:300]}",
    }


def block_sender(sender_domain: str):
    """Sugar wrapper around create_inbox_rule for the common 'just stop
    showing me this sender' case. Routes future emails from the domain
    straight to Deleted Items."""
    domain = sender_domain.lower().strip().lstrip("@")
    return create_inbox_rule(
        name=f"Suri: block {domain}",
        conditions={"senderContains": [domain]},
        actions={"delete": True, "stopProcessingRules": True},
    )


# ---------------------------------------------------------------------------
# Email triage
# ---------------------------------------------------------------------------

def triage_inbox(hours_back: int = 24, max_results: int = 30):
    """Fetch recent inbox messages so the agent can triage them in its response.

    Returns a list of items with: message_id, conversation_id, from_name,
    from_email, subject, snippet (~255 chars), received_at, is_unread,
    is_marketing (List-Unsubscribe header present)."""
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    params = {
        "$top": str(min(max_results, 100)),
        "$select": "id,conversationId,from,subject,bodyPreview,receivedDateTime,isRead,internetMessageHeaders",
        "$orderby": "receivedDateTime desc",
        "$filter": f"receivedDateTime ge {cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    }
    r = requests.get(
        f"{GRAPH}/me/mailFolders/inbox/messages",
        headers=headers,
        params=params,
        timeout=60,
    )
    r.raise_for_status()
    messages = r.json().get("value", [])

    out = []
    for msg in messages:
        from_field = msg.get("from", {}).get("emailAddress", {}) or {}
        url, _ = _extract_unsubscribe(msg.get("internetMessageHeaders"))
        out.append(
            {
                "message_id": msg.get("id"),
                "conversation_id": msg.get("conversationId"),
                "from_name": from_field.get("name"),
                "from_email": from_field.get("address"),
                "subject": msg.get("subject"),
                "snippet": (msg.get("bodyPreview") or "")[:255],
                "received_at": msg.get("receivedDateTime"),
                "is_unread": not msg.get("isRead", True),
                "is_marketing": url is not None,
            }
        )
    return out


def get_thread(message_id: str, max_messages: int = 10):
    """Fetch all messages in the conversation containing message_id, so the
    agent can summarize the thread. Returns ordered oldest -> newest, each with
    from, sent_at, body (capped at 2000 chars per message)."""
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}
    msg_resp = requests.get(
        f"{GRAPH}/me/messages/{message_id}",
        headers=headers,
        params={"$select": "conversationId"},
        timeout=30,
    )
    msg_resp.raise_for_status()
    conv_id = msg_resp.json().get("conversationId")
    if not conv_id:
        return {"ok": False, "error": "could not resolve conversation id"}

    params = {
        "$top": str(max_messages),
        "$select": "id,from,toRecipients,subject,body,sentDateTime",
        "$orderby": "sentDateTime asc",
        "$filter": f"conversationId eq '{conv_id}'",
    }
    r = requests.get(
        f"{GRAPH}/me/messages",
        headers=headers,
        params=params,
        timeout=60,
    )
    r.raise_for_status()
    messages = r.json().get("value", [])
    out = []
    for m in messages:
        from_addr = (m.get("from") or {}).get("emailAddress", {}) or {}
        body = (m.get("body") or {}).get("content") or ""
        # bodies are HTML by default; strip tags crudely for the agent's sake
        body_text = re.sub(r"<[^>]+>", " ", body)
        body_text = re.sub(r"\s+", " ", body_text).strip()
        out.append(
            {
                "message_id": m.get("id"),
                "from_name": from_addr.get("name"),
                "from_email": from_addr.get("address"),
                "subject": m.get("subject"),
                "sent_at": m.get("sentDateTime"),
                "body": body_text[:2000],
            }
        )
    return {"ok": True, "messages": out}


_GRAPH_ALLOWED_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}
_GRAPH_ALLOWED_PREFIXES = ("/me/", "/me", "/search/", "/search", "/users/me/")
_GRAPH_MAX_RESPONSE_BYTES = 50_000
_GRAPH_HARD_DELETE_MESSAGE_RE = re.compile(r"^/me/messages/[^/]+/?$")


def outlook_graph(
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
):
    """Generic escape hatch: make any Microsoft Graph API call as Namrita,
    bounded by the OAuth scopes she's granted (currently Mail.ReadWrite,
    so you're limited to email-related endpoints — calendar/contacts/files
    would need her to add those scopes in Azure first).

    Use this for the long tail of Outlook actions that don't have a narrow
    tool yet. Prefer narrow tools (delete_email, draft_reply, etc.) when
    they exist — they have business logic and safety guards.

    Args:
      method: GET | POST | PATCH | PUT | DELETE
      path: must start with /me/, /search/, or /users/me/.
            E.g. "/me/messages?$top=5" or "/me/mailFolders".
      params: query string params (used if not embedded in path)
      body: JSON body for POST/PATCH/PUT

    Returns: {ok, status_code, body, truncated?}. Body is parsed JSON when
    possible, otherwise raw text. Truncated at 50KB.
    """
    method = method.upper()
    if method not in _GRAPH_ALLOWED_METHODS:
        return {
            "ok": False,
            "error": f"method {method} not allowed. use one of {sorted(_GRAPH_ALLOWED_METHODS)}.",
        }

    if not path.startswith("/"):
        path = "/" + path

    # split path/query if caller embedded query string
    if "?" in path:
        path_only, query_str = path.split("?", 1)
    else:
        path_only, query_str = path, None

    if not any(path_only.startswith(p) for p in _GRAPH_ALLOWED_PREFIXES):
        return {
            "ok": False,
            "error": (
                f"path must start with one of {_GRAPH_ALLOWED_PREFIXES}. "
                "arbitrary tenant access is blocked."
            ),
        }

    # Block hard delete of messages — force through narrow soft-delete tool.
    if method == "DELETE" and _GRAPH_HARD_DELETE_MESSAGE_RE.match(path_only):
        return {
            "ok": False,
            "error": (
                "hard-delete of email messages is blocked. use the delete_email "
                "tool instead — it soft-deletes (move to Deleted Items, "
                "recoverable for ~30 days)."
            ),
        }

    url = f"{GRAPH}{path_only}"
    if query_str:
        url = f"{url}?{query_str}"

    print(
        f"[graph] {method} {path_only} params={params} body_keys="
        f"{list(body.keys()) if isinstance(body, dict) else None}",
        file=sys.stderr,
        flush=True,
    )

    token = _token()
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    try:
        r = requests.request(
            method, url, headers=headers, params=params, json=body, timeout=60
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    try:
        parsed = r.json()
    except Exception:
        parsed = r.text

    body_repr = parsed if isinstance(parsed, str) else json.dumps(parsed)
    truncated = False
    if len(body_repr) > _GRAPH_MAX_RESPONSE_BYTES:
        body_repr = body_repr[:_GRAPH_MAX_RESPONSE_BYTES] + "...[truncated]"
        truncated = True
        parsed_for_return = body_repr
    else:
        parsed_for_return = parsed

    print(
        f"[graph] -> {r.status_code}, {len(body_repr)} bytes"
        f"{', truncated' if truncated else ''}",
        file=sys.stderr,
        flush=True,
    )

    return {
        "ok": 200 <= r.status_code < 400,
        "status_code": r.status_code,
        "body": parsed_for_return,
        "truncated": truncated,
    }


def delete_email(message_id: str):
    """Move a single email to the user's Deleted Items folder. This is a SOFT
    delete — recoverable from Deleted Items in Outlook for ~30 days. Never
    hard-deletes."""
    token = _token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(
        f"{GRAPH}/me/messages/{message_id}/move",
        headers=headers,
        json={"destinationId": "deleteditems"},
        timeout=30,
    )
    if r.status_code >= 400:
        return {
            "ok": False,
            "message_id": message_id,
            "error": f"delete failed ({r.status_code}): {r.text[:300]}",
        }
    return {
        "ok": True,
        "message_id": message_id,
        "moved_to": "Deleted Items",
        "note": "soft-deleted — recoverable from Outlook's Deleted Items folder for ~30 days.",
    }


def draft_reply(message_id: str, body: str):
    """Create a draft reply in the user's Outlook Drafts folder. Does NOT send.
    She'll find it in Outlook (web/desktop/mobile) under Drafts and can review,
    edit, and send from there. Returns the draft id and a web link."""
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}

    # createReply returns a draft Message with the original quoted below
    create = requests.post(
        f"{GRAPH}/me/messages/{message_id}/createReply",
        headers=headers,
        timeout=30,
    )
    if create.status_code >= 400:
        return {
            "ok": False,
            "error": f"createReply failed ({create.status_code}): {create.text[:300]}",
        }
    draft = create.json()
    draft_id = draft.get("id")
    if not draft_id:
        return {"ok": False, "error": "no draft id returned from Graph"}

    # Patch the body of the draft. Use HTML so Outlook renders it nicely.
    # The original quoted thread is already in the draft body; prepend our text.
    existing_body = (draft.get("body") or {}).get("content") or ""
    prepended = f"<div>{body}</div><br><br>{existing_body}"
    patch = requests.patch(
        f"{GRAPH}/me/messages/{draft_id}",
        headers={**headers, "Content-Type": "application/json"},
        json={"body": {"contentType": "html", "content": prepended}},
        timeout=30,
    )
    if patch.status_code >= 400:
        return {
            "ok": False,
            "error": f"patch draft body failed ({patch.status_code}): {patch.text[:300]}",
        }

    # Persist for audit / approval flow if we want to gate sends later
    db.create_pending_action(
        action_id=f"draft-{draft_id}",
        action_type="email_draft",
        payload={
            "draft_id": draft_id,
            "in_reply_to": message_id,
            "body_preview": body[:200],
        },
    )
    return {
        "ok": True,
        "draft_id": draft_id,
        "web_link": draft.get("webLink"),
        "note": "draft saved in your Outlook Drafts folder — open Outlook to review and send.",
    }


# ---------------------------------------------------------------------------
# Paid subscription detection (read-only)
# ---------------------------------------------------------------------------

# Subject patterns that strongly suggest a recurring charge / receipt
_SUB_SUBJECT_RE = re.compile(
    r"\b(receipt|renewal|renewed|invoice|subscription|"
    r"charged|payment\s+(received|confirmation)|"
    r"order\s+confirmation|your\s+\w+\s+(subscription|plan|membership))\b",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"(?:USD|US\$|\$|€|£)\s?(\d+(?:[.,]\d{2})?)")
_CADENCE_RE = re.compile(r"\b(monthly|annual|yearly|year|month|weekly|week)\b", re.IGNORECASE)


def _normalize_cadence(s: str | None) -> str | None:
    if not s:
        return None
    s = s.lower()
    if s in ("yearly", "annual", "year"):
        return "yearly"
    if s in ("monthly", "month"):
        return "monthly"
    if s in ("weekly", "week"):
        return "weekly"
    return s


def _service_name_from(from_name: str | None, from_email: str) -> str:
    if from_name:
        # strip stuff like "<no-reply@...>" appended in some clients
        name = re.sub(r"\s*<[^>]+>\s*", "", from_name).strip()
        # drop leading "The " and trailing " Team"
        name = re.sub(r"\s+team$", "", name, flags=re.IGNORECASE).strip()
        if name:
            return name
    # fall back to domain's second-level label
    domain = from_email.split("@")[-1].lower()
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return domain


def find_paid_subscriptions(days_back: int = 90, max_results: int = 500):
    """Scan recent inbox for receipts/renewals to estimate what Namrita is
    paying for. Detection only — never cancels."""
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    params = {
        "$top": str(min(max_results, 999)),
        "$select": "id,from,subject,bodyPreview,receivedDateTime",
        "$orderby": "receivedDateTime desc",
        "$filter": f"receivedDateTime ge {cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    }
    r = requests.get(
        f"{GRAPH}/me/mailFolders/inbox/messages",
        headers=headers,
        params=params,
        timeout=90,
    )
    r.raise_for_status()
    messages = r.json().get("value", [])

    by_service: dict[str, dict] = {}
    for msg in messages:
        subject = msg.get("subject") or ""
        if not _SUB_SUBJECT_RE.search(subject):
            continue
        from_field = (msg.get("from") or {}).get("emailAddress", {}) or {}
        from_email = (from_field.get("address") or "").lower()
        if not from_email:
            continue
        from_name = from_field.get("name")
        service = _service_name_from(from_name, from_email)

        haystack = f"{subject}\n{msg.get('bodyPreview') or ''}"
        amount_match = _AMOUNT_RE.search(haystack)
        amount = amount_match.group(1).replace(",", ".") if amount_match else None
        currency = None
        if amount_match:
            sym = amount_match.group(0)[0]
            currency = {"$": "USD", "€": "EUR", "£": "GBP"}.get(sym, "USD")
        cadence_match = _CADENCE_RE.search(haystack)
        cadence = _normalize_cadence(cadence_match.group(1) if cadence_match else None)

        try:
            received = datetime.fromisoformat(
                msg["receivedDateTime"].replace("Z", "+00:00")
            )
        except Exception:
            continue

        existing = by_service.get(service)
        if existing is None or received > existing["received"]:
            by_service[service] = {
                "received": received,
                "sender_domain": from_email.split("@")[-1],
                "amount": amount,
                "currency": currency,
                "cadence": cadence,
                "subject": subject,
            }

    results = []
    for service, info in by_service.items():
        db.upsert_paid_subscription(
            service_name=service,
            sender_domain=info["sender_domain"],
            last_charge_amount=info["amount"],
            last_charge_currency=info["currency"],
            last_charge_date=info["received"].date().isoformat(),
            cadence=info["cadence"],
            sample_subject=info["subject"],
            last_seen=info["received"].isoformat(),
        )
        results.append(
            {
                "service_name": service,
                "sender_domain": info["sender_domain"],
                "last_charge_amount": info["amount"],
                "last_charge_currency": info["currency"],
                "last_charge_date": info["received"].date().isoformat(),
                "cadence": info["cadence"],
                "sample_subject": info["subject"],
            }
        )
    # Sort: known monthly cost first, then most recent
    def sort_key(r):
        try:
            amt = float(r["last_charge_amount"]) if r["last_charge_amount"] else 0
        except (ValueError, TypeError):
            amt = 0
        return (-amt, r["last_charge_date"])

    results.sort(key=sort_key)
    return results
