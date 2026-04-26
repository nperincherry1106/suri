import json
import os
import sys
import uuid
from datetime import datetime

from anthropic import Anthropic

from app import db
from app.tools import audit, memory, outlook, reminders

MODEL = "claude-sonnet-4-5"

PERSONA = """You are Suri, Namrita's personal assistant. You handle the small life-logistics
tasks that fall through the cracks because she's overwhelmed at work.

Tone:
- Warm but efficient. Like a competent friend, not a butler.
- Lowercase is fine. Casual contractions are fine.
- Brevity is respect. Don't pad. No more than 2 messages in a row without reason.
- No emoji unless she uses them first.
- Never apologize unless you actually messed up.

Formatting (you talk to her in Telegram, which renders plain text only):
- NO markdown. No **bold**, no *italics*, no `code`, no # headers, no [link](url).
  Telegram shows the raw asterisks, hashes, and brackets — it looks broken.
- For lists, use a hyphen or a number, never a markdown bullet.
- For emphasis, just write the words. Capitalization is fine for one or two words.
- For URLs, paste the bare URL. Telegram auto-links it.

Behavior:
- Default to action, not asking. If you can take a reasonable next step, take it.
- For high-stakes or destructive actions (sending messages as her, spending
  money, batch deletes, canceling subscriptions), pause for explicit approval
  before executing.
- If a step takes >10 seconds, send a short "on it" first.
- If she tells you a preference worth keeping ("I hate phone calls", "never
  unsubscribe me from USPS", "my partner is Alex"), call remember_fact so you
  have it next week. The "Known about the user" block below is your live
  memory — read from it every turn.
- If you don't have a tool for what she's asking, FIRST check whether
  outlook_graph could compose the call (it can do almost any Outlook action
  within her current scopes). Only say "I can't do that yet" once you've
  considered the escape hatch.

Honesty (HARD RULES, no exceptions):
1. If you SAY an action happened, you MUST have called the matching tool in
   THIS SAME TURN and seen ok:true in the result. If you didn't call the tool,
   the action didn't happen — say so plainly. Don't dress up "I will" as "I did".
2. Never write "I'll remind you" / "I'll handle that" / "I'll unsubscribe you"
   as a substitute for calling the tool now. Either call it now, or ask "want
   me to do that now?" and wait.
3. The "Action ground truth" block in your system prompt is the authoritative
   record of what has actually happened (from the database). If your
   conversation memory disagrees with it, your memory is wrong — defer to
   ground truth and tell her plainly. For the FULL list of what you did
   recently (not just the per-tool counts in ground truth), call
   what_did_you_do.
4. When summarizing a batch (multiple unsubscribes, multiple deletes),
   enumerate by ground truth, not by recollection: "X succeeded, Y failed
   because [reason]". Never "all done" if anything returned ok:false.
5. The tool descriptions in your tool list tell you HOW to use each tool
   (workflow, ordering, gates). Follow them."""

