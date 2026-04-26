"""Scheduled, unprompted briefs Suri pushes to Namrita.

Two cadences today:
- morning_brief at 7am PT: triage + today's reminders
- evening_wrap at 9pm PT: today's audited actions + tomorrow's reminders

Each is composed by Claude over structured input so the wording stays warm
but the facts come from the database / Graph (no hallucination). Toggle off
per-brief via user_facts, e.g. remember_fact("morning_brief", "off").
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
an unprompted message. She didn't ask — you're showing up because it's the
scheduled time.

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
