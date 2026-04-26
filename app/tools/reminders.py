"""Scheduled reminders. The agent is responsible for converting natural-language
times ("tomorrow 9am", "in 3 hours") into ISO 8601 timestamps using the current
time provided in its system prompt. The tool only accepts ISO."""

from datetime import datetime

from app import db, scheduler


def set_reminder(when_iso: str, body: str):
    try:
        when = datetime.fromisoformat(when_iso)
    except ValueError as e:
        return {
            "ok": False,
            "error": f"invalid timestamp: {e}. use ISO 8601 with tz, e.g. '2026-04-26T09:00:00-07:00'",
        }
    if when.tzinfo is None:
        return {
            "ok": False,
            "error": "timestamp must include a timezone offset, e.g. '2026-04-26T09:00:00-07:00'",
        }
    body = body.strip()
    if not body:
        return {"ok": False, "error": "reminder body cannot be empty"}

    reminder_id = db.create_reminder(when_iso, body)
    try:
        scheduler.schedule_reminder(reminder_id, when_iso, body)
    except Exception as e:
        return {"ok": False, "error": f"scheduling failed: {type(e).__name__}: {e}"}
    return {
        "ok": True,
        "id": reminder_id,
        "fire_at": when_iso,
        "body": body,
    }


def list_reminders():
    return db.list_pending_reminders()


def cancel_reminder(reminder_id: int):
    r = db.get_reminder(reminder_id)
    if r is None:
        return {"ok": False, "error": f"no reminder with id {reminder_id}"}
    if r["status"] != "pending":
        return {
            "ok": False,
            "error": f"reminder #{reminder_id} is already {r['status']}, can't cancel",
        }
    scheduler.cancel(reminder_id)
    db.mark_reminder_cancelled(reminder_id)
    return {"ok": True, "id": reminder_id}
