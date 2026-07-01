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

# Top-level callback data
CB_NEW        = "setup:new"
CB_CHANGE     = "setup:change"
CB_PAUSE_ALL  = "setup:pause_all"
CB_BACK_MENU  = "setup:back_menu"

# Chat state keys
AWAIT = "awaiting"
AWAIT_DAYS_NEW    = "days_new"
AWAIT_MONTHS_NEW  = "months_new"
AWAIT_MSG_NEW     = "msg_new"
AWAIT_DAYS_EDIT   = "days_edit"
AWAIT_MSG_EDIT    = "msg_edit"


# ---------------------------------------------------------------------------
# Persistence  (data shape: {chat_id: {"reminders": [...]}})
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}


def save_config(config: dict) -> None:
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_reminders(chat_id: int) -> list:
    return load_config().get(str(chat_id), {}).get("reminders", [])


def _save_reminders(chat_id: int, reminders: list) -> None:
    config = load_config()
    key = str(chat_id)
    cfg = config.get(key, {})
    cfg["reminders"] = reminders
    config[key] = cfg
    save_config(config)


def add_reminder(chat_id: int, days: list, message: str, end_date: str) -> int:
    reminders = get_reminders(chat_id)
    new_id = max((r["id"] for r in reminders), default=0) + 1
    reminders.append({
        "id": new_id,
        "days": days,
        "message": message,
        "paused": False,
        "end_date": end_date,
    })
    _save_reminders(chat_id, reminders)
    return new_id


def update_reminder(chat_id: int, reminder_id: int, **kwargs) -> None:
    reminders = get_reminders(chat_id)
    for r in reminders:
        if r["id"] == reminder_id:
            r.update(kwargs)
    _save_reminders(chat_id, reminders)


def delete_reminder(chat_id: int, reminder_id: int) -> None:
    reminders = [r for r in get_reminders(chat_id) if r["id"] != reminder_id]
    _save_reminders(chat_id, reminders)


def get_reminder_by_id(chat_id: int, reminder_id: int):
    return next((r for r in get_reminders(chat_id) if r["id"] == reminder_id), None)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _reminder_label(r: dict) -> str:
    days_str = " & ".join(str(d) for d in sorted(r["days"]))
    msg = r["message"]
    preview = (msg[:22] + "\u2026") if len(msg) > 22 else msg
    prefix = "\u23f8 " if r.get("paused") else ""
    return f"{prefix}Day {days_str}  \u00b7  {preview}"


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
    reminders = get_reminders(chat_id)
    all_paused = bool(reminders) and all(r.get("paused") for r in reminders)
    pause_label = "\u25b6\ufe0f Resume all" if all_paused else "\u23f8 Pause all"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2795 Create new alert", callback_data=CB_NEW)],
        [InlineKeyboardButton("\u270f\ufe0f Change reminders", callback_data=CB_CHANGE)],
        [InlineKeyboardButton(pause_label, callback_data=CB_PAUSE_ALL)],
    ])


def _reminder_list_keyboard(chat_id: int):
    reminders = get_reminders(chat_id)
    if not reminders:
        return None, "No reminders set up yet. Tap 'Create new alert' to add one."
    buttons = [
        [InlineKeyboardButton(_reminder_label(r), callback_data=f"rem:{r['id']}:view")]
        for r in reminders
    ]
    buttons.append([InlineKeyboardButton("\u2190 Back", callback_data=CB_BACK_MENU)])
    return InlineKeyboardMarkup(buttons), "Select a reminder to edit:"


def _reminder_detail(r: dict):
    rid = r["id"]
    days_str = ", ".join(str(d) for d in sorted(r["days"]))
    end = r.get("end_date")
    end_str = datetime.fromisoformat(end).strftime("%d %b %Y") if end else "no end date"
    status_str = "\u23f8 Paused" if r.get("paused") else "\u25b6\ufe0f Active"
    toggle_label = "\u25b6\ufe0f Resume" if r.get("paused") else "\u23f8 Pause"
    text = (
        f"Days: {days_str}\n"
        f"Message: {r['message']}\n"
        f"Until: {end_str}\n"
        f"Status: {status_str}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f4c5 Change days",    callback_data=f"rem:{rid}:change_days")],
        [InlineKeyboardButton("\U0001f4ac Change message", callback_data=f"rem:{rid}:change_msg")],
        [InlineKeyboardButton(toggle_label,                callback_data=f"rem:{rid}:toggle")],
        [InlineKeyboardButton("\U0001f5d1 Delete",         callback_data=f"rem:{rid}:delete")],
        [InlineKeyboardButton("\u2190 Back",               callback_data=CB_CHANGE)],
    ])
    return text, keyboard


# ---------------------------------------------------------------------------
# /setup
# ---------------------------------------------------------------------------

