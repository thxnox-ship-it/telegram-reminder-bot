# Telegram Reminder Bot

Sends reminder messages to a Telegram chat at a chosen time (9 AM / 12 PM /
9 PM, Singapore time) on one of four schedules:

- **Date(s) of the month** — e.g. the 1st and 15th
- **Weekly** — a chosen day of the week
- **Monthly by weekday** — the first / second / second-last / last X-day
  (e.g. "first Monday of the month")
- **Quarterly** — every 3 months from a chosen start date

Reminders run for a set number of months (final message flagged) or
**indefinitely**. Every reminder message starts with a `🙌 REMINDER:` header.
Built with [python-telegram-bot](https://python-telegram-bot.org/).

## Commands

| Command   | What it does                                             |
|-----------|---------------------------------------------------------|
| `/start`  | Intro + how it works                                     |
| `/help`   | Same as `/start`                                         |
| `/setup`  | Create / edit / pause / delete reminders (inline menus)  |
| `/status` | List reminders with their status and **next fire date**  |

In a group, only admins can create or change reminders. In a 1:1 chat, anyone can.

## Configuration (environment variables)

| Var         | Required | Default                 | Notes                                             |
|-------------|----------|-------------------------|---------------------------------------------------|
| `BOT_TOKEN` | yes      | —                       | From [@BotFather](https://t.me/BotFather).         |
| `DATA_FILE` | no       | `/data/reminders.json`  | Where reminders are stored. **Must be on a persistent volume** (see below). |

## ⚠️ Persistence — attach a volume on Railway

Reminders are stored as JSON at `DATA_FILE`. Railway's container filesystem is
**ephemeral** — without a mounted volume, every redeploy or restart **wipes all
reminders**. To make them durable:

1. In your Railway service → **Variables**, confirm/keep `DATA_FILE=/data/reminders.json`.
2. Go to **Settings → Volumes → New Volume** and set the **mount path** to `/data`.
3. Redeploy.

On startup the bot logs the resolved path and warns if it looks ephemeral
(`/tmp` or a relative path), so check the logs after deploy.

## Reliability behavior

- **No double-sends.** Each reminder records `last_sent` (a date) and fires at
  most once per calendar day, so overlapping restarts or the catch-up job below
  never re-send.
- **Catch-up on boot.** If the bot is offline during a send slot (crash,
  redeploy, cold start), on startup it delivers any of *today's* earlier slots
  that were missed — once each.
- **End-of-month clamp.** Day 29/30/31 on a shorter month is sent on the last
  day of that month instead of being skipped (e.g. day 31 → 28 Feb). The same
  clamp applies to quarterly start days.

## Running locally

```bash
pip install -r requirements.txt
export BOT_TOKEN="123456:your-token"
export DATA_FILE="./data/reminders.json"   # local, non-ephemeral
python bot.py
```

## Tests

`test_schedule.py` imports the real functions from `bot.py` and drives the
actual `send_reminders()` / `catch_up()` jobs across simulated timelines
(faked clock, in-memory store, captured sends). It covers monthly firing, hour
filtering, end-of-month clamping, pausing, dedupe, catch-up, extending an ended
reminder, next-fire calculation, weekly and monthly-by-weekday schedules,
quarterly firing (incl. the Feb clamp), indefinite reminders, the message
header, and start-date parsing.

```bash
python3 test_schedule.py    # exits non-zero on failure
```

## Deploy (Railway)

Configured via `railway.json` (Nixpacks build, `python bot.py` as a worker,
restart on failure). Set `BOT_TOKEN`, attach the `/data` volume as above, and
deploy.
