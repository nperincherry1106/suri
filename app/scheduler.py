"""Background scheduler for time-based jobs (reminders, future: digests, polls).

Uses APScheduler's BackgroundScheduler so it runs in its own thread regardless
of whether the main process is asyncio-based (telegram bot) or sync (CLI dev).

The push channel (e.g. telegram) registers itself via set_push_callback().
We deliberately avoid importing telegram_bot at fire-time — explicit
registration is more debuggable than implicit module-attribute lookup.
"""
import sys
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from app import db


_scheduler: BackgroundScheduler | None = None
_push_callback: Callable[[str], None] | None = None


def set_push_callback(cb: Callable[[str], None]):
    """Register a sync function the scheduler can call to deliver fired
    reminders to the user. Must be safe to call from a non-asyncio thread."""
    global _push_callback
    _push_callback = cb
    print("[scheduler] push callback registered", file=sys.stderr, flush=True)


def start():
    """Start the scheduler engine. Call restore_pending() AFTER push channels
    (e.g. telegram bot) are ready."""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.start()
    print("[scheduler] engine started", file=sys.stderr, flush=True)


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
    """Runs in the scheduler's thread when a reminder is due. Pushes via the
    registered callback if there is one; falls back to stdout for CLI dev mode."""
    msg = f"reminder: {body}"
    print(
        f"[scheduler] firing reminder #{reminder_id}, "
        f"push_callback={'registered' if _push_callback else 'NONE'}",
        file=sys.stderr,
        flush=True,
    )
    delivered = False
    if _push_callback is not None:
        try:
            _push_callback(msg)
            delivered = True
            print(
                f"[scheduler] reminder #{reminder_id} delivered via push callback",
                file=sys.stderr,
                flush=True,
            )
        except Exception as e:
            print(
                f"[scheduler] push callback raised for reminder #{reminder_id}: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
                flush=True,
            )
    if not delivered:
        print(f"\n[reminder fired - stdout fallback] {msg}", flush=True)
    db.mark_reminder_fired(reminder_id)
