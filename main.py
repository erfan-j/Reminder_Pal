import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters, ContextTypes

import sqlite3
import os
import datetime as dt

import scheduler

DB_PATH = os.path.join(os.path.dirname(__file__), "reminders.db")

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('one_time', 'repeatable')),
            remind_at TEXT,
            time_of_day TEXT,
            days_of_week TEXT,
            start_date TEXT,
            end_date TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'paused', 'deleted', 'completed', 'missed')),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminder_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reminder_id INTEGER NOT NULL REFERENCES reminders(id),
            sent_at TEXT NOT NULL,
            delivery_status TEXT NOT NULL CHECK(delivery_status IN ('sent', 'failed'))
        )
    """)
    conn.commit()
    return conn

def get_user_reminders(user_id: int, limit: int = 5, offset: int = 0) -> list:
    conn = get_db()
    cur = conn.execute(
        "SELECT id, message, type, remind_at, time_of_day, days_of_week, status FROM reminders WHERE user_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset)
    )
    return cur.fetchall()


def get_reminder_by_id(reminder_id: int) -> dict | None:
    conn = get_db()
    cur = conn.execute(
        "SELECT id, user_id, message, type, remind_at, time_of_day, days_of_week, status FROM reminders WHERE id = ?",
        (reminder_id,)
    )
    row = cur.fetchone()
    return dict(row) if row else None


def delete_reminder(reminder_id: int) -> None:
    """Hard-delete a reminder row. Does NOT touch reminder_logs."""
    conn = get_db()
    conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    conn.commit()
    scheduler.cancel_reminder_job(reminder_id)


def format_reminder_detail(r: dict) -> str:
    """Format a reminder row into a detail-view string."""
    if r["type"] == "repeatable":
        days_str = ", ".join(DAY_NAMES[d] for d in sorted(int(x) for x in r["days_of_week"].split(",") if x.strip()))
        return (
            "📋 **Reminder Detail**\n\n"
            f"📅 **Days:** {days_str}\n"
            f"⏰ **Time:** {r['time_of_day']}\n"
            f"💬 **Message:** {r['message']}"
        )
    return (
        "📋 **Reminder Detail**\n\n"
        f"📅 **Time:** {r['remind_at'][:16].replace('T', ' ')}\n"
        f"💬 **Message:** {r['message']}"
    )

def _load_bot_token() -> str:
    """Read BOT_TOKEN from .env or os.environ, no python-dotenv needed."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    os.environ["BOT_TOKEN"] = token
                    return token
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("ERROR: BOT_TOKEN not found. Set it in .env or export BOT_TOKEN=...")
    return token

BOT_TOKEN = _load_bot_token()

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Shared validation helpers ──

DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def validate_datetime(text: str) -> tuple[dt.datetime | None, str | None]:
    """Returns (parsed_datetime, error_message). On success error is None."""
    try:
        parsed = dt.datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        return None, "❌ Invalid format! Please send the date and time exactly like:\n`YYYY-MM-DD HH:MM`\n\nExample: `2026-07-14 14:00`"
    if parsed <= dt.datetime.now():
        return None, "❌ That time is in the past! Please send a future date and time.\nFormat: `YYYY-MM-DD HH:MM`"
    return parsed, None


def validate_hhmm(text: str) -> tuple[str | None, str | None]:
    """Returns (normalized_HH:MM, error_message). On success error is None."""
    try:
        parts = text.split(":")
        if len(parts) != 2:
            raise ValueError
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except ValueError:
        return None, "❌ Invalid time! Send in `HH:MM` format (24-hour).\nExample: `14:30`"
    return f"{h:02d}:{m:02d}", None


def validate_message(text: str) -> tuple[str | None, str | None]:
    """Returns (message, error_message). On success error is None."""
    msg = text.strip()
    if not msg:
        return None, "❌ Message can't be empty. Please send your reminder message:"
    return msg, None


