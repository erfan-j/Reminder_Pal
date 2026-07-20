# Reminder_pal

A Telegram bot for scheduling one-time and repeatable reminders. Built with `python-telegram-bot` v22, APScheduler, and SQLite.

## Features

- **⏰ Set Reminders** — one-time (date + time) or repeatable (selected weekdays + time)
- **📋 View Reminders** — paginated list, prev/next navigation
- **🗑 Delete Reminders** — detail view → confirm → hard delete
- **⏲️ Automatic Delivery** — reminders fire via APScheduler, logged to DB

## Setup

```bash
git clone git@github.com:erfan-j/Reminder_Pal.git
cd Reminder_Pal
uv venv && source .venv/bin/activate
uv sync
cp .env.example .env  # add your BOT_TOKEN
uv run python main.py
```

`BOT_TOKEN` is read from `.env` or the environment. See `.env.example`.

## Tech

`uv` · `python-telegram-bot` · `APScheduler` · `sqlite3`
