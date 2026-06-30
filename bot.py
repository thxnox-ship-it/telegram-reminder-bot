import json
import logging
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
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

ASKING_DAYS, ASKING_MESSAGE, UPDATING_MESSAGE = range(3)


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
    return load_config().get(str(chat_id), {"days": [], "message": ""})


def update_chat_cfg(chat_id: int, **kwargs) -> None:
    config = load_config()
    key = str(chat_id)
    cfg = config.get(key, {"days": [], "message": ""})
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
        return ConversationHandler.END
    await update.message.reply_text(
        "Let us set up your reminders!\n\nWhich days of the month should I send reminders?\nReply with day numbers separated by spaces, e.g. 1 15 28",
        parse_mode="Markdown",
    )
    return ASKING_DAYS


async def received_days(update, context):
    text = update.message.text.strip()
    try:
        days = sorted({int(d) for d in text.split()})
        if not days or not all(1 <= d <= 31 for d in days):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send valid day numbers between 1 and 31.")
        return ASKING_DAYS
    context.user_data["setup_days"] = days
    await update.message.reply_text(
        f"Days noted: {', '.join(str(d) for d in days)}\n\nNow, what message should I send?",
    )
    return ASKING_MESSAGE


async def received_message(update, context):
    message = update.message.text.strip()
    days = context.user_data.get("setup_days", [])
    chat_id = update.effective_chat.id
    update_chat_cfg(chat_id, days=days, message=message)
    await update.message.reply_text(
        f"All set! Reminders on day(s) {', '.join(str(d) for d in days)} at 12:00 PM SGT.",
    )
    return ConversationHandler.END


async def cancel_conversation(update, context):
    await update.message.reply_text("Setup cancelled.")
    return ConversationHandler.END


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


async def setmessage_start(update, context):
    if not await require_admin(update, context):
        return ConversationHandler.END
    await update.message.reply_text("What should the new reminder message be?")
    return UPDATING_MESSAGE


async def setmessage_done(update, context):
    message = update.message.text.strip()
    update_chat_cfg(update.effective_chat.id, message=message)
    await update.message.reply_text(f"Reminder message updated to: {message}")
    return ConversationHandler.END


async def status(update, context):
    cfg = get_chat_cfg(update.effective_chat.id)
    days = cfg.get("days", [])
    message = cfg.get("message", "")
    if not days and not message:
        await update.message.reply_text("No reminders configured. Use /setup to start.")
        return
    days_str = ", ".join(str(d) for d in sorted(days)) if days else "none"
    await update.message.reply_text(
        f"Reminder days: {days_str}\nTime: 12:00 PM SGT\nMessage: {message or '(not set)'}"
    )


async def send_reminders(context):
    today = datetime.now(SGT).day
    config = load_config()
    for chat_id_str, cfg in config.items():
        if today in cfg.get("days", []) and cfg.get("message"):
            try:
                await context.bot.send_message(int(chat_id_str), cfg["message"])
            except Exception as exc:
                logger.error("Failed to send reminder to %s: %s", chat_id_str, exc)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    setup_conv = ConversationHandler(
        entry_points=[CommandHandler("setup", setup_start)],
        states={
            ASKING_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_days)],
            ASKING_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_message)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    setmessage_conv = ConversationHandler(
        entry_points=[CommandHandler("setmessage", setmessage_start)],
        states={
            UPDATING_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, setmessage_done)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    app.add_handler(setup_conv)
    app.add_handler(setmessage_conv)
    app.add_handler(CommandHandler("setdays", setdays))
    app.add_handler(CommandHandler("status", status))
    app.job_queue.run_daily(send_reminders, time=time(hour=12, minute=0, tzinfo=SGT), name="daily_reminder")
    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