def _confirm_keyboard(confirm_cb: str) -> InlineKeyboardMarkup:
    """Shared Confirm/Cancel keyboard for both flows."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data=confirm_cb)],
        [InlineKeyboardButton("❌ Cancel", callback_data="remind_cancel")],
    ])


def _cleanup_conversation(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove all conversation-scoped user_data keys."""
    for key in ("remind_msg", "remind_at", "reminder_type", "repeat_days", "repeat_time", "repeat_msg"):
        context.user_data.pop(key, None)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    welcome_text = (
        f"👋 Hi {user.first_name}! Welcome to Reminder_pal — your personal reminder assistant.\n\n"
        "📌 What can I do?\n"
        "• Set reminders for tasks, events, or breaks\n"
        "• Help you stay on track throughout the day\n"
        "• Never let you forget what matters!\n\n"
        "👇 Choose an option below to get started:"
    )
    keyboard = [
        [InlineKeyboardButton("⏰ Set a Reminder", callback_data="set_reminder")],
        [InlineKeyboardButton("📋 View My Reminders", callback_data="view_reminders")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.effective_chat.send_message(welcome_text, reply_markup=reply_markup)

async def _render_reminder_list(query, user_id: int, page: int) -> None:
    """Build and edit-in-place the paginated reminder list. Caller must ensure page has items."""
    reminders = get_user_reminders(user_id, limit=5, offset=page * 5)
    kb = []
    for r in reminders:
        if r["type"] == "repeatable":
            days = r["days_of_week"] or "?"
            label = f"🔁 {r['time_of_day']} (days:{days}) - {r['message'][:20]}..."
        else:
            label = f"🔔 {r['remind_at'][:16]} - {r['message'][:20]}..."
        kb.append([InlineKeyboardButton(label, callback_data=f"view_{r['id']}")])
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data="view_prev"))
    if len(reminders) == 5:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data="view_next"))
    if nav_row:
        kb.append(nav_row)
    kb.append([InlineKeyboardButton("🏠 Home", callback_data="home")])
    await query.edit_message_text(f"📋 Your reminders (page {page + 1}):", reply_markup=InlineKeyboardMarkup(kb))


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "set_reminder":
        await query.edit_message_text("📅 What type of reminder?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏰ One-time", callback_data="remind_type_one_time")],
            [InlineKeyboardButton("🔁 Repeatable", callback_data="remind_type_repeatable")],
            [InlineKeyboardButton("❌ Cancel", callback_data="remind_cancel")],
        ]))
        return

    if data == "home":
        await start(update, context)
        return

    # ── View / navigate reminder list ──

    if data in ("view_reminders", "view_prev", "view_next"):
        page = int(context.user_data.get("view_page", 0))
        if data == "view_prev":
            page = max(0, page - 1)
        elif data == "view_next":
            page += 1
        context.user_data["view_page"] = page
        reminders = get_user_reminders(user_id, limit=5, offset=page * 5)
        if not reminders:
            if data == "view_next" and page > 0:
                # Fell off the end — go back a page
                page -= 1
                context.user_data["view_page"] = page
                reminders = get_user_reminders(user_id, limit=5, offset=page * 5)
            if not reminders:
                await query.edit_message_text("📋 No reminders yet.", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Home", callback_data="home")]
                ]))
                return
        await _render_reminder_list(query, user_id, page)
        return

    # ── Detail view / delete flow ──

    if data.startswith("view_"):
        reminder_id = int(data.split("_")[1])
        r = get_reminder_by_id(reminder_id)
        if not r or r["user_id"] != user_id:
            await query.edit_message_text("❌ Reminder not found.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 back", callback_data="home")]
            ]))
            return
        detail = format_reminder_detail(r)
        context.user_data["delete_reminder_id"] = reminder_id
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 back", callback_data="del_detail_home")],
            [InlineKeyboardButton("🗑 Delete", callback_data="del_confirm")],
        ])
        await query.edit_message_text(detail, reply_markup=kb)
        return

    if data == "del_detail_home":
        page = int(context.user_data.get("view_page", 0))
        reminders = get_user_reminders(user_id, limit=5, offset=page * 5)
        if not reminders:
            page = max(0, page - 1)
            context.user_data["view_page"] = page
            reminders = get_user_reminders(user_id, limit=5, offset=page * 5)
        if not reminders:
            await query.edit_message_text("📋 No reminders yet.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 back", callback_data="home")]
            ]))
            return
        await _render_reminder_list(query, user_id, page)
        return

    if data == "del_confirm":
        await query.edit_message_text(
            "⚠️ **Are you sure you want to delete this reminder?**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes", callback_data="del_yes")],
                [InlineKeyboardButton("❌ No", callback_data="del_no")],
            ])
        )
        return

    if data == "del_yes":
        r_id = context.user_data.pop("delete_reminder_id", None)
        if r_id:
            delete_reminder(r_id)
        page = int(context.user_data.get("view_page", 0))
        reminders = get_user_reminders(user_id, limit=5, offset=page * 5)
        if not reminders and page > 0:
            page -= 1
            context.user_data["view_page"] = page
            reminders = get_user_reminders(user_id, limit=5, offset=page * 5)
        if not reminders:
            await query.edit_message_text("🗑 Reminder deleted. You have no reminders left.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Home", callback_data="home")]
            ]))
            return
        await _render_reminder_list(query, user_id, page)
        return

    if data == "del_no":
        page = int(context.user_data.get("view_page", 0))
        reminders = get_user_reminders(user_id, limit=5, offset=page * 5)
        if not reminders:
            page = max(0, page - 1)
            context.user_data["view_page"] = page
            reminders = get_user_reminders(user_id, limit=5, offset=page * 5)
        if not reminders:
            await query.edit_message_text("📋 No reminders yet.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Home", callback_data="home")]
            ]))
            return
        await _render_reminder_list(query, user_id, page)
        return

    # ── Other handlers ──

    if data == "remind_type_one_time":
        context.user_data["reminder_type"] = "one_time"
        await query.edit_message_text(
            "⏰ When should I remind you?\n"
            "Please send the date and time in this format:\n"
            "`YYYY-MM-DD HH:MM`\n\n"
            "For example: `2026-07-14 14:00`"
        )
    elif query.data == "remind_type_repeatable":
        # Handled by ConversationHandler entry point, this is a no-op fallback
        pass
    elif query.data == "remind_cancel":
        await query.edit_message_text("❌ Reminder cancelled.")
        await start(update, context)