TOOLS = [
    {
        "name": "outlook_graph",
        "description": (
            "ESCAPE HATCH for Outlook. Make any Microsoft Graph API call as "
            "Namrita, scoped to whatever permissions her Azure app currently "
            "has (today: Mail.ReadWrite — so email actions only; calendar/"
            "contacts/files would need her to add scopes). Use this BEFORE "
            "saying 'I can't do that' for anything email-related.\n\n"
            "Prefer narrow tools when they exist (delete_email, draft_reply, "
            "unsubscribe_from, etc.) because they carry safety guards. Use "
            "outlook_graph for the long tail: moving to specific folders, "
            "marking read/unread, flagging, custom searches, listing folders, "
            "categorizing, etc.\n\n"
            "Examples:\n"
            "  GET  /me/messages?$filter=isRead eq false&$top=10\n"
            "  GET  /me/mailFolders\n"
            "  POST /me/messages/{id}/move  body={destinationId: 'archive'}\n"
            "  PATCH /me/messages/{id}     body={isRead: true, flag: {flagStatus: 'flagged'}}\n"
            "  POST /me/messages/{id}/createReply\n\n"
            "Hard-delete of /me/messages/{id} is BLOCKED — use delete_email "
            "(soft-delete, recoverable). For any write (POST/PATCH/PUT/DELETE) "
            "get explicit user approval first unless it's the obvious "
            "continuation of an action she just approved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"]},
                "path": {
                    "type": "string",
                    "description": "Graph path starting with /me/, /search/, or /users/me/. Query string can be embedded.",
                },
                "params": {
                    "type": "object",
                    "description": "Optional query string params (alternative to embedding in path).",
                },
                "body": {
                    "type": "object",
                    "description": "Optional JSON body for POST/PATCH/PUT.",
                },
            },
            "required": ["method", "path"],
        },
    },
    {
        "name": "triage_inbox",
        "description": (
            "Fetch recent inbox messages from Outlook. Returns up to 30 items "
            "with sender, subject, snippet, is_unread, and an is_marketing "
            "hint (List-Unsubscribe header present).\n\n"
            "WORKFLOW: Call this when she asks 'what's in my inbox' / 'what's "
            "important' / 'anything I missed'. Then in your reply, GROUP the "
            "results yourself into: urgent (needs reply soon) / waiting on "
            "reply from her / FYI (account, receipts) / noise (marketing). "
            "Surface the top ~10 items, offer to show more. Use is_marketing "
            "+ sender + subject to bucket."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours_back": {
                    "type": "integer",
                    "description": "How many hours back to look. Default 24.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Cap on items returned. Default 30, max 100.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_thread",
        "description": (
            "Fetch the full conversation containing a message_id so you can "
            "summarize it. Returns up to 10 messages oldest-to-newest with "
            "bodies (capped at 2000 chars each). Use for 'what is this email "
            "about' / 'summarize this thread'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "max_messages": {"type": "integer"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "delete_email",
        "description": (
            "Soft-delete one email — moves it to Namrita's Outlook Deleted "
            "Items folder, recoverable from there for ~30 days. Never "
            "hard-deletes.\n\n"
            "WORKFLOW: For a single email she's pointed at, just do it. For "
            "BATCH deletes, list the specific subjects/senders first and get "
            "explicit confirmation ('yes delete those') before calling. Then "
            "call this in parallel for each message_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The message_id from triage_inbox.",
                }
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "draft_reply",
        "description": (
            "Save a draft reply to a specific email in Namrita's Outlook "
            "Drafts folder. She opens Outlook to review and click Send "
            "herself — you do NOT auto-send.\n\n"
            "WORKFLOW (mandatory):\n"
            "1. Show her the draft text in chat.\n"
            "2. Wait for explicit approval ('send it' / 'go ahead' / "
            "'looks good').\n"
            "3. Then call this tool.\n"
            "4. After it returns ok:true, tell her: 'draft saved in your "
            "Outlook Drafts — open Outlook to send'.\n\n"
            "If she says 'send it' but you haven't drafted anything yet, "
            "ask for the body first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The message_id from triage_inbox or get_thread you're replying to.",
                },
                "body": {
                    "type": "string",
                    "description": "The reply body, plain text or simple HTML. The original quoted thread will be appended automatically.",
                },
            },
            "required": ["message_id", "body"],
        },
    },
    {
        "name": "find_paid_subscriptions",
        "description": (
            "Scan Namrita's inbox over the last N days for receipts, "
            "renewals, and invoices to estimate what she's currently paying "
            "for. DETECTION ONLY — does NOT cancel anything. If she asks to "
            "cancel a paid sub, say plainly that you can't do that yet. "
            "Takes ~10-30 seconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_back": {
                    "type": "integer",
                    "description": "Lookback window in days. Default 90.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "find_marketing_senders",
        "description": (
            "Scan Namrita's Outlook inbox for senders with a List-Unsubscribe "
            "header (i.e. real marketing/newsletter senders). Returns up to "
            "20 senders ranked by email volume, with sample subjects and "
            "whether RFC 8058 one-click unsubscribe is supported. Takes "
            "~10-30 seconds.\n\n"
            "WORKFLOW: Call this first, show her the list, get her pick of "
            "which to unsubscribe from, THEN call unsubscribe_from for each. "
            "Don't unsubscribe from transactional senders (receipts, account "
            "alerts, anything from a real human) unless she explicitly says to."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "unsubscribe_from",
        "description": (
            "Try to unsubscribe from a marketing sender by their domain. "
            "Three methods in order:\n"
            "  1. RFC 8058 one-click POST (instant; server returns 200 = "
            "request accepted, NOT verified honored)\n"
            "  2. Headless browser: opens the unsubscribe page, clicks the "
            "confirm button (works for most opt-out pages, success "
            "sometimes confirmable from page text)\n"
            "  3. Returns the URL so you can hand it to Namrita to click\n\n"
            "Result includes:\n"
            "  ok: whether the request was successfully sent\n"
            "  method: which path was used (rfc8058-one-click-post, "
            "browser-..., or needs-manual-click)\n"
            "  verification: 'server-ack-only' | 'page-confirmed' | "
            "'unconfirmed' | absent (true verification only available "
            "for browser confirms)\n"
            "  action_url: present if she needs to click it herself\n\n"
            "HONESTY about reporting:\n"
            "- For method 'rfc8058-one-click-post' or browser '-unconfirmed': "
            "say 'request sent — should stop within 1-7 days, but I can't "
            "verify until then. If they keep emailing in 2 weeks I can "
            "block_sender as a fallback.' Don't say 'unsubscribed' — say "
            "'request sent' / 'asked them to stop'.\n"
            "- For browser '-confirmed': say 'confirmed unsubscribed (the "
            "page said so)'.\n"
            "- For ok:false with action_url: paste the URL so she can click.\n"
            "- For batches: enumerate by ground truth, never 'all done' if "
            "any returned ok:false.\n\n"
            "Prefer exact sender_domain from the most recent "
            "find_marketing_senders result. Calling in parallel for multiple "
            "senders in one turn is fine."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sender_domain": {
                    "type": "string",
                    "description": "The sender domain to unsubscribe from, e.g. 'hyatt.com'.",
                }
            },
            "required": ["sender_domain"],
        },
    },
    {
        "name": "block_sender",
        "description": (
            "Convenience: create an Outlook inbox rule that auto-deletes all "
            "FUTURE emails from a sender domain (soft-delete to Deleted Items). "
            "Use when unsubscribe_from won't work (politicians, unsubscribe-"
            "resistant marketers) or when she just wants someone gone. Doesn't "
            "stop them sending — just hides their emails. Reversible.\n\n"
            "Get explicit approval first. Don't block transactional senders or "
            "anyone she might actually want to hear from. For more nuanced "
            "rules (auto-archive, auto-flag, auto-mark-read, route by subject), "
            "use create_inbox_rule instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sender_domain": {
                    "type": "string",
                    "description": "Domain to block, e.g. 'aaronparnas.com' or 'mail.goodreads.com'.",
                }
            },
            "required": ["sender_domain"],
        },
    },
    {
        "name": "create_inbox_rule",
        "description": (
            "Create an Outlook inbox rule that auto-acts on FUTURE incoming "
            "emails. Powerful: you can move/copy to a folder, mark read, "
            "flag/categorize by importance, soft-delete, forward, or any "
            "combination.\n\n"
            "CONDITIONS dict (when to apply — combine for AND):\n"
            "  senderContains: ['domain.com', 'name@x.com']\n"
            "  fromAddresses: [{'emailAddress': {'address': 'x@y.com'}}]\n"
            "  subjectContains: ['receipt', 'invoice']\n"
            "  bodyContains: ['unsubscribe']\n"
            "  hasAttachments: true\n"
            "  importance: 'high'\n\n"
            "ACTIONS dict (what to do — combine for multiple):\n"
            "  moveToFolder: 'Archive' | 'Deleted Items' | 'Junk Email' | <custom folder name or id>\n"
            "  copyToFolder: <same as above>\n"
            "  markAsRead: true\n"
            "  markImportance: 'low' | 'normal' | 'high'\n"
            "  delete: true   (soft-delete to Deleted Items, recoverable)\n"
            "  assignCategories: ['Newsletter', 'Receipts']\n"
            "  forwardTo: [{'emailAddress': {'address': 'x@y.com'}}]\n\n"
            "permanentDelete is BLOCKED. stopProcessingRules defaults to true.\n\n"
            "WORKFLOW: Get explicit approval before creating the rule. Show "
            "Namrita a plain-English summary of what the rule will do, then "
            "call this. After it returns ok:true, tell her she can see/edit/"
            "delete it in Outlook → Settings → Rules, or you can do it via "
            "list_inbox_rules + delete_inbox_rule."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Display name for the rule. Prefix with 'Suri:' so it's clear who made it. E.g. 'Suri: archive Hyatt receipts'.",
                },
                "conditions": {
                    "type": "object",
                    "description": "Conditions dict — see tool description.",
                },
                "actions": {
                    "type": "object",
                    "description": "Actions dict — see tool description.",
                },
                "sequence": {
                    "type": "integer",
                    "description": "Priority (lower = higher priority). Default 1.",
                },
                "is_enabled": {
                    "type": "boolean",
                    "description": "Default true.",
                },
            },
            "required": ["name", "conditions", "actions"],
        },
    },
    {
        "name": "list_inbox_rules",
        "description": (
            "List every inbox rule on Namrita's mailbox — both ones Suri "
            "created and ones she set up herself. Returns id, name, "
            "sequence, isEnabled, conditions, actions. Useful before "
            "creating overlapping rules or to audit what's auto-acting "
            "on her inbox."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "delete_inbox_rule",
        "description": (
            "Delete an inbox rule by its id (get from list_inbox_rules). "
            "Get explicit approval before deleting a rule she didn't ask "
            "you to remove — she may have set it up intentionally."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_id": {
                    "type": "string",
                    "description": "Rule id from list_inbox_rules.",
                }
            },
            "required": ["rule_id"],
        },
    },
    {
        "name": "remember_fact",
        "description": (
            "Save a long-term fact about Namrita to persistent memory. Use "
            "for preferences ('hates phone calls'), schedule ('works 9-7 "
            "PT'), recurring context ('partner is named Alex'), or anti-rules "
            "she's stated ('never unsubscribe me from USPS', 'always keep "
            "Hyatt emails').\n\n"
            "DON'T use for trivia, one-off info, or anything she'd find "
            "creepy to have permanently stored. The 'Known about the user' "
            "block in your system prompt is your live memory — read from "
            "there, write via this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Short snake_case key, e.g. 'work_hours', 'partner_name', 'hates_phone_calls', 'never_unsub_usps'.",
                },
                "value": {
                    "type": "string",
                    "description": "The fact itself in natural language.",
                },
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "forget_fact",
        "description": "Delete a previously-remembered fact by its key.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "what_did_you_do",
        "description": (
            "Look up the actual tool calls Suri made in a recent window. "
            "Use when Namrita asks 'what did you do today?' / 'what happened "
            "while I was away?' / 'did you actually unsubscribe X?'.\n\n"
            "This reads from the audit log (a database table updated every "
            "time a tool is called), so it's authoritative — prefer it over "
            "scrolling chat history.\n\n"
            "After calling, summarize for her in plain English. Group by "
            "tool when many calls happened. Call out failures (ok:false) "
            "explicitly — don't bury them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": (
                        "Window: 'today' | '24h' | 'yesterday' (last 48h) | "
                        "'7d' | 'week' | '<N>h' (e.g. '6h'). Default '24h'."
                    ),
                }
            },
            "required": [],
        },
    },
    {
        "name": "set_reminder",
        "description": (
            "Schedule a reminder pushed to Namrita's Telegram at the given "
            "time. Convert her natural-language time ('tomorrow 9am', 'in "
            "3 hours', 'monday at 5pm') to ISO 8601 with timezone offset, "
            "using the 'Current time' line in your system prompt as anchor.\n\n"
            "MANDATORY: when she asks for a reminder, call this in the SAME "
            "turn. Don't say 'got it, I'll remind you' without calling — "
            "the reminder doesn't exist until this returns ok:true. After "
            "calling, quote the exact fire_at back to her. Verify via the "
            "'Active reminders' block in your system prompt, not memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "when_iso": {
                    "type": "string",
                    "description": "ISO 8601 timestamp with tz offset, e.g. '2026-04-26T09:00:00-07:00'.",
                },
                "body": {
                    "type": "string",
                    "description": "What to remind her about. Short and clear.",
                },
            },
            "required": ["when_iso", "body"],
        },
    },
    {
        "name": "list_reminders",
        "description": "List all pending (not yet fired, not cancelled) reminders.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a pending reminder by its id (from list_reminders).",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    },
]

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _ground_truth_block() -> str:
    """The authoritative state of persistent actions, injected into the system
    prompt every turn so the agent can't lie about what it has done."""
    parts = []

    unsubbed = db.list_unsubscribed_senders()
    failed = db.list_failed_unsubscribes()
    if unsubbed:
        lines = "\n".join(
            f"  - {s['service_name'] or s['sender_domain']} ({s['sender_domain']})"
            for s in unsubbed
        )
        parts.append(f"Successfully unsubscribed from (verified):\n{lines}")
    else:
        parts.append("Successfully unsubscribed from (verified): NONE.")

    if failed:
        lines = "\n".join(
            f"  - {s['service_name'] or s['sender_domain']} ({s['sender_domain']})"
            for s in failed
        )
        parts.append(f"Recent unsubscribe FAILURES (still receiving):\n{lines}")

    pending = db.list_pending_reminders()
    if pending:
        lines = "\n".join(
            f"  - #{r['id']} at {r['fire_at']}: {r['body']}" for r in pending
        )
        parts.append(f"Active scheduled reminders:\n{lines}")
    else:
        parts.append("Active scheduled reminders: NONE.")

    # Recent tool calls — collapses to a per-tool count + failures so the
    # block stays short. For the full payload the agent should call
    # what_did_you_do.
    recent = db.recent_agent_actions(hours_back=24, limit=200)
    if recent:
        counts: dict[str, int] = {}
        failures: list[str] = []
        for a in recent:
            counts[a["tool_name"]] = counts.get(a["tool_name"], 0) + 1
            if not a["ok"]:
                err = a["result"]
                if isinstance(err, dict):
                    err = err.get("error") or err.get("note") or "ok:false"
                failures.append(f"  - {a['tool_name']}: {str(err)[:120]}")
        summary = ", ".join(f"{k}×{v}" for k, v in sorted(counts.items()))
        section = f"Recent tool calls (last 24h):\n  {summary}"
        if failures:
            section += "\nRecent FAILURES (be honest about these):\n" + "\n".join(failures[:10])
        parts.append(section)

    return "Action ground truth (from database — this is REALITY):\n\n" + "\n\n".join(parts)


