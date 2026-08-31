"""Recurring schedules stored in SQLite + APScheduler cron jobs.

The agent converts natural language ('every day at 8pm', 'weekdays at noon')
into hour/minute/days. No deploy needed — jobs persist on Fly's /data volume
and register on the next process boot (or immediately via scheduler.schedule_recurring).
"""

from app import db, scheduler

VALID_DAYS = {"daily", "weekdays", "weekends"}
VALID_ACTIONS = {"push", "email_scan"}


def _validate_time(hour: int, minute: int) -> str | None:
    if not (0 <= hour <= 23):
        return f"hour must be 0-23, got {hour}"
    if not (0 <= minute <= 59):
        return f"minute must be 0-59, got {minute}"
    return None


def _validate_days(days: str) -> str | None:
    d = (days or "").strip().lower()
    if d in VALID_DAYS:
        return None
    if all(part.strip().isdigit() and 0 <= int(part.strip()) <= 6 for part in d.split(",")):
        return None
    return (
        "days must be 'daily', 'weekdays', 'weekends', or comma-separated "
        "weekday numbers 0=Mon … 6=Sun (e.g. '0,2,4')"
    )


def _human_schedule(hour: int, minute: int, days: str, tz: str) -> str:
    h12 = hour % 12 or 12
    ampm = "am" if hour < 12 else "pm"
    t = f"{h12}:{minute:02d}{ampm}"
    d = days.lower()
    if d == "daily":
        cadence = "every day"
    elif d == "weekdays":
        cadence = "weekdays"
    elif d == "weekends":
        cadence = "weekends"
    else:
        cadence = f"on days {days}"
    return f"{cadence} at {t} {tz}"


def set_recurring_reminder(
    hour: int,
    minute: int,
    body: str,
    days: str = "daily",
    action: str = "push",
    timezone: str = "America/Los_Angeles",
    replace_existing: bool = True,
):
    err = _validate_time(hour, minute)
    if err:
        return {"ok": False, "error": err}
    err = _validate_days(days)
    if err:
        return {"ok": False, "error": err}
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "body cannot be empty"}
    action = (action or "push").strip().lower()
    if action not in VALID_ACTIONS:
        return {
            "ok": False,
            "error": f"action must be one of {sorted(VALID_ACTIONS)}",
        }
    days = days.strip().lower()
    timezone = (timezone or "America/Los_Angeles").strip()

    if replace_existing and action != "push":
        old_id = db.cancel_recurring_by_action(action)
        if old_id is not None:
            try:
                scheduler.cancel_recurring(old_id)
            except Exception:
                pass

    rid = db.create_recurring_reminder(
        hour, minute, days, body, action=action, timezone=timezone
    )
    try:
        row = db.get_recurring_reminder(rid)
        scheduler.schedule_recurring(row)
    except Exception as e:
        db.mark_recurring_cancelled(rid)
        return {"ok": False, "error": f"scheduling failed: {type(e).__name__}: {e}"}

    if action == "email_scan":
        db.set_fact("daily_email_scan", body)

    schedule_human = _human_schedule(hour, minute, days, timezone)
    return {
        "ok": True,
        "id": rid,
        "schedule": schedule_human,
        "body": body,
        "action": action,
    }


def list_recurring_reminders():
    return db.list_active_recurring_reminders()


def cancel_recurring_reminder(recurring_id: int):
    r = db.get_recurring_reminder(recurring_id)
    if r is None:
        return {"ok": False, "error": f"no recurring reminder with id {recurring_id}"}
    if r["status"] != "active":
        return {
            "ok": False,
            "error": f"#{recurring_id} is already {r['status']}",
        }
    scheduler.cancel_recurring(recurring_id)
    db.mark_recurring_cancelled(recurring_id)
    if r.get("action") == "email_scan":
        db.set_fact("daily_email_scan", "off")
    return {"ok": True, "id": recurring_id}
