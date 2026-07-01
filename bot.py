import calendar
import json
import logging
import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SGT = ZoneInfo("Asia/Singapore")
DATA_FILE = os.environ.get("DATA_FILE", "/data/reminders.json")
BOT_TOKEN = os.environ["BOT_TOKEN"]

CB_NEW = "setup:new"
CB_CHANGE = "setup:change"
CB_PAUSE = "setup:pause"
CB_CHANGE_DAYS = "setup:change_days"
CB_CHANGE_MSG = "setup:change_message"
CB_BACK_MENU = "setup:back_menu"
CB_BACK_DAYS = "setup:back_days"
CB_BACK_MONTHS = "setup:back_months"

AWAIT = "awaiting"
AWAIT_DAYS_NEW = "days_new"
AWAIT_MONTHS_NEW = "months_new"
AWAIT_MSG_NEW = "msg_new"
AWAIT_DAYS_CHANGE = "days_change"
AWAIT_MSG_CHANGE = "msg_change"


def load_config() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}


def save_config(config: dict) -> None:
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_chat_cfg(chat_id: int) -> dict:
    return load_config().get(
        str(chat_id),
        {"days": [], "message": "", "paused": False, "end_date": None},
    )


def update_chat_cfg(chat_id: int, **kwargs) -> None:
    config = load_config()
    key = str(chat_id)
    cfg = config.get(key, {"days": [], "message": "", "paused": False, "end_date": None})
    cfg.update(kwargs)
    config[key] = cfg
    save_config(config)


def add_months(dt: date, months: int) -> date:
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def next_reminder_date(days: list, after: date):
    for d in sorted(days):
        if d > after.day:
            max_day = calendar.monthrange(after.year, after.month)[1]
            if d <= max_day:
                return after.replace(day=d)
    nm = add_months(after, 1)
    for d in sorted(days):
        max_day = calendar.monthrange(nm.year, nm.month)[1]
        if d <= max_day:
            return nm.replace(day=d)
    return None


async def is_admin(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        return True
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in ("administrator", "creator")


async def require_admin(update, context):
    if not await is_admin(update, context):
        await update.message.reply_text("Only group admins can use this command.")
        return False
    return True


def _menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    cfg = get_chat_cfg(chat_id)
    pause_label = "Resume reminders" if cfg.get("paused") else "Pause all reminders"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Create new alert", callback_data=CB_NEW)],
        [InlineKeyboardButton("Change reminders", callback_data=CB_CHANGE)],
        [InlineKeyboardButton(pause_label, callback_data=CB_PAUSE)],
    ])


async def setup_start(update, context):
    if not await require_admin(update, context):
        return
    context.chat_data.pop(AWAIT, None)
    await update.message.reply_text(
        "What would you like to do?",
        reply_markup=_menu_keyboard(update.effective_chat.id),
    )


