import json
import logging
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
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

STATE_AWAITING = "awaiting"
AWAIT_DAYS_NEW = "days_new"
AWAIT_DAYS_CHANGE = "days_change"
AWAIT_MSG_NEW = "msg_new"
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
    return load_config().get(str(chat_id), {"days": [], "message": "", "paused": False})


def update_chat_cfg(chat_id: int, **kwargs) -> None:
    config = load_config()
    key = str(chat_id)
    cfg = config.get(key, {"days": [], "message": "", "paused": False})
    cfg.update(kwargs)
    config[key] = cfg
    save_config(config)


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


async def setup_start(update, context):
    if not await require_admin(update, context):
        return
    cfg = get_chat_cfg(update.effective_chat.id)
    paused = cfg.get("paused", False)
    pause_label = "Resume reminders" if paused else "Pause all reminders"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Create new alert", callback_data=CB_NEW)],
        [InlineKeyboardButton("Change reminders", callback_data=CB_CHANGE)],
        [InlineKeyboardButton(pause_label, callback_data=CB_PAUSE)],
    ])
    await update.message.reply_text("What would you like to do?", reply_markup=keyboard)


async def setup_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not await is_admin(update, context):
        await query.answer("Only admins can do this.", show_alert=True)
        return
    chat_id = query.message.chat.id
    if query.data == CB_NEW:
        context.user_data[STATE_AWAITING] = AWAIT_DAYS_NEW
        await query.edit_message_text(
            "Which days of the month should I send reminders?\nReply with day numbers separated by spaces, e.g. 1 15 28"
        )
    elif query.data == CB_CHANGE:
        cfg = get_chat_cfg(chat_id)
        days_str = ", ".join(str(d) for d in sorted(cfg.get("days", []))) or "none"
        msg = cfg.get("message", "(not set)")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Change days", callback_data=CB_CHANGE_DAYS)],
            [InlineKeyboardButton("Change message", callback_data=CB_CHANGE_MSG)],
        ])
        await query.edit_message_text(
            f"Current setup:\nDays: {days_str}\nMessage: {msg}\n\nWhat would you like to change?",
            reply_markup=keyboard,
        )
    elif query.data == CB_CHANGE_DAYS:
        context.user_data[STATE_AWAITING] = AWAIT_DAYS_CHANGE
        await query.edit_message_text("Reply with the new reminder days, e.g. 1 15 28")
    elif query.data == CB_CHANGE_MSG:
        context.user_data[STATE_AWAITING] = AWAIT_MSG_CHANGE
        await query.edit_message_text("Reply with the new reminder message.")
    elif query.data == CB_PAUSE:
        cfg = get_chat_cfg(chat_id)
        if cfg.get("paused"):
            update_chat_cfg(chat_id, paused=False)
            await query.edit_message_text("Reminders resumed!")
        else:
            update_chat_cfg(chat_id, paused=True)
            await query.edit_message_text("All reminders paused. Use /setup to resume.")


async def handle_reply(update, context):
    awaiting = context.user_data.get(STATE_AWAITING)
    if not awaiting:
        return
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    if awaiting in (AWAIT_DAYS_NEW, AWAIT_DAYS_CHANGE):
        try:
            days = sorted({int(d) for d in text.split()})
            if not days or not all(1 <= d <= 31 for d in days):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send valid day numbers between 1 and 31.")
            return
        if awaiting == AWAIT_DAYS_NEW:
            context.user_data["pending_days"] = days
            context.user_data[STATE_AWAITING] = AWAIT_MSG_NEW
            await update.message.reply_text(
                f"Days noted: {', '.join(str(d) for d in days)}\n\nNow, what message should I send on those days?"
            )
        else:
            update_chat_cfg(chat_id, days=days)
            context.user_data.pop(STATE_AWAITING, None)
            await update.message.reply_text(f"Reminder days updated to: {', '.join(str(d) for d in days)}")
    elif awaiting == AWAIT_MSG_NEW:
        days = context.user_data.pop("pending_days", [])
        context.user_data.pop(STATE_AWAITING, None)
        update_chat_cfg(chat_id, days=days, message=text, paused=False)
        await update.message.reply_text(
            f"All set! Reminders on day(s) {', '.join(str(d) for d in days)} at 12:00 PM SGT with:\n\n{text}"
        )
    elif awaiting == AWAIT_MSG_CHANGE:
        context.user_data.pop(STATE_AWAITING, None)
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
    await update.message.reply_text("Which day would you like to cancel?", reply_markup=keyboard)


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
    if not days and not message:
        await update.message.reply_text("No reminders configured yet. Use /setup to get started.")
        return
    days_str = ", ".join(str(d) for d in sorted(days)) if days else "none"
    status_str = "Paused" if paused else "Active"
    await update.message.reply_text(
        f"Reminder days: {days_str}\nTime: 12:00 PM SGT\nMessage: {message or '(not set)'}\nStatus: {status_str}"
    )


async def setdays(update, context):
    if not await require_admin(update, context):
        return
    try:
        days = sorted({int(d) for d in context.args})
        if not days or not all(1 <= d <= 31 for d in days):
            raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text("Usage: /setdays 1 15 28")
        return
    update_chat_cfg(update.effective_chat.id, days=days)
    await update.message.reply_text(f"Reminder days updated: {', '.join(str(d) for d in days)}")


async def send_reminders(context):
    today = datetime.now(SGT).day
    config = load_config()
    for chat_id_str, cfg in config.items():
        if cfg.get("paused"):
            continue
        if today in cfg.get("days", []) and cfg.get("message"):
            try:
                await context.bot.send_message(int(chat_id_str), cfg["message"])
                logger.info("Sent reminder to chat %s", chat_id_str)
            except Exception as exc:
                logger.error("Failed to send reminder to %s: %s", chat_id_str, exc)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("setup", setup_start))
    app.add_handler(CommandHandler("cancelday", cancelday))
    app.add_handler(CommandHandler("setdays", setdays))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(setup_callback, pattern=r"^setup:"))
    app.add_handler(CallbackQueryHandler(cancelday_callback, pattern=r"^cancelday:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply))
    app.job_queue.run_daily(send_reminders, time=time(hour=12, minute=0, tzinfo=SGT), name="daily_reminder")
    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
