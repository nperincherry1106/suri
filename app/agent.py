import json
import os
import sys
import uuid
from datetime import datetime

from anthropic import Anthropic, APIStatusError

from app import accounts, db
from app.tools import audit, memory, outlook, plaid as plaid_tool, reminders
from app.tools.outlook import OutlookAuthRequired

MODEL = "claude-sonnet-4-5"
COMPACT_MODEL = "claude-haiku-4-5-20251001"

# When more than COMPACT_THRESHOLD messages have arrived since the last
# summary, fold the oldest half into a new summary. Keeps live history
# bounded while preserving long-running context.
COMPACT_THRESHOLD = 30
COMPACT_KEEP_LIVE = 15

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
  before executing. To make approval one tap instead of typed: end your
  message with a line like
      [CONFIRM]: <one-line description of what Yes will do>
  on its own. The Telegram transport will detect the marker, strip it from
  the visible message, and render Yes/No buttons. If she taps Yes you'll
  receive a synthetic user message saying "yes — confirmed via button: ..."
  and should then execute the action immediately. If she taps No, drop it.
  Use [CONFIRM] for ANY gated action (drafts, deletes, blocks, rules,
  subscription cancels). Don't use it for plain questions ("did you mean X?").
- POST-CONFIRMATION RULE (HARD): When you receive a message starting with
  "yes — confirmed via button: ...", the action you queued is already
  approved. You MUST execute the destructive tool call(s) in this same
  turn. If you need IDs you don't have in context (e.g. the message_ids
  for the emails you proposed deleting), call the lookup tool AND THEN
  call the destructive tool in the same turn — do NOT stop after the
  lookup with "let me pull those" / "give me a sec". One turn from Yes
  to done. After the destructive calls return, summarize the outcome
  by ground truth (e.g. "deleted 14 of 15 — Hyatt promo bounced because
  X").
- DELETE RECEIPT (HARD): If you call delete_email (once or many times) in
  a turn, your reply MUST include a numeric receipt from the tool results:
  how many returned ok:true vs ok:false, and a one-line reason for each
  failure (from the result). Examples of what counts: "15 moved to deleted,
  0 failed" or "12 ok, 3 failed: …". Banned when deletes ran: "all set",
  "taken care of", "should be good now", "cleaned that up" without the
  numbers, or any implication that it worked without citing counts. If
  you also sent "on it" for a slow step, the receipt still has to follow
  in the same turn once the tool results are back.
- If a step takes >10 seconds, send a short "on it" first.
- If she tells you a preference worth keeping ("I hate phone calls", "never
  unsubscribe me from USPS", "my partner is Alex"), call remember_fact so you
  have it next week. The "Known about the user" block below is your live
  memory — read from it every turn.
- Proactive life management: she shouldn't have to remember to "check in" with
  you. You already push scheduled briefs (morning 7am PT, evening 9pm PT, and
  on weekdays around noon a short nudge only if someone is waiting on her
  and/or a reminder is due soon). Treat those as part of the product, not
  spam — they exist so she can stay passive until something matters. If she
  says to stop a feed ("stop the morning briefs", "no more evening recaps",
  "turn off the midday check-ins"), call remember_fact with the matching key
  ("morning_brief", "evening_wrap", or "midday_nudge") and value "off";
  forget_fact the same key to turn back on. Confirm which feed she changed.
- When you answer a request, you don't have to be coldly efficient only: if a
  clear next action would help (draft one reply, triage a pile, block a
  sender, set a reminder), end with a single one-line offer — not a menu, one
  thing she can type in three words. If she's overwhelmed, prefer fewer choices.
- Replies when she's silent: you can't invent reasons to message her, but in
  chat you can reference what's coming from the scheduled briefs or reminders
  so the product feels one coherent "she's on it" system instead of a dumb bot
  that only reacts to pings.
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
4. The "Connected accounts" block tells you which integrations she has
   actually authorized. BEFORE you say "let me check your inbox" / "I'll
   look at your calendar" / etc., verify the relevant provider is listed.
   If it's not, don't pretend — say "I don't have access to that yet, want
   to connect it?" and the next tool call will trigger the one-tap connect
   flow. Never call a provider tool just to "see what happens" if the
   account block shows it's not connected — you'll trigger an unnecessary
   auth prompt.
5. When summarizing a batch (multiple unsubscribes, multiple deletes,
   or delete_email in parallel), enumerate by ground truth, not from
   memory: exact counts, then failures with reasons. Never "all done" or
   "all good" if anything returned ok:false. For deletes specifically,
   the user must be able to verify how many were soft-deleted — vague
   reassurance without counts is a failure.
6. The tool descriptions in your tool list tell you HOW to use each tool
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
        "name": "find_owed_replies",
        "description": (
            "Find inbox threads where Namrita is the one who owes a reply: "
            "the most recent message is from someone else, she was addressed "
            "(to/cc), it's older than days_threshold, and it's not marketing. "
            "Sorted oldest-first by days_waiting.\n\n"
            "WORKFLOW: Use when she asks 'what do I owe' / 'what am I behind "
            "on' / 'who's waiting on me'. Also auto-included in the morning "
            "brief — don't repeat unless she asks. After calling, surface as "
            "'X people waiting on you for N+ days', list top 3-5 by sender + "
            "subject. Offer to draft replies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_threshold": {
                    "type": "integer",
                    "description": "Min days waited before counting as owed. Default 2.",
                },
                "lookback_days": {
                    "type": "integer",
                    "description": "How many days of inbox to scan. Default 14.",
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
            "call this in parallel for each message_id.\n\n"
            "MANDATORY: After the last delete_email in this turn, your very "
            "next words to her must be a receipt: total ok count, total fail "
            "count, and per-failure one line (subject or id + error). If all "
            "succeeded, say the exact number. She should never have to ask "
            "'did it work' — you tell her, from these results, not from memory."
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
            "Scan Namrita's Outlook inbox over the last N days for receipts, "
            "renewals, and invoices to estimate what she's currently paying "
            "for. DETECTION ONLY — does NOT cancel anything. This is the "
            "EMAIL-based signal. For money that actually left her card/bank, "
            "use plaid_sync_transactions and plaid_recurring (after she has "
            "linked via plaid_start_link). If she asks to cancel a paid sub, say "
            "plainly that you can't do that yet. Takes ~10-30 seconds."
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
        "name": "list_events",
        "description": (
            "List Namrita's calendar events in the next N days (and "
            "optionally past M days). Returns subject, start, end, "
            "location, attendees, and event_id (use with cancel_event).\n\n"
            "Use for 'what's on my calendar' / 'what do I have today' / "
            "'when am I free Friday'. Times are in Pacific."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "Default 7."},
                "days_back": {"type": "integer", "description": "Default 0."},
            },
            "required": [],
        },
    },
    {
        "name": "find_free_time",
        "description": (
            "Find calendar gaps of at least duration_minutes in the next "
            "days_ahead days. Defaults: weekday 9am-6pm Pacific, skips "
            "weekends. Returns up to 10 candidate slots.\n\n"
            "WORKFLOW: use when she asks 'when am I free for X' / 'find "
            "me 30 min this week'. Show top 3-5 slots, let her pick, then "
            "call create_event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "duration_minutes": {"type": "integer"},
                "days_ahead": {"type": "integer", "description": "Default 7."},
                "work_hours_only": {"type": "boolean", "description": "Default true (skip weekends, constrain to work hours)."},
                "work_start_hour": {"type": "integer", "description": "Default 9."},
                "work_end_hour": {"type": "integer", "description": "Default 18."},
            },
            "required": ["duration_minutes"],
        },
    },
    {
        "name": "create_event",
        "description": (
            "Create a calendar event and (if attendees) send invites.\n\n"
            "WORKFLOW: HIGH STAKES. Always show the proposed event in chat "
            "(title, time, attendees) and gate with [CONFIRM] before "
            "calling. After it returns ok:true, share the web_link so she "
            "can verify in Outlook.\n\n"
            "start_iso/end_iso must be ISO 8601 with tz offset, e.g. "
            "'2026-04-26T14:00:00-07:00'. Use the 'Current time' line in "
            "your system prompt as anchor."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Email addresses.",
                },
                "body": {"type": "string", "description": "Optional event body."},
                "location": {"type": "string", "description": "Optional location string."},
            },
            "required": ["title", "start_iso", "end_iso"],
        },
    },
    {
        "name": "cancel_event",
        "description": (
            "Cancel/delete a calendar event by id (from list_events). For "
            "events with attendees this AUTOMATICALLY sends cancellation "
            "notices — never call without explicit approval. Gate with "
            "[CONFIRM] when there are attendees."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
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
        "name": "plaid_start_link",
        "description": (
            "Read-only bank/card onboarding via Plaid. Returns connect_url; "
            "the result also includes say_this_to_namrita (step-by-step — paste "
            "or adapt it so she is not lost). Suri only gets transaction read "
            "data through Plaid, not wire/transfer power. Plaid can link "
            "multiple institutions in one flow when the user is offered 'add another'. "
            "After she finishes in the browser: plaid_list_items, then sync/recurring. "
            "Requires server Plaid + SURI_PUBLIC_URL. Say 'on it' if needed."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "plaid_list_items",
        "description": (
            "List Plaid-linked institutions (item ids, names). No secrets. Use "
            "to see whether she has any bank data connected yet."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "plaid_sync_transactions",
        "description": (
            "Run Plaid /transactions/sync for all linked items (or one item_id) "
            "and return how many new transactions were added plus a small sample. "
            "Call before plaid_recurring if she just linked or if you need fresh "
            "tx. May take 10-30+ seconds; say 'on it' if needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Optional: sync only this Plaid item_id; omit for all items.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "plaid_recurring",
        "description": (
            "Fetch Plaid recurring transaction streams (inflow/outflow) for "
            "all linked items. This is the CARD/BANK view of regular charges — "
            "complement, not replace, find_paid_subscriptions (email). If empty, "
            "she may need more transaction history; try plaid_sync_transactions first."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
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

    plaid_rows = db.list_plaid_items_public()
    if plaid_rows:
        lines = "\n".join(
            f"  - {p.get('institution_name') or p['item_id']} (item_id {p['item_id']})"
            for p in plaid_rows
        )
        parts.append(f"Plaid (banks/cards) linked — this is REALITY:\n{lines}")
    else:
        parts.append(
            "Plaid (read-only tx data) linked: NONE — use plaid_start_link if she wants card/bank-side data."
        )

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
    accounts_block = accounts.status_block()
    facts = db.user_facts()
    sections = [PERSONA, time_block, truth_block, accounts_block]
    summary = db.latest_summary()
    if summary:
        sections.append(
            "Earlier conversation summary (everything before the live history "
            f"below, last updated {summary['created_at']}):\n{summary['summary']}"
        )
    if facts:
        facts_block = "Known about the user:\n" + "\n".join(
            f"- {k}: {v}" for k, v in facts.items()
        )
        sections.append(facts_block)
    return "\n\n".join(sections)


def _history():
    summary = db.latest_summary()
    after_id = summary["covers_through_message_id"] if summary else 0
    msgs = []
    for d, b in db.recent_messages(20, after_id=after_id):
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


def _anthropic_transcript_plausible(msgs: list) -> bool:
    """Claude's API requires non-empty, user-first, alternating user/assistant
    messages. Corrupt persisted state (e.g. duplicate user, wrong role) gets
    400s — this catches the common breakages before we call the API."""
    if not isinstance(msgs, list) or not msgs:
        return False
    if msgs[0].get("role") != "user":
        return False
    for i in range(len(msgs) - 1):
        if msgs[i].get("role") == msgs[i + 1].get("role"):
            return False
    return True


def _load_messages_for_turn() -> list:
    """Prefer the last turn's full Anthropic messages (with tool results).
    If missing or looks corrupt, fall back to text-only _history()."""
    state = db.get_conversation_messages()
    if not state or not isinstance(state, list) or not state:
        return _history()
    if state[-1].get("role") != "assistant":
        return _history()
    body = db.latest_inbound_body()
    if not body:
        return _history()
    out = state + [{"role": "user", "content": body}]
    if not _anthropic_transcript_plausible(out):
        print(
            "[agent] persisted conversation_state failed transcript check; "
            "using text-only _history()",
            file=sys.stderr,
            flush=True,
        )
        return _history()
    return out


def _compact_if_needed():
    """If too many messages have accumulated since the last summary, fold the
    older half into a new summary via Haiku. Synchronous — adds ~1-2s to the
    triggering turn. Errors are logged and swallowed so a Haiku outage can't
    break the user-facing turn."""
    summary = db.latest_summary()
    after_id = summary["covers_through_message_id"] if summary else 0
    pending = db.messages_since(after_id=after_id, limit=COMPACT_THRESHOLD * 4)
    if len(pending) <= COMPACT_THRESHOLD:
        return

    # Summarize everything except the most recent COMPACT_KEEP_LIVE messages
    # so the live window still has continuity into the new summary.
    to_summarize = pending[:-COMPACT_KEEP_LIVE]
    last_id = to_summarize[-1][0]

    transcript = "\n".join(
        f"{'NAMRITA' if d == 'inbound' else 'SURI'}: {b}"
        for _, d, b in to_summarize
    )
    prior_summary = summary["summary"] if summary else ""

    prompt_parts = []
    if prior_summary:
        prompt_parts.append(
            f"PREVIOUS SUMMARY (covers everything before the transcript):\n{prior_summary}\n"
        )
    prompt_parts.append(
        "NEW TRANSCRIPT TO FOLD IN:\n" + transcript + "\n\n"
        "Update the summary so it captures what's important about Namrita's "
        "ongoing context — open threads, decisions she's made, things Suri "
        "has done or promised, recurring themes, anything that future-Suri "
        "needs to remember to be useful. 5-10 hyphen bullets, no preamble, "
        "no markdown. Don't list every action — that's in the audit log."
    )

    try:
        resp = _get_client().messages.create(
            model=COMPACT_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": "\n".join(prompt_parts)}],
        )
    except Exception as e:
        print(f"[compact] failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return

    new_summary = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not new_summary:
        print("[compact] empty summary, skipping", file=sys.stderr, flush=True)
        return
    db.save_summary(new_summary, last_id)
    db.clear_conversation_state()
    print(
        f"[compact] folded {len(to_summarize)} msgs through id={last_id} "
        "(text-only history; tool state cleared for next turn)",
        file=sys.stderr,
        flush=True,
    )


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
    elif name == "find_owed_replies":
        result = outlook.find_owed_replies(
            days_threshold=input_.get("days_threshold", 2),
            lookback_days=input_.get("lookback_days", 14),
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
    elif name == "list_events":
        result = outlook.list_events(
            days_ahead=input_.get("days_ahead", 7),
            days_back=input_.get("days_back", 0),
        )
    elif name == "find_free_time":
        result = outlook.find_free_time(
            duration_minutes=input_["duration_minutes"],
            days_ahead=input_.get("days_ahead", 7),
            work_hours_only=input_.get("work_hours_only", True),
            work_start_hour=input_.get("work_start_hour", 9),
            work_end_hour=input_.get("work_end_hour", 18),
        )
    elif name == "create_event":
        result = outlook.create_event(
            title=input_["title"],
            start_iso=input_["start_iso"],
            end_iso=input_["end_iso"],
            attendees=input_.get("attendees"),
            body=input_.get("body", ""),
            location=input_.get("location", ""),
        )
    elif name == "cancel_event":
        result = outlook.cancel_event(input_["event_id"])
    elif name == "remember_fact":
        result = memory.remember_fact(input_["key"], input_["value"])
    elif name == "forget_fact":
        result = memory.forget_fact(input_["key"])
    elif name == "plaid_start_link":
        result = plaid_tool.start_link()
    elif name == "plaid_list_items":
        result = plaid_tool.list_items()
    elif name == "plaid_sync_transactions":
        result = plaid_tool.sync_transactions(input_.get("item_id"))
    elif name == "plaid_recurring":
        result = plaid_tool.fetch_recurring()
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
    _compact_if_needed()
    messages = _load_messages_for_turn()
    sent: list[str] = []
    # One turn_id per user message → spans every tool call in this loop.
    # Lets the audit log group "what happened in response to that ask".
    turn_id = uuid.uuid4().hex[:12]
    first_api_in_turn = True
    recovered_persisted_state = False
    while True:
        try:
            resp = _get_client().messages.create(
                model=MODEL,
                max_tokens=2048,
                system=_system_prompt(),
                messages=messages,
                tools=TOOLS,
            )
        except APIStatusError as e:
            # Bad persisted tool transcript (or oversized payload) — recover once
            # at the *start* of a turn, then re-build from text-only _history().
            if (
                e.status_code in (400, 404, 413, 422)
                and first_api_in_turn
                and not recovered_persisted_state
                and db.get_conversation_messages() is not None
            ):
                print(
                    f"[agent] messages.create {e.status_code} "
                    f"({str(e)[:200]}); clearing conversation_state, "
                    "retrying on text history",
                    file=sys.stderr,
                    flush=True,
                )
                db.clear_conversation_state()
                messages = _history()
                recovered_persisted_state = True
                continue
            raise
        first_api_in_turn = False
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if text:
            sent.append(text)
            if send:
                send(text)

        if resp.stop_reason != "tool_use":
            if any(b.type in ("text", "tool_use") for b in resp.content):
                messages.append(
                    {"role": "assistant", "content": _serialize(resp.content)}
                )
            try:
                db.set_conversation_messages(messages)
            except Exception as e:
                print(
                    f"[agent] conversation_state save failed: {type(e).__name__}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
            return sent

        messages.append({"role": "assistant", "content": _serialize(resp.content)})
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            try:
                result = _execute_tool(block.name, block.input, turn_id)
            except OutlookAuthRequired as e:
                # _token() already pushed the magic link to the user. Bail out
                # of the agent loop entirely — the OAuth callback will replay
                # the original prompt once consent completes. Don't feed an
                # error back to Claude; we don't want a half-answer. Skip
                # the audit log too: the auth prompt isn't a tool failure.
                print(
                    f"[agent] outlook auth required (state={e.state}); "
                    "bailing until callback fires.",
                    file=sys.stderr,
                    flush=True,
                )
                return sent
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