async def setup_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not await is_admin(update, context):
        await query.answer("Only admins can do this.", show_alert=True)
        return
    chat_id = query.message.chat.id
    data = query.data

    if data == CB_NEW:
        context.chat_data[AWAIT] = AWAIT_DAYS_NEW
        await query.edit_message_text(
            "Which days of the month should I send reminders?\nReply with day numbers separated by spaces, e.g. 1 15 28",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=CB_BACK_MENU)]]),
        )
    elif data == CB_CHANGE:
        cfg = get_chat_cfg(chat_id)
        days_str = ", ".join(str(d) for d in sorted(cfg.get("days", []))) or "none"
        msg = cfg.get("message", "(not set)")
        end = cfg.get("end_date")
        end_str = datetime.fromisoformat(end).strftime("%d %b %Y") if end else "no end date"
        await query.edit_message_text(
            f"Current setup:\nDays: {days_str}\nMessage: {msg}\nRuns until: {end_str}\n\nWhat would you like to change?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Change days", callback_data=CB_CHANGE_DAYS)],
                [InlineKeyboardButton("Change message", callback_data=CB_CHANGE_MSG)],
                [InlineKeyboardButton("Back", callback_data=CB_BACK_MENU)],
            ]),
        )
    elif data == CB_CHANGE_DAYS:
        context.chat_data[AWAIT] = AWAIT_DAYS_CHANGE
        await query.edit_message_text(
            "Reply with the new reminder days, e.g. 1 15 28",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=CB_CHANGE)]]),
        )
    elif data == CB_CHANGE_MSG:
        context.chat_data[AWAIT] = AWAIT_MSG_CHANGE
        await query.edit_message_text(
            "Reply with the new reminder message.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=CB_CHANGE)]]),
        )
    elif data == CB_PAUSE:
        cfg = get_chat_cfg(chat_id)
        if cfg.get("paused"):
            update_chat_cfg(chat_id, paused=False)
            await query.edit_message_text("Reminders resumed!")
        else:
            update_chat_cfg(chat_id, paused=True)
            await query.edit_message_text("All reminders paused. Use /setup to resume.")
    elif data == CB_BACK_MENU:
        context.chat_data.pop(AWAIT, None)
        await query.edit_message_text(
            "What would you like to do?",
            reply_markup=_menu_keyboard(chat_id),
        )
    elif data == CB_BACK_DAYS:
        context.chat_data[AWAIT] = AWAIT_DAYS_NEW
        await query.edit_message_text(
            "Which days of the month should I send reminders?\nReply with day numbers separated by spaces, e.g. 1 15 28",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=CB_BACK_MENU)]]),
        )
    elif data == CB_BACK_MONTHS:
        context.chat_data[AWAIT] = AWAIT_MONTHS_NEW
        days = context.chat_data.get("pending_days", [])
        await query.edit_message_text(
            f"Days noted: {', '.join(str(d) for d in days)}\n\nHow many months should this reminder persist?\nReply with a number, e.g. 3",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=CB_BACK_DAYS)]]),
        )


async def handle_reply(update, context):
    awaiting = context.chat_data.get(AWAIT)
    if not awaiting:
        return
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if awaiting == AWAIT_DAYS_NEW:
        try:
            days = sorted({int(d) for d in text.split()})
            if not days or not all(1 <= d <= 31 for d in days):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send valid day numbers between 1 and 31.")
            return
        context.chat_data["pending_days"] = days
        context.chat_data[AWAIT] = AWAIT_MONTHS_NEW
        await update.message.reply_text(
            f"Days noted: {', '.join(str(d) for d in days)}\n\nHow many months should this reminder persist?\nReply with a number, e.g. 3",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=CB_BACK_DAYS)]]),
        )
    elif awaiting == AWAIT_MONTHS_NEW:
        try:
            months = int(text)
            if months < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send a valid number of months, e.g. 3")
            return
        end_date = add_months(datetime.now(SGT).date(), months)
        context.chat_data["pending_months"] = months
        context.chat_data["pending_end_date"] = end_date.isoformat()
        context.chat_data[AWAIT] = AWAIT_MSG_NEW
        await update.message.reply_text(
            f"Got it, {months} month(s) running until {end_date.strftime('%d %b %Y')}.\n\nWhat message should I send to this group on those days?",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=CB_BACK_MONTHS)]]),
        )
    elif awaiting == AWAIT_MSG_NEW:
        days = context.chat_data.pop("pending_days", [])
        context.chat_data.pop("pending_months", None)
        end_date_iso = context.chat_data.pop("pending_end_date", None)
        context.chat_data.pop(AWAIT, None)
        update_chat_cfg(chat_id, days=days, message=text, paused=False, end_date=end_date_iso)
        end_str = datetime.fromisoformat(end_date_iso).strftime("%d %b %Y") if end_date_iso else "no end date"
        await update.message.reply_text(
            f"All set! I will remind this group on day(s) {', '.join(str(d) for d in days)} of each month at 12:00 PM SGT until {end_str}.\n\nMessage:\n{text}"
        )
    elif awaiting == AWAIT_DAYS_CHANGE:
        try:
            days = sorted({int(d) for d in text.split()})
            if not days or not all(1 <= d <= 31 for d in days):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send valid day numbers between 1 and 31.")
            return
        context.chat_data.pop(AWAIT, None)
        update_chat_cfg(chat_id, days=days)
        await update.message.reply_text(f"Reminder days updated to: {', '.join(str(d) for d in days)}")
    elif awaiting == AWAIT_MSG_CHANGE:
        context.chat_data.pop(AWAIT, None)
        update_chat_cfg(chat_id, message=text)
        await update.message.reply_text(f"Reminder message updated to:\n\n{text}")


