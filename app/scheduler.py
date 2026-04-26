"""Background scheduler for time-based jobs (reminders, future: digests, polls).

Uses APScheduler's BackgroundScheduler so it runs in its own thread regardless
of whether the main process is asyncio-based (telegram bot) or sync (CLI dev).

Delivery to the user goes through app.push (registered by telegram_bot.py).
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
    print("[scheduler] engine started + proactive cadence registered (7am / 9pm PT)",
          file=sys.stderr, flush=True)


def restore_pending():
    if _scheduler is None:
        raise RuntimeError("scheduler not started")
    restored = 0
    for r in db.list_pending_reminders():
        _schedule_one(r["id"], r["fire_at"], r["body"])
        restored += 1
    print(
        f"[scheduler] restored {restored} pending reminder(s)",
        file=sys.stderr,
        flush=True,
    )


def shutdown():
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None


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
        # No transport / callback raised. Fall back to stdout so CLI dev still
        # sees it, then mark failed instead of fired so we don't silently lose
        # the reminder. Recoverable manually via a future re-run / requeue.
        print(f"\n[reminder fired - stdout fallback] {msg}", flush=True)
        db.mark_reminder_failed(reminder_id)