async def setup_start(update, context):
    if not await require_admin(update, context):
        return
    context.chat_data.pop(AWAIT, None)
    context.chat_data.pop("editing_id", None)
    await update.message.reply_text(
        "What would you like to do?",
        reply_markup=_menu_keyboard(update.effective_chat.id),
    )


# ---------------------------------------------------------------------------
# Top-level callback handler (setup:*)
# ---------------------------------------------------------------------------

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
            "Which days of the month should I send reminders?\nReply with day numbers separated by spaces \u2014 e.g. 1 15 28",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("\u2190 Back", callback_data=CB_BACK_MENU)]]
            ),
        )

    elif data == CB_CHANGE:
        keyboard, text = _reminder_list_keyboard(chat_id)
        back = InlineKeyboardMarkup([[InlineKeyboardButton("\u2190 Back", callback_data=CB_BACK_MENU)]])
        await query.edit_message_text(text, reply_markup=keyboard or back)

    elif data == CB_PAUSE_ALL:
        reminders = get_reminders(chat_id)
        if not reminders:
            await query.edit_message_text(
                "No reminders to pause. Use 'Create new alert' first.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2190 Back", callback_data=CB_BACK_MENU)]]),
            )
            return
        all_paused = all(r.get("paused") for r in reminders)
        new_state = not all_paused
        for r in reminders:
            r["paused"] = new_state
        _save_reminders(chat_id, reminders)
        word = "paused" if new_state else "resumed"
        await query.edit_message_text(f"All reminders {word}. Use /setup to make changes.")

    elif data == CB_BACK_MENU:
        context.chat_data.pop(AWAIT, None)
        context.chat_data.pop("editing_id", None)
        await query.edit_message_text(
            "What would you like to do?",
            reply_markup=_menu_keyboard(chat_id),
        )

    elif data == "setup:back_days":
        context.chat_data[AWAIT] = AWAIT_DAYS_NEW
        await query.edit_message_text(
            "Which days of the month should I send reminders?\nReply with day numbers separated by spaces \u2014 e.g. 1 15 28",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("\u2190 Back", callback_data=CB_BACK_MENU)]]
            ),
        )

    elif data == "setup:back_months":
        context.chat_data[AWAIT] = AWAIT_MONTHS_NEW
        days = context.chat_data.get("pending_days", [])
        await query.edit_message_text(
            f"Days noted: {', '.join(str(d) for d in days)}\n\nHow many months should this reminder persist?\nReply with a number \u2014 e.g. 3",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("\u2190 Back", callback_data="setup:back_days")]]
            ),
        )


# ---------------------------------------------------------------------------
# Per-reminder callback handler (rem:{id}:{action})
# ---------------------------------------------------------------------------

async def reminder_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not await is_admin(update, context):
        await query.answer("Only admins can do this.", show_alert=True)
        return
    chat_id = query.message.chat.id

    _, rid_str, action = query.data.split(":", 2)
    rid = int(rid_str)
    r = get_reminder_by_id(chat_id, rid)
    if r is None:
        await query.edit_message_text("This reminder no longer exists.")
        return

    if action == "view":
        text, keyboard = _reminder_detail(r)
        await query.edit_message_text(text, reply_markup=keyboard)

    elif action == "change_days":
        context.chat_data[AWAIT] = AWAIT_DAYS_EDIT
        context.chat_data["editing_id"] = rid
        days_str = ", ".join(str(d) for d in sorted(r["days"]))
        await query.edit_message_text(
            f"Current days: {days_str}\n\nReply with the new days \u2014 e.g. 1 15 28",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("\u2190 Back", callback_data=f"rem:{rid}:view")]]
            ),
        )

    elif action == "change_msg":
        context.chat_data[AWAIT] = AWAIT_MSG_EDIT
        context.chat_data["editing_id"] = rid
        await query.edit_message_text(
            f"Current message:\n{r['message']}\n\nReply with the new message.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("\u2190 Back", callback_data=f"rem:{rid}:view")]]
            ),
        )

    elif action == "toggle":
        new_paused = not r.get("paused", False)
        update_reminder(chat_id, rid, paused=new_paused)
        r["paused"] = new_paused
        text, keyboard = _reminder_detail(r)
        await query.edit_message_text(text, reply_markup=keyboard)

    elif action == "delete":
        days_str = ", ".join(str(d) for d in sorted(r["days"]))
        preview = (r["message"][:30] + "\u2026") if len(r["message"]) > 30 else r["message"]
        await query.edit_message_text(
            f"Delete this reminder?\n\nDays: {days_str}\nMessage: {preview}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f5d1 Yes, delete", callback_data=f"rem:{rid}:confirm_delete")],
                [InlineKeyboardButton("\u2190 Keep it",        callback_data=f"rem:{rid}:view")],
            ]),
        )

    elif action == "confirm_delete":
        delete_reminder(chat_id, rid)
        keyboard, text = _reminder_list_keyboard(chat_id)
        back = InlineKeyboardMarkup([[InlineKeyboardButton("\u2190 Back", callback_data=CB_BACK_MENU)]])
        await query.edit_message_text(
            "Reminder deleted.\n\n" + text,
            reply_markup=keyboard or back,
        )


