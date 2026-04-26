import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Where to store the SQLite db and other persistent state.
# In production (fly.io) this is mounted as a volume at /data.
# Locally it defaults to <repo>/data/.
_DATA_DIR = Path(os.environ.get("SURI_DATA_DIR", Path(__file__).parent.parent / "data"))
DB_PATH = _DATA_DIR / "sai.db"


@contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                direction TEXT NOT NULL,
                body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_facts (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pending_actions (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS subscriptions_found (
                id INTEGER PRIMARY KEY,
                service_name TEXT NOT NULL,
                last_charge_date DATE,
                amount REAL,
                source_email_id TEXT,
                cancellation_url TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS marketing_senders (
                sender_domain TEXT PRIMARY KEY,
                service_name TEXT,
                unsubscribe_url TEXT NOT NULL,
                post_supported INTEGER NOT NULL DEFAULT 0,
                last_seen DATETIME,
                email_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY,
                fire_at DATETIME NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                fired_at DATETIME
            );
            CREATE TABLE IF NOT EXISTS paid_subscriptions (
                service_name TEXT PRIMARY KEY,
                sender_domain TEXT,
                last_charge_amount TEXT,
                last_charge_currency TEXT,
                last_charge_date DATE,
                cadence TEXT,
                sample_subject TEXT,
                last_seen DATETIME
            );
            CREATE TABLE IF NOT EXISTS agent_actions (
                id INTEGER PRIMARY KEY,
                turn_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                input_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                ok INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_agent_actions_created
                ON agent_actions(created_at DESC);
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                id INTEGER PRIMARY KEY,
                summary TEXT NOT NULL,
                covers_through_message_id INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def log_inbound(body: str):
    with conn() as c:
        c.execute("INSERT INTO messages (direction, body) VALUES (?, ?)", ("inbound", body))


def log_outbound(body: str):
    with conn() as c:
        c.execute("INSERT INTO messages (direction, body) VALUES (?, ?)", ("outbound", body))


def recent_messages(limit: int = 20, after_id: int = 0):
    with conn() as c:
        rows = c.execute(
            "SELECT direction, body FROM messages "
            "WHERE id > ? ORDER BY id DESC LIMIT ?",
            (after_id, limit),
        ).fetchall()
    return [(r["direction"], r["body"]) for r in reversed(rows)]


def messages_since(after_id: int = 0, limit: int = 1000):
    """Return (id, direction, body) for messages with id > after_id, oldest
    first. Used by the compaction loop to materialize what to summarize."""
    with conn() as c:
        rows = c.execute(
            "SELECT id, direction, body FROM messages "
            "WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after_id, limit),
        ).fetchall()
    return [(r["id"], r["direction"], r["body"]) for r in rows]


def latest_summary():
    """Most recent conversation summary, or None if none exists."""
    with conn() as c:
        row = c.execute(
            "SELECT id, summary, covers_through_message_id, created_at "
            "FROM conversation_summaries ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "summary": row["summary"],
        "covers_through_message_id": row["covers_through_message_id"],
        "created_at": row["created_at"],
    }


def save_summary(summary: str, covers_through_message_id: int):
    with conn() as c:
        cur = c.execute(
            "INSERT INTO conversation_summaries (summary, covers_through_message_id) "
            "VALUES (?, ?)",
            (summary, covers_through_message_id),
        )
        return cur.lastrowid


def user_facts():
    with conn() as c:
        rows = c.execute("SELECT key, value FROM user_facts").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_fact(key: str, value: str):
    with conn() as c:
        c.execute(
            "INSERT INTO user_facts (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
            (key, value),
        )


def pending_actions():
    with conn() as c:
        rows = c.execute(
            "SELECT id, type, payload FROM pending_actions WHERE status = 'pending'"
        ).fetchall()
    return [
        {"id": r["id"], "type": r["type"], "payload": json.loads(r["payload"])}
        for r in rows
    ]


def create_pending_action(action_id: str, action_type: str, payload: dict):
    with conn() as c:
        c.execute(
            "INSERT INTO pending_actions (id, type, payload) VALUES (?, ?, ?)",
            (action_id, action_type, json.dumps(payload)),
        )


def get_pending_action(action_id: str):
    with conn() as c:
        row = c.execute(
            "SELECT id, type, payload, status FROM pending_actions WHERE id = ?",
            (action_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "type": row["type"],
        "payload": json.loads(row["payload"]),
        "status": row["status"],
    }


def update_pending_action_status(action_id: str, status: str):
    with conn() as c:
        c.execute(
            "UPDATE pending_actions SET status = ? WHERE id = ?",
            (status, action_id),
        )


def upsert_marketing_sender(
    sender_domain: str,
    service_name: str | None,
    unsubscribe_url: str,
    post_supported: bool,
    last_seen: str,
    email_count: int,
):
    with conn() as c:
        c.execute(
            "INSERT INTO marketing_senders "
            "(sender_domain, service_name, unsubscribe_url, post_supported, last_seen, email_count) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(sender_domain) DO UPDATE SET "
            "service_name = excluded.service_name, "
            "unsubscribe_url = excluded.unsubscribe_url, "
            "post_supported = excluded.post_supported, "
            "last_seen = excluded.last_seen, "
            "email_count = excluded.email_count",
            (
                sender_domain,
                service_name,
                unsubscribe_url,
                1 if post_supported else 0,
                last_seen,
                email_count,
            ),
        )


def get_marketing_sender(sender_domain: str):
    with conn() as c:
        row = c.execute(
            "SELECT sender_domain, service_name, unsubscribe_url, post_supported, status "
            "FROM marketing_senders WHERE sender_domain = ?",
            (sender_domain,),
        ).fetchone()
    if row is None:
        return None
    return {
        "sender_domain": row["sender_domain"],
        "service_name": row["service_name"],
        "unsubscribe_url": row["unsubscribe_url"],
        "post_supported": bool(row["post_supported"]),
        "status": row["status"],
    }


def list_unsubscribed_senders():
    with conn() as c:
        rows = c.execute(
            "SELECT sender_domain, service_name FROM marketing_senders "
            "WHERE status = 'unsubscribed' ORDER BY sender_domain"
        ).fetchall()
    return [
        {"sender_domain": r["sender_domain"], "service_name": r["service_name"]}
        for r in rows
    ]


def list_failed_unsubscribes():
    with conn() as c:
        rows = c.execute(
            "SELECT sender_domain, service_name FROM marketing_senders "
            "WHERE status = 'failed' ORDER BY sender_domain"
        ).fetchall()
    return [
        {"sender_domain": r["sender_domain"], "service_name": r["service_name"]}
        for r in rows
    ]


def clear_message_history():
    with conn() as c:
        c.execute("DELETE FROM messages")


def find_marketing_senders_match(query: str):
    """Match a sender by exact domain, suffix domain, or service-name substring.
    Returns a list of matching rows (possibly empty, possibly multiple)."""
    q = query.lower().strip()
    with conn() as c:
        rows = c.execute(
            "SELECT sender_domain, service_name, unsubscribe_url, post_supported, status "
            "FROM marketing_senders "
            "WHERE LOWER(sender_domain) = ? "
            "   OR LOWER(sender_domain) LIKE ? "
            "   OR LOWER(service_name) LIKE ?",
            (q, f"%.{q}", f"%{q}%"),
        ).fetchall()
    return [
        {
            "sender_domain": r["sender_domain"],
            "service_name": r["service_name"],
            "unsubscribe_url": r["unsubscribe_url"],
            "post_supported": bool(r["post_supported"]),
            "status": r["status"],
        }
        for r in rows
    ]


def set_marketing_sender_status(sender_domain: str, status: str):
    with conn() as c:
        c.execute(
            "UPDATE marketing_senders SET status = ? WHERE sender_domain = ?",
            (status, sender_domain),
        )


def forget_fact(key: str):
    with conn() as c:
        c.execute("DELETE FROM user_facts WHERE key = ?", (key,))


def create_reminder(fire_at: str, body: str) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO reminders (fire_at, body) VALUES (?, ?)",
            (fire_at, body),
        )
        return cur.lastrowid


def get_reminder(reminder_id: int):
    with conn() as c:
        row = c.execute(
            "SELECT id, fire_at, body, status FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "fire_at": row["fire_at"],
        "body": row["body"],
        "status": row["status"],
    }


def list_pending_reminders():
    with conn() as c:
        rows = c.execute(
            "SELECT id, fire_at, body FROM reminders "
            "WHERE status = 'pending' ORDER BY fire_at ASC"
        ).fetchall()
    return [
        {"id": r["id"], "fire_at": r["fire_at"], "body": r["body"]}
        for r in rows
    ]


def mark_reminder_fired(reminder_id: int):
    with conn() as c:
        c.execute(
            "UPDATE reminders SET status = 'fired', fired_at = CURRENT_TIMESTAMP WHERE id = ?",
            (reminder_id,),
        )


def mark_reminder_failed(reminder_id: int):
    with conn() as c:
        c.execute(
            "UPDATE reminders SET status = 'failed', fired_at = CURRENT_TIMESTAMP WHERE id = ?",
            (reminder_id,),
        )


def mark_reminder_cancelled(reminder_id: int):
    with conn() as c:
        c.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ?",
            (reminder_id,),
        )


def upsert_paid_subscription(
    service_name: str,
    sender_domain: str | None,
    last_charge_amount: str | None,
    last_charge_currency: str | None,
    last_charge_date: str | None,
    cadence: str | None,
    sample_subject: str | None,
    last_seen: str,
):
    with conn() as c:
        c.execute(
            "INSERT INTO paid_subscriptions "
            "(service_name, sender_domain, last_charge_amount, last_charge_currency, "
            " last_charge_date, cadence, sample_subject, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(service_name) DO UPDATE SET "
            "  sender_domain = excluded.sender_domain, "
            "  last_charge_amount = excluded.last_charge_amount, "
            "  last_charge_currency = excluded.last_charge_currency, "
            "  last_charge_date = excluded.last_charge_date, "
            "  cadence = excluded.cadence, "
            "  sample_subject = excluded.sample_subject, "
            "  last_seen = excluded.last_seen",
            (
                service_name,
                sender_domain,
                last_charge_amount,
                last_charge_currency,
                last_charge_date,
                cadence,
                sample_subject,
                last_seen,
            ),
        )


def log_agent_action(
    turn_id: str,
    tool_name: str,
    input_obj: dict,
    result_obj,
    ok: bool,
):
    """Persist a single tool invocation. Result is truncated to ~2KB so the
    table doesn't bloat from large Graph payloads (full result is still in
    stderr logs if needed)."""
    result_str = json.dumps(result_obj, default=str)
    if len(result_str) > 2048:
        result_str = result_str[:2048] + "...[truncated]"
    with conn() as c:
        c.execute(
            "INSERT INTO agent_actions "
            "(turn_id, tool_name, input_json, result_json, ok) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                turn_id,
                tool_name,
                json.dumps(input_obj, default=str),
                result_str,
                1 if ok else 0,
            ),
        )


def recent_agent_actions(hours_back: float = 24, limit: int = 50):
    """Return recent tool invocations in oldest-to-newest order, capped at
    `limit`. Used by the ground-truth block and the what_did_you_do tool."""
    with conn() as c:
        rows = c.execute(
            "SELECT turn_id, tool_name, input_json, result_json, ok, created_at "
            "FROM agent_actions "
            "WHERE created_at >= datetime('now', ?) "
            "ORDER BY id DESC LIMIT ?",
            (f"-{hours_back} hours", limit),
        ).fetchall()
    def _maybe_json(s: str):
        # Stored values are JSON, but result payloads may be truncated and
        # therefore unparseable — fall back to the raw string in that case.
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return s

    out = [
        {
            "turn_id": r["turn_id"],
            "tool_name": r["tool_name"],
            "input": _maybe_json(r["input_json"]),
            "result": _maybe_json(r["result_json"]),
            "ok": bool(r["ok"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    out.reverse()
    return out


def list_paid_subscriptions():
    with conn() as c:
        rows = c.execute(
            "SELECT service_name, sender_domain, last_charge_amount, last_charge_currency, "
            "       last_charge_date, cadence, sample_subject "
            "FROM paid_subscriptions ORDER BY last_charge_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]