async def cancelday(update, context):
    if not await require_admin(update, context):
        return
    cfg = get_chat_cfg(update.effective_chat.id)
    days = cfg.get("days", [])
    if not days:
        await update.message.reply_text("No reminder days configured. Use /setup first.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Cancel Day {d}", callback_data=f"cancelday:{d}")]
        for d in sorted(days)
    ])
    await update.message.reply_text("Which day would you like to remove?", reply_markup=keyboard)


async def cancelday_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not await is_admin(update, context):
        await query.answer("Only admins can do this.", show_alert=True)
        return
    day = int(query.data.split(":")[1])
    chat_id = query.message.chat.id
    cfg = get_chat_cfg(chat_id)
    days = cfg.get("days", [])
    if day in days:
        days.remove(day)
        update_chat_cfg(chat_id, days=days)
        remaining = ", ".join(str(d) for d in sorted(days)) if days else "none"
        await query.edit_message_text(f"Day {day} removed. Remaining days: {remaining}")
    else:
        await query.edit_message_text(f"Day {day} was already removed.")


async def status(update, context):
    cfg = get_chat_cfg(update.effective_chat.id)
    days = cfg.get("days", [])
    message = cfg.get("message", "")
    paused = cfg.get("paused", False)
    end_date = cfg.get("end_date")
    if not days and not message:
        await update.message.reply_text("No reminders configured yet. Use /setup to get started.")
        return
    days_str = ", ".join(str(d) for d in sorted(days)) if days else "none"
    end_str = datetime.fromisoformat(end_date).strftime("%d %b %Y") if end_date else "no end date"
    status_str = "Paused" if paused else "Active"
    await update.message.reply_text(
        f"Reminder days: {days_str}\nTime: 12:00 PM SGT\nMessage: {message or '(not set)'}\nRuns until: {end_str}\nStatus: {status_str}"
    )


async def send_reminders(context):
    today = datetime.now(SGT).date()
    config = load_config()
    for chat_id_str, cfg in list(config.items()):
        if cfg.get("paused"):
            continue
        days = cfg.get("days", [])
        message = cfg.get("message", "")
        end_date_str = cfg.get("end_date")
        if not days or not message:
            continue
        if end_date_str:
            end_date = date.fromisoformat(end_date_str)
            if today > end_date:
                continue
        if today.day not in days:
            continue
        is_final = False
        if end_date_str:
            end_date = date.fromisoformat(end_date_str)
            nrd = next_reminder_date(days, today)
            if nrd is None or nrd > end_date:
                is_final = True
        send_text = message
        if is_final:
            send_text += "\n\nThis is the final reminder for this alert. Use /setup to continue or create a new one."
        try:
            await context.bot.send_message(int(chat_id_str), send_text)
            logger.info("Sent reminder to chat %s", chat_id_str)
            if is_final:
                cfg["paused"] = True
                config[chat_id_str] = cfg
                save_config(config)
        except Exception as exc:
            logger.error("Failed to send to %s: %s", chat_id_str, exc)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("setup", setup_start))
    app.add_handler(CommandHandler("cancelday", cancelday))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(setup_callback, pattern=r"^setup:"))
    app.add_handler(CallbackQueryHandler(cancelday_callback, pattern=r"^cancelday:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply))
    app.job_queue.run_daily(send_reminders, time=time(hour=12, minute=0, tzinfo=SGT), name="daily_reminder")
    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
