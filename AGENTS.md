# Reminder_pal - Telegram Reminder Bot

## Purpose
A Telegram bot that allows users to set reminders with custom messages to be sent at specific times. Supports one-time reminders (fire once at a datetime) and repeatable reminders (fire on selected weekdays at a fixed time).

## Core Features

### 1. Set Reminder
- User picks reminder type: one-time or repeatable
- One-time flow: strict `YYYY-MM-DD HH:MM` input, validated (format + future check), message text, confirmation summary, save
- Repeatable flow: day-of-week multi-select inline keyboard (toggle in-place), `HH:MM` time input, message text, confirmation summary, save
- `/cancel` works at every step, returns to main menu with no leftover state
- Shared helpers: `validate_datetime()`, `validate_hhmm()`, `validate_message()`, `_confirm_keyboard()`, `_cleanup_conversation()`
- **Status:** ✅ Implemented (one-time + repeatable)

### 2. View Reminders
- Paginated list (5 per page) with Prev/Next/Home navigation
- Each reminder shown as a pressable button with a label (🔔 for one-time, 🔁 for repeatable)
- Pressing a reminder opens a detail view (same summary format as Set Reminder confirmation)
- Detail view has Home and Delete buttons
- **Status:** ✅ Implemented

### 3. Delete Reminder
- From detail view: Delete → Yes/No confirmation → hard `DELETE FROM reminders`
- `reminder_logs` rows are intentionally left untouched (no FK cascade)
- After delete: returns to refreshed list on same page, or previous page if now empty, or "no reminders" empty state
- `delete_reminder()` in main.py calls `scheduler.cancel_reminder_job()` to remove the APScheduler job
- **Status:** ✅ Implemented

### 4. Database
- SQLite via `sqlite3` stdlib — no server, no ORM
- **`reminders` table:**
  - `id` INTEGER PRIMARY KEY AUTOINCREMENT
  - `user_id` INTEGER NOT NULL
  - `message` TEXT NOT NULL
  - `type` TEXT NOT NULL CHECK(type IN ('one_time', 'repeatable'))
  - `remind_at` TEXT — ISO datetime, used only when type='one_time', NULL otherwise
  - `time_of_day` TEXT — 'HH:MM', used only when type='repeatable', NULL otherwise
  - `days_of_week` TEXT — comma-separated ints 0-6 (0=Sunday...6=Saturday), repeatable only
  - `start_date` TEXT — ISO date, repeatable only, defaults to today at insert time
  - `end_date` TEXT — ISO date, repeatable only, NULL = no end date
  - `status` TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'paused', 'deleted', 'completed', 'missed'))
  - `created_at` TEXT NOT NULL DEFAULT (datetime('now'))
- **`reminder_logs` table:**
  - `id` INTEGER PRIMARY KEY AUTOINCREMENT
  - `reminder_id` INTEGER NOT NULL REFERENCES reminders(id)
  - `sent_at` TEXT NOT NULL — ISO datetime of the actual send
  - `delivery_status` TEXT NOT NULL CHECK(delivery_status IN ('sent', 'failed'))
- `get_db()` — auto-creates both tables on first call
- `get_user_reminders(user_id, limit, offset)` — paginated fetch, active only
- `get_reminder_by_id(reminder_id)` — single fetch with user_id for ownership checks
- `delete_reminder(reminder_id)` — hard delete + cancels scheduler job
- `format_reminder_detail(r)` — formats detail view string
- **Status:** ✅ Implemented

### 5. Scheduler
- `scheduler.py` module using APScheduler's `AsyncIOScheduler` (in-memory, not persisted)
- On every startup: jobs rebuilt from `reminders` table (DB is source of truth)
- **One-time:** `DateTrigger` set to `remind_at`, job id `f"reminder_{id}"`
- **Repeatable:** `CronTrigger` with day-of-week converted from our int convention (0=Sun..6=Sat) to APScheduler's day-name strings (sun, mon, ..., sat) to avoid off-by-one. Trigger fires at `time_of_day` on selected days.
- **Startup logic (`_load_and_schedule_all`):**
  - One-time active reminders past due → status set to 'missed' (not sent late, not scheduled)
  - One-time active reminders in the future → rescheduled via `schedule_one_time_reminder()`
  - Repeatable active reminders → rescheduled via `schedule_repeatable_reminder()`
- **Job callback (`send_reminder`):**
  - Fetches reminder row, sends `bot.send_message(user_id, message)`
  - Inserts `reminder_logs` row with `delivery_status` = 'sent' or 'failed' (exceptions caught)
  - One-time + sent → status set to 'completed'
- **Wired into Set Reminder:** both one-time and repeatable flows call `scheduler.schedule_*()` immediately after DB insert (no restart needed)
- **Wired into Delete Reminder:** `delete_reminder()` calls `scheduler.cancel_reminder_job()`
- **Lifecycle:** `post_init` hook calls `init_scheduler(bot)`, `post_shutdown` hook calls `shutdown_scheduler()`
- `cancel_reminder_job(reminder_id)` — safe to call even if job doesn't exist
- **Status:** ✅ Implemented

## Tech Stack
- `uv` — package management
- `python-telegram-bot` v22.x — Telegram API
- `APScheduler` 3.11.3 — task scheduling (AsyncIOScheduler, in-memory)
- `sqlite3` (stdlib) — database

## Files
- `main.py` — bot handlers, conversation flow, DB helpers, inline keyboards
- `scheduler.py` — APScheduler setup, job functions, startup rescheduling
- `.env` — `BOT_TOKEN` (gitignored)
- `reminders.db` — SQLite database (auto-created)

## Current State
- ✅ Bot structure with `/start` command and inline keyboard
- ✅ Database with SQLite — `reminders` + `reminder_logs` tables auto-created
- ✅ `get_user_reminders()` — paginated query by user_id
- ✅ View Reminders — inline list with Prev/Next/Home, detail view
- ✅ Set Reminder — one-time + repeatable flows, ConversationHandler, validation
- ✅ Delete Reminder — detail view → confirm → hard delete → job cancelled
- ✅ Scheduler — APScheduler AsyncIOScheduler, DateTrigger + CronTrigger, startup reschedule, post_init/post_shutdown hooks
- ❌ Edit reminder — not implemented (no flow to modify an existing reminder)
- ❌ Timezone handling — all datetimes are naive (local time implied, no tz awareness)
- ❌ Paused status — 'paused' is in the CHECK constraint but no UI/logic to pause or resume
- ❌ Missed reminders UI — 'missed' status set on startup but no way for users to see/manage them