# ── ConversationHandler thin entry-point wrappers ──

async def remind_type_one_time_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await button_callback(update, context)
    return 1  # → state 1 (remind_datetime)


async def remind_type_repeatable_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["repeat_days"] = set()
    kb = _days_keyboard(context.user_data["repeat_days"])
    await update.callback_query.edit_message_text("📅 Select the days for this reminder:", reply_markup=kb)
    return 4


async def remind_cancel_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel from a callback button — clean up all conversation state."""
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("❌ Reminder cancelled.")
    _cleanup_conversation(context)
    await start(update, context)
    return ConversationHandler.END


# ── Repeatable reminder helpers ──


def _days_keyboard(selected: set) -> InlineKeyboardMarkup:
    kb = []
    for i in range(0, 7, 3):
        row = []
        for d in range(i, min(i + 3, 7)):
            checked = "✅ " if d in selected else ""
            row.append(InlineKeyboardButton(f"{checked}{DAY_NAMES[d]}", callback_data=f"repeat_day_{d}"))
        kb.append(row)
    kb.append([InlineKeyboardButton("✅ Done", callback_data="repeat_days_done")])
    return InlineKeyboardMarkup(kb)


async def repeat_toggle_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    d = int(q.data.split("_")[-1])
    if d in context.user_data["repeat_days"]:
        context.user_data["repeat_days"].discard(d)
    else:
        context.user_data["repeat_days"].add(d)
    await q.edit_message_text("📅 Select the days for this reminder:", reply_markup=_days_keyboard(context.user_data["repeat_days"]))
    return 4


async def repeat_days_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not context.user_data.get("repeat_days"):
        await q.answer("Select at least one day!", show_alert=True)
        return 4
    await q.edit_message_text("⏰ What time should this reminder repeat at?\nFormat: `HH:MM`\n\nExample: `08:00`")
    return 5


async def repeat_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and validate HH:MM for repeatable reminder."""
    time_str, err = validate_hhmm(update.message.text.strip())
    if err:
        await update.message.reply_text(err)
        return 5
    context.user_data["repeat_time"] = time_str
    await update.message.reply_text("✏️ Great! Now send me the reminder message:")
    return 6


async def repeat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the reminder message and show confirmation."""
    msg, err = validate_message(update.message.text)
    if err:
        await update.message.reply_text(err)
        return 6
    context.user_data["repeat_msg"] = msg

    days = context.user_data["repeat_days"]
    day_str = ", ".join(DAY_NAMES[d] for d in sorted(days))
    summary = (
        "📋 **Repeatable Reminder Summary**\n\n"
        f"📅 **Days:** {day_str}\n"
        f"⏰ **Time:** {context.user_data['repeat_time']}\n"
        f"💬 **Message:** {msg}\n\n"
        "Does everything look right?"
    )
    await update.message.reply_text(summary, reply_markup=_confirm_keyboard("repeat_confirm"))
    return 7


async def repeat_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirm and save the repeatable reminder to DB."""
    q = update.callback_query
    await q.answer()
    days_str = ",".join(str(d) for d in sorted(context.user_data["repeat_days"]))
    today = dt.date.today().isoformat()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO reminders (user_id, message, type, time_of_day, days_of_week, start_date, status) VALUES (?, ?, 'repeatable', ?, ?, ?, 'active')",
        (update.effective_user.id, context.user_data["repeat_msg"], context.user_data["repeat_time"], days_str, today)
    )
    conn.commit()
    scheduler.schedule_repeatable_reminder({
        "id": cur.lastrowid,
        "days_of_week": days_str,
        "time_of_day": context.user_data["repeat_time"],
    })

    _cleanup_conversation(context)

    await q.edit_message_text("✅ **Repeatable reminder set successfully!** 🎉")
    await start(update, context)
    return ConversationHandler.END


