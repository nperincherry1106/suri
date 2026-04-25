import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "sai.db"


@contextmanager
def conn():
    DB_PATH.parent.mkdir(exist_ok=True)
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
            """
        )


def log_inbound(body: str):
    with conn() as c:
        c.execute("INSERT INTO messages (direction, body) VALUES (?, ?)", ("inbound", body))


def log_outbound(body: str):
    with conn() as c:
        c.execute("INSERT INTO messages (direction, body) VALUES (?, ?)", ("outbound", body))


def recent_messages(limit: int = 20):
    with conn() as c:
        rows = c.execute(
            "SELECT direction, body FROM messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [(r["direction"], r["body"]) for r in reversed(rows)]


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