# ---------------------------------------------------------------------------
# Text reply handler
# ---------------------------------------------------------------------------

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
            f"Days noted: {', '.join(str(d) for d in days)}\n\nHow many months should this reminder persist?\nReply with a number \u2014 e.g. 3",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("\u2190 Back", callback_data="setup:back_days")]]
            ),
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
            f"Got it \u2014 {months} month(s), running until {end_date.strftime('%d %b %Y')}.\n\nWhat message should I send to this group on those days?",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("\u2190 Back", callback_data="setup:back_months")]]
            ),
        )

    elif awaiting == AWAIT_MSG_NEW:
        days = context.chat_data.pop("pending_days", [])
        context.chat_data.pop("pending_months", None)
        end_date_iso = context.chat_data.pop("pending_end_date", None)
        context.chat_data.pop(AWAIT, None)
        add_reminder(chat_id, days, text, end_date_iso)
        end_str = (
            datetime.fromisoformat(end_date_iso).strftime("%d %b %Y")
            if end_date_iso else "no end date"
        )
        await update.message.reply_text(
            f"Reminder added! Day(s) {', '.join(str(d) for d in days)} "
            f"at 12:00 PM SGT until {end_str}.\n\nMessage:\n{text}"
        )

    elif awaiting == AWAIT_DAYS_EDIT:
        try:
            days = sorted({int(d) for d in text.split()})
            if not days or not all(1 <= d <= 31 for d in days):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send valid day numbers between 1 and 31.")
            return
        rid = context.chat_data.pop("editing_id", None)
        context.chat_data.pop(AWAIT, None)
        if rid is not None:
            update_reminder(chat_id, rid, days=days)
        await update.message.reply_text(f"Days updated to: {', '.join(str(d) for d in days)}")

    elif awaiting == AWAIT_MSG_EDIT:
        rid = context.chat_data.pop("editing_id", None)
        context.chat_data.pop(AWAIT, None)
        if rid is not None:
            update_reminder(chat_id, rid, message=text)
        await update.message.reply_text(f"Message updated to:\n\n{text}")


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

async def status(update, context):
    reminders = get_reminders(update.effective_chat.id)
    if not reminders:
        await update.message.reply_text("No reminders configured. Use /setup to create one.")
        return
    lines = ["Active reminders:\n"]
    for i, r in enumerate(reminders, 1):
        days_str = ", ".join(str(d) for d in sorted(r["days"]))
        end = r.get("end_date")
        end_str = datetime.fromisoformat(end).strftime("%d %b %Y") if end else "no end date"
        status_str = "\u23f8 Paused" if r.get("paused") else "\u25b6\ufe0f Active"
        lines.append(
            f"{i}. {status_str}\n"
            f"   Days: {days_str} \u00b7 Until: {end_str}\n"
            f"   Message: {r['message']}"
        )
    await update.message.reply_text("\n\n".join(lines))


# ---------------------------------------------------------------------------
# Scheduled job
# ---------------------------------------------------------------------------

async def send_reminders(context):
    today = datetime.now(SGT).date()
    config = load_config()

    for chat_id_str, chat_cfg in list(config.items()):
        reminders = chat_cfg.get("reminders", [])
        changed = False
        for r in reminders:
            if r.get("paused"):
                continue
            days = r.get("days", [])
            message = r.get("message", "")
            end_date_str = r.get("end_date")
            if not days or not message:
                continue
            if end_date_str and today > date.fromisoformat(end_date_str):
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
                send_text += (
                    "\n\nThis is the final reminder for this alert. "
                    "Use /setup to continue or create a new one."
                )
            try:
                await context.bot.send_message(int(chat_id_str), send_text)
                logger.info("Sent reminder %d to chat %s", r["id"], chat_id_str)
                if is_final:
                    r["paused"] = True
                    changed = True
            except Exception as exc:
                logger.error("Failed to send reminder %d to %s: %s", r["id"], chat_id_str, exc)
        if changed:
            config[chat_id_str] = chat_cfg
            save_config(config)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("setup", setup_start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(setup_callback, pattern=r"^setup:"))
    app.add_handler(CallbackQueryHandler(reminder_callback, pattern=r"^rem:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply))
    app.job_queue.run_daily(
        send_reminders,
        time=time(hour=12, minute=0, tzinfo=SGT),
        name="daily_reminder",
    )
    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