def _system_prompt() -> str:
    now = datetime.now().astimezone()
    time_block = f"Current time: {now.isoformat(timespec='seconds')} ({now.tzname()})"
    truth_block = _ground_truth_block()
    facts = db.user_facts()
    sections = [PERSONA, time_block, truth_block]
    if facts:
        facts_block = "Known about the user:\n" + "\n".join(
            f"- {k}: {v}" for k, v in facts.items()
        )
        sections.append(facts_block)
    return "\n\n".join(sections)


def _history():
    msgs = []
    for d, b in db.recent_messages(20):
        role = "user" if d == "inbound" else "assistant"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + b
        else:
            msgs.append({"role": role, "content": b})
    # Anthropic requires the first message to be role=user. A pushed reminder
    # logged via db.log_outbound can otherwise end up as the first item in
    # the rolling 20-message window — drop leading assistant entries.
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def _execute_tool(name: str, input_: dict, turn_id: str):
    print(f"[tool] {name}({input_})", file=sys.stderr, flush=True)
    if name == "outlook_graph":
        result = outlook.outlook_graph(
            method=input_["method"],
            path=input_["path"],
            params=input_.get("params"),
            body=input_.get("body"),
        )
    elif name == "triage_inbox":
        result = outlook.triage_inbox(
            hours_back=input_.get("hours_back", 24),
            max_results=input_.get("max_results", 30),
        )
    elif name == "get_thread":
        result = outlook.get_thread(
            input_["message_id"], max_messages=input_.get("max_messages", 10)
        )
    elif name == "delete_email":
        result = outlook.delete_email(input_["message_id"])
    elif name == "draft_reply":
        result = outlook.draft_reply(input_["message_id"], input_["body"])
    elif name == "find_paid_subscriptions":
        result = outlook.find_paid_subscriptions(days_back=input_.get("days_back", 90))
    elif name == "find_marketing_senders":
        result = outlook.find_marketing_senders()
    elif name == "unsubscribe_from":
        result = outlook.unsubscribe_from(input_["sender_domain"])
    elif name == "block_sender":
        result = outlook.block_sender(input_["sender_domain"])
    elif name == "create_inbox_rule":
        result = outlook.create_inbox_rule(
            name=input_["name"],
            conditions=input_["conditions"],
            actions=input_["actions"],
            sequence=input_.get("sequence", 1),
            is_enabled=input_.get("is_enabled", True),
        )
    elif name == "list_inbox_rules":
        result = outlook.list_inbox_rules()
    elif name == "delete_inbox_rule":
        result = outlook.delete_inbox_rule(input_["rule_id"])
    elif name == "remember_fact":
        result = memory.remember_fact(input_["key"], input_["value"])
    elif name == "forget_fact":
        result = memory.forget_fact(input_["key"])
    elif name == "set_reminder":
        result = reminders.set_reminder(input_["when_iso"], input_["body"])
    elif name == "list_reminders":
        result = reminders.list_reminders()
    elif name == "cancel_reminder":
        result = reminders.cancel_reminder(input_["id"])
    elif name == "what_did_you_do":
        result = audit.what_did_you_do(since=input_.get("since", "24h"))
    else:
        result = {"ok": False, "error": f"unknown tool: {name}"}
    # Truncate large results in the log
    if isinstance(result, list):
        log_view = f"<list of {len(result)}>"
    else:
        s = str(result)
        log_view = s if len(s) <= 300 else s[:300] + "...[truncated]"
    print(f"[tool] -> {log_view}", file=sys.stderr, flush=True)
    # Persist for ground-truth + what_did_you_do. ok:false dicts are still
    # logged — failures are part of the audit trail. Skip self-logging
    # what_did_you_do calls so the tool's own queries don't pollute results.
    if name != "what_did_you_do":
        ok = True
        if isinstance(result, dict) and result.get("ok") is False:
            ok = False
        try:
            db.log_agent_action(turn_id, name, input_, result, ok)
        except Exception as e:
            print(f"[audit] log failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    return result


def _serialize(blocks):
    out = []
    for b in blocks:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append(
                {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
            )
    return out


def handle(send=None) -> list[str]:
    """Run the agent loop. Calls `send(text)` for each agent message as it
    happens (so callers can stream "on it" before tool calls finish). Also
    returns the full list of messages sent."""
    messages = _history()
    sent: list[str] = []
    # One turn_id per user message → spans every tool call in this loop.
    # Lets the audit log group "what happened in response to that ask".
    turn_id = uuid.uuid4().hex[:12]
    while True:
        resp = _get_client().messages.create(
            model=MODEL,
            max_tokens=2048,
            system=_system_prompt(),
            messages=messages,
            tools=TOOLS,
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if text:
            sent.append(text)
            if send:
                send(text)

        if resp.stop_reason != "tool_use":
            return sent

        messages.append({"role": "assistant", "content": _serialize(resp.content)})
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            try:
                result = _execute_tool(block.name, block.input, turn_id)
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
                try:
                    db.log_agent_action(turn_id, block.name, block.input, result, False)
                except Exception as log_e:
                    print(f"[audit] log failed: {type(log_e).__name__}: {log_e}", file=sys.stderr, flush=True)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                }
            )
        messages.append({"role": "user", "content": tool_results})
