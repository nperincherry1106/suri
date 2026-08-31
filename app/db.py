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
            CREATE TABLE IF NOT EXISTS pending_oauth (
                state TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                flow_json TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                original_prompt TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
            -- Single row: the exact Anthropic messages[] array (incl. tool_use /
            -- tool_result blocks) as of the end of the last completed agent turn.
            -- Without this, _history() is text-only and the model can't see
            -- message_ids from a prior triage on the next turn.
            CREATE TABLE IF NOT EXISTS conversation_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                messages_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plaid_items (
                item_id TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                institution_id TEXT,
                institution_name TEXT,
                cursor TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS plaid_link_sessions (
                id TEXT PRIMARY KEY,
                link_token TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            -- iOS device push tokens. Single-user app today, but keeping the
            -- table per-device (not per-user) so the iOS-on-iPhone +
            -- iOS-on-iPad case works once she has both. apns_env is either
            -- 'production' (TestFlight / App Store builds) or 'sandbox'
            -- (Xcode debug builds straight to a USB-attached device); the
            -- iOS app reports which one it is at registration time.
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                apns_token TEXT NOT NULL UNIQUE,
                apns_env TEXT NOT NULL DEFAULT 'production',
                bundle_id TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'ios',
                active INTEGER NOT NULL DEFAULT 1,
                last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_devices_active
                ON devices(active, apns_env);
            -- Sessions issued after a successful Sign in with Apple. The
            -- iOS app stores the `session_jwt` in the keychain and presents
            -- it on every /api/v1/* call. We persist a row per JWT so we
            -- can revoke individual sessions later (e.g. "log out this
            -- device") without rotating the global signing secret.
            CREATE TABLE IF NOT EXISTS app_sessions (
                jti TEXT PRIMARY KEY,
                apple_sub TEXT NOT NULL,
                issued_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                expires_at DATETIME NOT NULL,
                last_seen_at DATETIME,
                user_agent TEXT,
                revoked INTEGER NOT NULL DEFAULT 0,
                revoked_at DATETIME
            );
            CREATE INDEX IF NOT EXISTS idx_app_sessions_sub
                ON app_sessions(apple_sub, revoked);
            -- Persisted transactions from Plaid /transactions/sync. Today
            -- sync() returns added+modified+removed slices that we discard
            -- after extracting a sample; storing them locally turns the
            -- Finances tab into a fast SQLite read instead of a per-render
            -- Plaid round-trip, and unlocks aggregations (today's spend,
            -- weekly digest) that would otherwise require either re-pulling
            -- everything or paying Plaid for /transactions/get on every load.
            -- Plaid's `amount` convention: positive = outflow (you spent),
            -- negative = inflow (refund / credit). We preserve that sign.
            CREATE TABLE IF NOT EXISTS plaid_transactions (
                transaction_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                account_id TEXT,
                amount REAL NOT NULL,
                iso_currency_code TEXT,
                date TEXT NOT NULL,
                authorized_date TEXT,
                name TEXT,
                merchant_name TEXT,
                pending INTEGER NOT NULL DEFAULT 0,
                category_primary TEXT,
                category_detailed TEXT,
                payment_channel TEXT,
                raw_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_plaid_tx_date
                ON plaid_transactions(date DESC);
            CREATE INDEX IF NOT EXISTS idx_plaid_tx_item
                ON plaid_transactions(item_id, date DESC);
            CREATE INDEX IF NOT EXISTS idx_plaid_tx_pending
                ON plaid_transactions(pending);
            CREATE TABLE IF NOT EXISTS recurring_reminders (
                id INTEGER PRIMARY KEY,
                hour INTEGER NOT NULL,
                minute INTEGER NOT NULL DEFAULT 0,
                days TEXT NOT NULL DEFAULT 'daily',
                timezone TEXT NOT NULL DEFAULT 'America/Los_Angeles',
                body TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT 'push',
                status TEXT NOT NULL DEFAULT 'active',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_fired_at DATETIME
            );
            """
        )
    _migrate_recurring_from_facts()


def _migrate_recurring_from_facts():
    """One-time-style migration: legacy daily_email_scan user_facts become a
    recurring job so existing Fly installs keep the 8pm scan after deploy."""
    fact = user_facts().get("daily_email_scan", "").strip()
    if not fact or fact.lower() == "off":
        return
    with conn() as c:
        existing = c.execute(
            "SELECT id FROM recurring_reminders "
            "WHERE action = 'email_scan' AND status = 'active' LIMIT 1"
        ).fetchone()
        if existing:
            return
        c.execute(
            "INSERT INTO recurring_reminders (hour, minute, days, body, action) "
            "VALUES (20, 0, 'daily', ?, 'email_scan')",
            (fact,),
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


def get_conversation_messages() -> list | None:
    """The persisted Anthropic messages[] from the end of the last turn, or
    None if we should fall back to text-only _history()."""
    with conn() as c:
        row = c.execute("SELECT messages_json FROM conversation_state WHERE id = 1").fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["messages_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def set_conversation_messages(msgs: list):
    with conn() as c:
        c.execute(
            "INSERT INTO conversation_state (id, messages_json) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET messages_json = excluded.messages_json",
            (json.dumps(msgs, default=str),),
        )


def clear_conversation_state():
    with conn() as c:
        c.execute("DELETE FROM conversation_state WHERE id = 1")


def latest_inbound_body() -> str | None:
    with conn() as c:
        row = c.execute(
            "SELECT body FROM messages WHERE direction = 'inbound' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return None if row is None else row["body"]


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
    clear_conversation_state()


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


def create_recurring_reminder(
    hour: int,
    minute: int,
    days: str,
    body: str,
    action: str = "push",
    timezone: str = "America/Los_Angeles",
) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO recurring_reminders "
            "(hour, minute, days, timezone, body, action) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (hour, minute, days, timezone, body, action),
        )
        return cur.lastrowid


def get_recurring_reminder(recurring_id: int):
    with conn() as c:
        row = c.execute(
            "SELECT id, hour, minute, days, timezone, body, action, status, "
            "last_fired_at FROM recurring_reminders WHERE id = ?",
            (recurring_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def list_active_recurring_reminders():
    with conn() as c:
        rows = c.execute(
            "SELECT id, hour, minute, days, timezone, body, action, last_fired_at "
            "FROM recurring_reminders WHERE status = 'active' "
            "ORDER BY hour, minute, id"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_recurring_cancelled(recurring_id: int):
    with conn() as c:
        c.execute(
            "UPDATE recurring_reminders SET status = 'cancelled' WHERE id = ?",
            (recurring_id,),
        )


def touch_recurring_fired(recurring_id: int):
    with conn() as c:
        c.execute(
            "UPDATE recurring_reminders SET last_fired_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (recurring_id,),
        )


def cancel_recurring_by_action(action: str) -> int | None:
    """Cancel active recurring job(s) with this action. Returns cancelled id."""
    with conn() as c:
        row = c.execute(
            "SELECT id FROM recurring_reminders "
            "WHERE action = ? AND status = 'active' LIMIT 1",
            (action,),
        ).fetchone()
        if row is None:
            return None
        c.execute(
            "UPDATE recurring_reminders SET status = 'cancelled' WHERE id = ?",
            (row["id"],),
        )
        return row["id"]


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


def create_pending_oauth(
    state: str,
    provider: str,
    flow_json: str,
    telegram_user_id: int,
    original_prompt: str | None,
):
    with conn() as c:
        c.execute(
            "INSERT INTO pending_oauth (state, provider, flow_json, telegram_user_id, original_prompt) "
            "VALUES (?, ?, ?, ?, ?)",
            (state, provider, flow_json, telegram_user_id, original_prompt),
        )


def get_pending_oauth(state: str):
    with conn() as c:
        row = c.execute(
            "SELECT state, provider, flow_json, telegram_user_id, original_prompt, created_at "
            "FROM pending_oauth WHERE state = ?",
            (state,),
        ).fetchone()
    if row is None:
        return None
    return {
        "state": row["state"],
        "provider": row["provider"],
        "flow_json": row["flow_json"],
        "telegram_user_id": row["telegram_user_id"],
        "original_prompt": row["original_prompt"],
        "created_at": row["created_at"],
    }


def delete_pending_oauth(state: str):
    with conn() as c:
        c.execute("DELETE FROM pending_oauth WHERE state = ?", (state,))


def prune_expired_oauth(older_than_minutes: int = 60):
    with conn() as c:
        c.execute(
            "DELETE FROM pending_oauth "
            "WHERE created_at < datetime('now', ?)",
            (f"-{older_than_minutes} minutes",),
        )


# --- Plaid ------------------------------------------------------------------


def create_plaid_link_session(session_id: str, link_token: str):
    with conn() as c:
        c.execute(
            "INSERT INTO plaid_link_sessions (id, link_token) VALUES (?, ?)",
            (session_id, link_token),
        )


def get_plaid_link_session(session_id: str):
    with conn() as c:
        row = c.execute(
            "SELECT id, link_token, created_at FROM plaid_link_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "link_token": row["link_token"],
        "created_at": row["created_at"],
    }


def delete_plaid_link_session(session_id: str):
    with conn() as c:
        c.execute("DELETE FROM plaid_link_sessions WHERE id = ?", (session_id,))


def prune_stale_plaid_link_sessions(older_than_minutes: int = 20):
    with conn() as c:
        c.execute(
            "DELETE FROM plaid_link_sessions WHERE created_at < datetime('now', ?)",
            (f"-{older_than_minutes} minutes",),
        )


def upsert_plaid_item(
    item_id: str,
    access_token: str,
    institution_id: str | None,
    institution_name: str | None,
):
    with conn() as c:
        c.execute(
            "INSERT INTO plaid_items (item_id, access_token, institution_id, institution_name) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET "
            "  access_token = excluded.access_token, "
            "  institution_id = excluded.institution_id, "
            "  institution_name = excluded.institution_name, "
            "  updated_at = CURRENT_TIMESTAMP",
            (item_id, access_token, institution_id, institution_name),
        )


def set_plaid_cursor(item_id: str, cursor: str | None):
    with conn() as c:
        c.execute(
            "UPDATE plaid_items SET cursor = ?, updated_at = CURRENT_TIMESTAMP WHERE item_id = ?",
            (cursor, item_id),
        )


def get_plaid_item(item_id: str):
    with conn() as c:
        row = c.execute("SELECT * FROM plaid_items WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    return {
        "item_id": row["item_id"],
        "access_token": row["access_token"],
        "institution_id": row["institution_id"],
        "institution_name": row["institution_name"],
        "cursor": row["cursor"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_plaid_items_public():
    """Institution info only — no access_token."""
    with conn() as c:
        rows = c.execute(
            "SELECT item_id, institution_id, institution_name, created_at, updated_at, "
            "CASE WHEN cursor IS NOT NULL AND cursor != '' THEN 1 ELSE 0 END AS has_sync_cursor "
            "FROM plaid_items ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_plaid_item(item_id: str):
    with conn() as c:
        c.execute("DELETE FROM plaid_items WHERE item_id = ?", (item_id,))


# ---------------------------------------------------------------------------
# devices (APNs targets)
# ---------------------------------------------------------------------------


def register_device(
    apns_token: str,
    bundle_id: str,
    apns_env: str = "production",
    platform: str = "ios",
) -> int:
    """Idempotent upsert by apns_token. Bumps last_seen and re-activates if
    the token had previously been deactivated (a user can revoke and re-grant
    notification permission). Returns the row id."""
    env = (apns_env or "production").strip().lower()
    if env not in ("production", "sandbox"):
        env = "production"
    with conn() as c:
        c.execute(
            "INSERT INTO devices (apns_token, apns_env, bundle_id, platform, active, last_seen) "
            "VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP) "
            "ON CONFLICT(apns_token) DO UPDATE SET "
            "  apns_env = excluded.apns_env, "
            "  bundle_id = excluded.bundle_id, "
            "  platform = excluded.platform, "
            "  active = 1, "
            "  last_seen = CURRENT_TIMESTAMP",
            (apns_token, env, bundle_id, platform),
        )
        row = c.execute(
            "SELECT id FROM devices WHERE apns_token = ?", (apns_token,)
        ).fetchone()
    return int(row["id"])


def list_active_devices() -> list[dict]:
    """All devices currently eligible for push. Caller groups by apns_env to
    pick the right APNs host (production vs sandbox)."""
    with conn() as c:
        rows = c.execute(
            "SELECT id, apns_token, apns_env, bundle_id, platform, last_seen, created_at "
            "FROM devices WHERE active = 1 ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def deactivate_device(apns_token: str) -> None:
    """Called when APNs returns 410 Unregistered (app was uninstalled or the
    user disabled notifications). We don't delete the row — keeping it lets
    re-registration with the same token preserve history."""
    with conn() as c:
        c.execute(
            "UPDATE devices SET active = 0 WHERE apns_token = ?",
            (apns_token,),
        )


# ---------------------------------------------------------------------------
# app_sessions (JWTs issued after Sign in with Apple)
# ---------------------------------------------------------------------------


def create_app_session(
    jti: str,
    apple_sub: str,
    expires_at: str,
    user_agent: str | None = None,
) -> None:
    """Persist a freshly-issued session. `jti` is the JWT's unique id (claim
    'jti'); revocation works by setting revoked=1 here so a stolen token
    can be killed without rotating the signing secret."""
    with conn() as c:
        c.execute(
            "INSERT INTO app_sessions (jti, apple_sub, expires_at, user_agent) "
            "VALUES (?, ?, ?, ?)",
            (jti, apple_sub, expires_at, user_agent),
        )


def get_app_session(jti: str) -> dict | None:
    with conn() as c:
        row = c.execute(
            "SELECT jti, apple_sub, issued_at, expires_at, last_seen_at, "
            "user_agent, revoked, revoked_at "
            "FROM app_sessions WHERE jti = ?",
            (jti,),
        ).fetchone()
    return dict(row) if row else None


def touch_app_session(jti: str) -> None:
    """Bump last_seen_at on every authenticated request. Cheap signal we
    can use later for 'idle for 90 days, revoke' policies."""
    with conn() as c:
        c.execute(
            "UPDATE app_sessions SET last_seen_at = CURRENT_TIMESTAMP WHERE jti = ?",
            (jti,),
        )


def revoke_app_session(jti: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE app_sessions "
            "SET revoked = 1, revoked_at = CURRENT_TIMESTAMP WHERE jti = ?",
            (jti,),
        )


# ---------------------------------------------------------------------------
# plaid_transactions (persisted from /transactions/sync)
# ---------------------------------------------------------------------------


def upsert_plaid_transaction(
    transaction_id: str,
    item_id: str,
    account_id: str | None,
    amount: float,
    iso_currency_code: str | None,
    date: str,
    authorized_date: str | None,
    name: str | None,
    merchant_name: str | None,
    pending: bool,
    category_primary: str | None,
    category_detailed: str | None,
    payment_channel: str | None,
    raw_json: str | None,
) -> None:
    """Idempotent upsert. Plaid sometimes re-emits transactions in the
    `modified` slice (e.g. category re-classification, pending->posted with
    same id) so we always overwrite mutable fields and refresh updated_at."""
    with conn() as c:
        c.execute(
            "INSERT INTO plaid_transactions ("
            "  transaction_id, item_id, account_id, amount, iso_currency_code, "
            "  date, authorized_date, name, merchant_name, pending, "
            "  category_primary, category_detailed, payment_channel, raw_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(transaction_id) DO UPDATE SET "
            "  item_id = excluded.item_id, "
            "  account_id = excluded.account_id, "
            "  amount = excluded.amount, "
            "  iso_currency_code = excluded.iso_currency_code, "
            "  date = excluded.date, "
            "  authorized_date = excluded.authorized_date, "
            "  name = excluded.name, "
            "  merchant_name = excluded.merchant_name, "
            "  pending = excluded.pending, "
            "  category_primary = excluded.category_primary, "
            "  category_detailed = excluded.category_detailed, "
            "  payment_channel = excluded.payment_channel, "
            "  raw_json = excluded.raw_json, "
            "  updated_at = CURRENT_TIMESTAMP",
            (
                transaction_id, item_id, account_id, float(amount), iso_currency_code,
                date, authorized_date, name, merchant_name, 1 if pending else 0,
                category_primary, category_detailed, payment_channel, raw_json,
            ),
        )


def delete_plaid_transactions(transaction_ids: list[str]) -> int:
    """Used by sync() to apply the `removed` slice when Plaid drops a tx
    (e.g. pending tx that never posted). Returns rows affected."""
    if not transaction_ids:
        return 0
    with conn() as c:
        cur = c.execute(
            "DELETE FROM plaid_transactions WHERE transaction_id IN "
            f"({','.join('?' for _ in transaction_ids)})",
            transaction_ids,
        )
        return cur.rowcount


def list_plaid_transactions(
    days: int | None = 30,
    limit: int = 200,
    item_id: str | None = None,
) -> list[dict]:
    """Return transactions sorted by date desc. `days=None` means no date
    filter (use sparingly — could be thousands of rows)."""
    where: list[str] = []
    args: list = []
    if days is not None:
        where.append("date >= date('now', ?)")
        args.append(f"-{int(days)} days")
    if item_id:
        where.append("item_id = ?")
        args.append(item_id)
    sql = (
        "SELECT transaction_id, item_id, account_id, amount, iso_currency_code, "
        "date, authorized_date, name, merchant_name, pending, "
        "category_primary, category_detailed, payment_channel "
        "FROM plaid_transactions"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date DESC, transaction_id DESC LIMIT ?"
    args.append(int(limit))
    with conn() as c:
        rows = c.execute(sql, tuple(args)).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["pending"] = bool(d["pending"])
        out.append(d)
    return out


def count_plaid_transactions() -> int:
    with conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM plaid_transactions").fetchone()
    return int(row["n"]) if row else 0