async def remind_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and validate the datetime for a one-time reminder."""
    parsed, err = validate_datetime(update.message.text.strip())
    if err:
        await update.message.reply_text(err)
        return 1
    context.user_data["remind_at"] = parsed.strftime("%Y-%m-%dT%H:%M:%S")
    await update.message.reply_text("✏️ Great! Now send me the reminder message:")
    return 2


async def remind_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the reminder message and show confirmation."""
    msg, err = validate_message(update.message.text)
    if err:
        await update.message.reply_text(err)
        return 2

    context.user_data["remind_msg"] = msg
    remind_at = context.user_data["remind_at"]

    summary = (
        "📋 **Reminder Summary**\n\n"
        f"📅 **Time:** {remind_at[:16].replace('T', ' ')}\n"
        f"💬 **Message:** {msg}\n\n"
        "Does everything look right?"
    )
    await update.message.reply_text(summary, reply_markup=_confirm_keyboard("remind_confirm"))
    return 3


async def remind_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirm and save the one-time reminder to DB."""
    query = update.callback_query
    await query.answer()

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO reminders (user_id, message, type, remind_at, status) VALUES (?, ?, 'one_time', ?, 'active')",
        (update.effective_user.id, context.user_data["remind_msg"], context.user_data["remind_at"])
    )
    conn.commit()
    scheduler.schedule_one_time_reminder({"id": cur.lastrowid, "remind_at": context.user_data["remind_at"]})

    # Clean up conversation state
    for key in ("remind_msg", "remind_at", "reminder_type"):
        context.user_data.pop(key, None)

    await query.edit_message_text("✅ **Reminder set successfully!** 🎉")
    await start(update, context)
    return ConversationHandler.END


async def remind_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel from a callback button (legacy path)."""
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("❌ Reminder cancelled.")
    _cleanup_conversation(context)
    await start(update, context)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel command during any step."""
    await update.message.reply_text("❌ Reminder cancelled.")
    _cleanup_conversation(context)
    await start(update, context)
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update: %s", context.error)


async def post_init(app: Application) -> None:
    """Runs once, inside the bot's event loop, before polling starts."""
    scheduler.init_scheduler(app.bot)


async def post_shutdown(app: Application) -> None:
    """Runs once, on bot shutdown."""
    scheduler.shutdown_scheduler()


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    # Conversation handler for set reminder
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(remind_type_one_time_entry, pattern="^remind_type_one_time$"),
            CallbackQueryHandler(remind_type_repeatable_entry, pattern="^remind_type_repeatable$"),
        ],
        states={
            1: [MessageHandler(filters.TEXT & ~filters.COMMAND, remind_datetime)],
            2: [MessageHandler(filters.TEXT & ~filters.COMMAND, remind_message)],
            3: [CallbackQueryHandler(remind_confirm, pattern="^remind_confirm$"),
                CallbackQueryHandler(remind_cancel_entry, pattern="^remind_cancel$")],
            4: [CallbackQueryHandler(repeat_toggle_day, pattern="^repeat_day_"),
                CallbackQueryHandler(repeat_days_done, pattern="^repeat_days_done$"),
                CallbackQueryHandler(remind_cancel_entry, pattern="^remind_cancel$")],
            5: [MessageHandler(filters.TEXT & ~filters.COMMAND, repeat_time)],
            6: [MessageHandler(filters.TEXT & ~filters.COMMAND, repeat_message)],
            7: [CallbackQueryHandler(repeat_confirm, pattern="^repeat_confirm$"),
                CallbackQueryHandler(remind_cancel_entry, pattern="^remind_cancel$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(remind_cancel_entry, pattern="^remind_cancel$"),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()