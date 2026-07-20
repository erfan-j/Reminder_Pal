import logging
import datetime as dt

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Single in-memory scheduler for the whole process. Jobs are NOT persisted by
# APScheduler itself — on every startup we rebuild them from the `reminders`
# table, which is the real source of truth.
scheduler = AsyncIOScheduler()

# Our DB stores days_of_week as 0=Sunday..6=Saturday. APScheduler's CronTrigger
# does NOT use that convention, so we always convert to day-name strings to
# avoid an off-by-one bug rather than passing raw ints.
_DAY_INT_TO_CRON_NAME = {0: "sun", 1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat"}

_bot = None  # set once in init_scheduler()


def init_scheduler(bot) -> None:
    """Start the scheduler and reschedule every active reminder from the DB.

    Must be called once, from inside a running asyncio event loop
    (e.g. PTB's `post_init` hook), after the bot is ready to send messages.
    """
    global _bot
    _bot = bot
    scheduler.start()
    logger.info("Scheduler started.")
    _load_and_schedule_all()


def shutdown_scheduler() -> None:
    """Cleanly stop the scheduler on bot shutdown."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down.")


async def send_reminder(reminder_id: int) -> None:
    """Job callback: send one reminder, log it, and update status if one-time."""
    from main import get_db, get_reminder_by_id  # deferred import avoids circular import

    r = get_reminder_by_id(reminder_id)
    if not r:
        logger.warning("send_reminder: reminder %s no longer exists, skipping.", reminder_id)
        return

    delivery_status = "sent"
    try:
        await _bot.send_message(chat_id=r["user_id"], text=f"⏰ Reminder: {r['message']}")
    except Exception:
        logger.exception("Failed to send reminder %s to user %s", reminder_id, r["user_id"])
        delivery_status = "failed"

    conn = get_db()
    conn.execute(
        "INSERT INTO reminder_logs (reminder_id, sent_at, delivery_status) VALUES (?, ?, ?)",
        (reminder_id, dt.datetime.now().isoformat(timespec="seconds"), delivery_status),
    )
    if r["type"] == "one_time" and delivery_status == "sent":
        conn.execute("UPDATE reminders SET status = 'completed' WHERE id = ?", (reminder_id,))
    conn.commit()

    logger.info("Reminder %s (%s) processed with delivery_status=%s", reminder_id, r["type"], delivery_status)


def schedule_one_time_reminder(reminder: dict) -> None:
    """Add/replace a DateTrigger job for a one-time reminder. reminder needs: id, remind_at."""
    run_date = dt.datetime.strptime(reminder["remind_at"], "%Y-%m-%dT%H:%M:%S")
    job_id = f"reminder_{reminder['id']}"
    scheduler.add_job(
        send_reminder,
        trigger=DateTrigger(run_date=run_date),
        args=[reminder["id"]],
        id=job_id,
        replace_existing=True,
    )
    logger.info("Scheduled one-time reminder %s for %s", reminder["id"], run_date)


def schedule_repeatable_reminder(reminder: dict) -> None:
    """Add/replace a CronTrigger job for a repeatable reminder.

    reminder needs: id, days_of_week (comma-separated ints, 0=Sun..6=Sat), time_of_day ("HH:MM").
    """
    days = [int(x) for x in reminder["days_of_week"].split(",") if x.strip() != ""]
    day_of_week = ",".join(_DAY_INT_TO_CRON_NAME[d] for d in days)
    hour_str, minute_str = reminder["time_of_day"].split(":")
    job_id = f"reminder_{reminder['id']}"
    scheduler.add_job(
        send_reminder,
        trigger=CronTrigger(day_of_week=day_of_week, hour=int(hour_str), minute=int(minute_str)),
        args=[reminder["id"]],
        id=job_id,
        replace_existing=True,
    )
    logger.info(
        "Scheduled repeatable reminder %s for days=%s time=%s",
        reminder["id"], day_of_week, reminder["time_of_day"],
    )


def cancel_reminder_job(reminder_id: int) -> None:
    """Remove a scheduled job, if any. Safe to call even if the job doesn't exist
    (e.g. a one-time reminder that already fired, or a reminder that was never scheduled)."""
    job_id = f"reminder_{reminder_id}"
    try:
        scheduler.remove_job(job_id)
        logger.info("Cancelled scheduled job for reminder %s", reminder_id)
    except Exception:
        pass


def _load_and_schedule_all() -> None:
    """On startup: reschedule future one-time reminders, mark overdue ones as
    missed (no send), and reschedule all active repeatable reminders."""
    from main import get_db

    conn = get_db()
    now = dt.datetime.now()

    one_time_rows = conn.execute(
        "SELECT id, remind_at FROM reminders WHERE type = 'one_time' AND status = 'active'"
    ).fetchall()
    missed_count = 0
    scheduled_one_time = 0
    for row in one_time_rows:
        r = dict(row)
        remind_at = dt.datetime.strptime(r["remind_at"], "%Y-%m-%dT%H:%M:%S")
        if remind_at <= now:
            conn.execute("UPDATE reminders SET status = 'missed' WHERE id = ?", (r["id"],))
            logger.info("Marked one-time reminder %s as missed (was due %s)", r["id"], remind_at)
            missed_count += 1
        else:
            schedule_one_time_reminder(r)
            scheduled_one_time += 1
    conn.commit()

    repeatable_rows = conn.execute(
        "SELECT id, days_of_week, time_of_day FROM reminders WHERE type = 'repeatable' AND status = 'active'"
    ).fetchall()
    for row in repeatable_rows:
        schedule_repeatable_reminder(dict(row))

    logger.info(
        "Startup scheduling complete: %d one-time job(s) rescheduled, %d marked missed, %d repeatable job(s) loaded.",
        scheduled_one_time, missed_count, len(repeatable_rows),
    )