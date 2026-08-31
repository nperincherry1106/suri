"""Background scheduler for time-based jobs (reminders, future: digests, polls).

Uses APScheduler's BackgroundScheduler so it runs in its own thread alongside
the FastAPI/uvicorn event loop in app.main.

Delivery to the user goes through app.push (callback registered by app.main:
stderr stub today, APNs once step 2 of the iOS plan lands).
"""
import sys
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app import db, proactive, push


_scheduler: BackgroundScheduler | None = None


def start():
    """Start the scheduler engine. Call restore_pending() AFTER push channels
    (e.g. telegram bot) are ready."""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.start()
    # Cron triggers carry their own timezone, so PT cadence works regardless
    # of the engine's UTC default.
    _scheduler.add_job(
        proactive.morning_brief,
        trigger=CronTrigger(hour=7, minute=0, timezone="America/Los_Angeles"),
        id="proactive-morning-brief",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.add_job(
        proactive.evening_wrap,
        trigger=CronTrigger(hour=21, minute=0, timezone="America/Los_Angeles"),
        id="proactive-evening-wrap",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # Mon–Fri only; 0=Mon … 4=Fri in APScheduler. Skips if nothing to surface.
    _scheduler.add_job(
        proactive.weekday_midday_nudge,
        trigger=CronTrigger(
            hour=12,
            minute=0,
            day_of_week="0-4",
            timezone="America/Los_Angeles",
        ),
        id="proactive-midday-nudge",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    print(
        "[scheduler] engine started + proactive: 7am / noontime* / 9pm wrap PT "
        "(*=weekdays, only if nudge-worthy) + user recurring jobs from DB",
        file=sys.stderr,
        flush=True,
    )


def restore_pending():
    if _scheduler is None:
        raise RuntimeError("scheduler not started")
    restored = 0
    for r in db.list_pending_reminders():
        _schedule_one(r["id"], r["fire_at"], r["body"])
        restored += 1
    recurring = 0
    for r in db.list_active_recurring_reminders():
        schedule_recurring(r)
        recurring += 1
    print(
        f"[scheduler] restored {restored} pending reminder(s), "
        f"{recurring} recurring job(s)",
        file=sys.stderr,
        flush=True,
    )


def shutdown():
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None


def _days_to_cron(days: str) -> dict:
    d = (days or "daily").strip().lower()
    if d == "weekdays":
        return {"day_of_week": "0-4"}
    if d == "weekends":
        return {"day_of_week": "5,6"}
    if d != "daily":
        return {"day_of_week": d}
    return {}


def schedule_recurring(row: dict):
    if _scheduler is None:
        raise RuntimeError("scheduler not started")
    rid = row["id"]
    tz = row.get("timezone") or "America/Los_Angeles"
    trigger_kw = {
        "hour": row["hour"],
        "minute": row["minute"],
        "timezone": tz,
        **_days_to_cron(row["days"]),
    }
    _scheduler.add_job(
        _fire_recurring,
        trigger=CronTrigger(**trigger_kw),
        args=[rid],
        id=f"recurring-{rid}",
        replace_existing=True,
        misfire_grace_time=3600,
    )


def cancel_recurring(recurring_id: int):
    if _scheduler is None:
        return
    job_id = f"recurring-{recurring_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)


def _schedule_one(reminder_id: int, fire_at: str, body: str):
    when = datetime.fromisoformat(fire_at)
    _scheduler.add_job(
        _fire,
        trigger=DateTrigger(run_date=when),
        args=[reminder_id, body],
        id=f"reminder-{reminder_id}",
        replace_existing=True,
        misfire_grace_time=3600,
    )


def schedule_reminder(reminder_id: int, fire_at: str, body: str):
    if _scheduler is None:
        raise RuntimeError("scheduler not started")
    _schedule_one(reminder_id, fire_at, body)


def cancel(reminder_id: int):
    if _scheduler is None:
        return
    job_id = f"reminder-{reminder_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)


def _fire_recurring(recurring_id: int):
    r = db.get_recurring_reminder(recurring_id)
    if r is None or r["status"] != "active":
        return
    print(
        f"[scheduler] firing recurring #{recurring_id} action={r['action']}",
        file=sys.stderr,
        flush=True,
    )
    ok = False
    if r["action"] == "email_scan":
        try:
            proactive.daily_email_scan()
            ok = True
        except Exception as e:
            print(
                f"[scheduler] recurring email_scan failed: {type(e).__name__}: {e}",
                file=sys.stderr,
                flush=True,
            )
    else:
        ok = push.push(f"reminder: {r['body']}")
    if ok:
        db.touch_recurring_fired(recurring_id)
        print(
            f"[scheduler] recurring #{recurring_id} delivered",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            f"[scheduler] recurring #{recurring_id} delivery failed",
            file=sys.stderr,
            flush=True,
        )


def _fire(reminder_id: int, body: str):
    """Runs in the scheduler's thread when a reminder is due. Delivery goes
    through app.push; CLI dev mode falls back to stdout if no transport is
    registered."""
    msg = f"reminder: {body}"
    print(f"[scheduler] firing reminder #{reminder_id}", file=sys.stderr, flush=True)
    if push.push(msg):
        print(
            f"[scheduler] reminder #{reminder_id} delivered",
            file=sys.stderr,
            flush=True,
        )
        db.mark_reminder_fired(reminder_id)
    else:
        print(f"\n[reminder fired - stdout fallback] {msg}", flush=True)
        db.mark_reminder_failed(reminder_id)
