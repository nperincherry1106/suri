"""Scheduled, unprompted briefs Suri pushes to Namrita.

Cadences (all America/Los_Angeles):
- morning_brief 7:00: inbox + owed + today's reminders
- weekday_midday_nudge 12:00 Mon–Fri only, and only if there's something
  to surface (owed replies and/or a reminder in the next 4h) — no spam
- evening_wrap 21:00: what you did today + tomorrow's reminders

Composed morning/evening use Claude; midday is a short template to stay
reliable. Toggle per feed via user_facts, e.g. remember_fact("midday_nudge", "off").
"""

import json
import os
import sys
from datetime import datetime, timedelta

from anthropic import Anthropic

from app import db, push
from app.tools import outlook


_PROACTIVE_MODEL = "claude-sonnet-4-5"

_PROACTIVE_PROMPT = """You are Suri, Namrita's personal assistant, sending her
an unprompted message. She didn't ask — you're showing up on purpose, to take
cognitive load off her so she doesn't have to remember to check email or open
a dozen apps. This is a feature, not a bother.

Open with a word or two that signals what this is ("morning", "end of day",
etc.) so she recognizes the pattern. End with at most one easy handle to reply
(e.g. "say triage if you want me to go through the pile") — never a wall of
options.

Tone: warm, brief, direct. Lowercase fine. Telegram plain-text only — no
markdown, no emoji unless she'd use them. Be honest: every line MUST come
from the data block below. If a section is empty, omit it entirely. If
everything is empty, send a single line ("quiet morning" / "nothing to
report tonight"). Never pad. Never invent."""


_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _disabled(toggle_key: str) -> bool:
    return (db.user_facts().get(toggle_key) or "").strip().lower() == "off"


def _compose_and_push(label: str, system: str, user_msg: str):
    try:
        resp = _get_client().messages.create(
            model=_PROACTIVE_MODEL,
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        print(f"[proactive:{label}] compose failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        print(f"[proactive:{label}] empty draft, skipping", file=sys.stderr, flush=True)
        return
    if not push.push(text):
        print(f"[proactive:{label}] push channel unavailable", file=sys.stderr, flush=True)
        return
    print(f"[proactive:{label}] delivered", file=sys.stderr, flush=True)


def morning_brief():
    if _disabled("morning_brief"):
        print("[proactive:morning] disabled via user_facts", file=sys.stderr, flush=True)
        return
    try:
        items = outlook.triage_inbox(hours_back=12, max_results=20)
    except Exception as e:
        print(f"[proactive:morning] triage failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        items = []
    try:
        owed = outlook.find_owed_replies(days_threshold=2, lookback_days=14)
        if isinstance(owed, dict) and not owed.get("ok", True):
            owed = []
    except Exception as e:
        print(f"[proactive:morning] owed-replies failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        owed = []
    pending = db.list_pending_reminders()
    today = datetime.now().astimezone().date().isoformat()
    today_reminders = [r for r in pending if (r["fire_at"] or "")[:10] == today]
    marketing_count = sum(1 for i in items if i.get("is_marketing"))
    real_items = [i for i in items if not i.get("is_marketing")]

    payload = {
        "date": today,
        "inbox_last_12h_excluding_marketing": real_items,
        "marketing_noise_count": marketing_count,
        "owed_replies_2d_plus": owed[:10],
        "reminders_firing_today": today_reminders,
    }
    user_msg = (
        "Compose Namrita's morning brief. Hyphen-list bullets. Sections (omit "
        "any that have no content):\n"
        "- needs your attention: real human emails, urgent / time-sensitive "
        "(from inbox_last_12h_excluding_marketing)\n"
        "- waiting on you: from owed_replies_2d_plus, sender + short subject "
        "+ N days waiting\n"
        "- on your plate today: reminders firing today\n"
        "- noise: one-line summary of marketing volume (e.g. '12 marketing — "
        "want me to triage?')\n\n"
        "Be specific (sender + 3-word subject hint), never vague. If "
        "everything is empty, just send 'quiet morning'.\n\n"
        "Data:\n" + json.dumps(payload, default=str, indent=2)
    )
    _compose_and_push("morning", _PROACTIVE_PROMPT, user_msg)


def evening_wrap():
    if _disabled("evening_wrap"):
        print("[proactive:evening] disabled via user_facts", file=sys.stderr, flush=True)
        return
    actions = db.recent_agent_actions(hours_back=14, limit=200)
    pending = db.list_pending_reminders()
    tomorrow = (datetime.now().astimezone().date() + timedelta(days=1)).isoformat()
    tomorrow_reminders = [r for r in pending if (r["fire_at"] or "")[:10] == tomorrow]

    payload = {
        "actions_today": actions,
        "reminders_firing_tomorrow": tomorrow_reminders,
    }
    user_msg = (
        "Compose Namrita's evening wrap-up. Tight, hyphen-list. Sections (omit "
        "any that have no content):\n"
        "- handled today: group by tool, count, name key items (e.g. "
        "'unsubscribed from 3 senders: hyatt, goodreads, nyt')\n"
        "- failures: list ok:false items explicitly with the reason — never "
        "bury these\n"
        "- on tap tomorrow: reminders scheduled for tomorrow\n\n"
        "If nothing happened today AND no reminders tomorrow, just send "
        "'nothing to report tonight'.\n\n"
        "Data:\n" + json.dumps(payload, default=str, indent=2)
    )
    _compose_and_push("evening", _PROACTIVE_PROMPT, user_msg)


def _parse_fire_at(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return t


def weekday_midday_nudge():
    """12pm PT, Mon–Fri only. Skips if nothing to say — no empty pings."""
    if _disabled("midday_nudge"):
        print(
            "[proactive:midday] disabled via user_facts",
            file=sys.stderr,
            flush=True,
        )
        return
    now = datetime.now().astimezone()
    try:
        owed = outlook.find_owed_replies(days_threshold=1, lookback_days=10)
        if isinstance(owed, dict) and not owed.get("ok", True):
            owed = []
    except Exception as e:
        print(
            f"[proactive:midday] owed-replies failed: {type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )
        owed = []
    in_four_h = now + timedelta(hours=4)
    soon: list[dict] = []
    for r in db.list_pending_reminders():
        when = _parse_fire_at(str(r.get("fire_at", "")))
        if when is None:
            continue
        if now <= when <= in_four_h:
            soon.append(r)
    if not owed and not soon:
        print(
            "[proactive:midday] nothing to nudge, skipping (no cost to her)",
            file=sys.stderr,
            flush=True,
        )
        return
    parts: list[str] = [
        "midday — pinging because i'd rather surface this than have you hold it in your head."
    ]
    if owed:
        n = len(owed)
        o0 = owed[0]
        subj = (o0.get("subject") or "no subject")[:70]
        snd = o0.get("from_name") or o0.get("from_email") or "someone"
        parts.append(
            f"people waiting on you: {n} (top: {snd} – {subj}…). "
            f"say 'draft' or 'what do i owe' if you want me to go deeper."
        )
    if soon:
        s0 = soon[0]
        body = (s0.get("body") or "reminder")[:120]
        wh = str(s0.get("fire_at", ""))[:16]
        parts.append(f"up soon ({wh}): {body}")
    text = " ".join(parts)
    if not push.push(text):
        print(
            "[proactive:midday] push channel unavailable",
            file=sys.stderr,
            flush=True,
        )
        return
    print("[proactive:midday] delivered", file=sys.stderr, flush=True)
