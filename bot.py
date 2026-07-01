import calendar
import json
import logging
import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
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
AWAIT_DAYS_NEW  = "days_new"
AWAIT_MSG_NEW   = "msg_new"
AWAIT_DAYS_EDIT = "days_edit"
AWAIT_MSG_EDIT  = "msg_edit"

HOUR_LABELS = {9: "9:00 AM", 12: "12:00 PM", 21: "9:00 PM"}
SEND_HOURS = (9, 12, 21)

HELP_TEXT = (
    "👋 *Monthly reminder bot*\n\n"
    "I send a message to this chat on the days of the month you pick, at a "
    "time you choose (9 AM, 12 PM or 9 PM, Singapore time).\n\n"
    "*Commands*\n"
    "• /setup — create or manage reminders\n"
    "• /status — see your active reminders and when each fires next\n"
    "• /help — show this message\n\n"
    "In a group, only admins can create or change reminders."
)


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


def add_reminder(chat_id: int, days: list, hour: int, message: str, end_date: str) -> int:
    reminders = get_reminders(chat_id)
    new_id = max((r["id"] for r in reminders), default=0) + 1
    reminders.append({
        "id": new_id,
        "days": days,
        "hour": hour,
        "message": message,
        "paused": False,
        "end_date": end_date,
        "last_sent": None,
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


def effective_days(days: list, year: int, month: int) -> set:
    """Map each target day to the actual day it fires in a given month.

    Days beyond the month's length (e.g. 31 in February) are clamped to the
    last day of that month so the reminder is never skipped.
    """
    last = calendar.monthrange(year, month)[1]
    return {min(d, last) for d in days}


def day_fires_today(days: list, today: date) -> bool:
    return today.day in effective_days(days, today.year, today.month)


def next_reminder_date(days: list, after: date):
    eff = effective_days(days, after.year, after.month)
    for d in sorted(eff):
        if d > after.day:
            return after.replace(day=d)
    nm = add_months(after, 1)
    nm_eff = effective_days(days, nm.year, nm.month)
    if nm_eff:
        return nm.replace(day=min(nm_eff))
    return None


def is_expired(r: dict, today: date) -> bool:
    end = r.get("end_date")
    return bool(end) and today > date.fromisoformat(end)


def next_fire_date(r: dict, today: date):
    """The next date this reminder will actually fire, or None if it won't.

    Accounts for pause, end_date, and whether today's slot was already sent.
    """
    if r.get("paused"):
        return None
    days = r.get("days", [])
    if not days:
        return None
    already_sent_today = r.get("last_sent") == today.isoformat()
    if day_fires_today(days, today) and not already_sent_today:
        candidate = today
    else:
        candidate = next_reminder_date(days, today)
    if candidate is None:
        return None
    end = r.get("end_date")
    if end and candidate > date.fromisoformat(end):
        return None
    return candidate


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _short_month_note(days: list) -> str:
    high = [d for d in sorted(days) if d > 28]
    if not high:
        return ""
    return (
        "\n\nNote: some months are shorter, so for day(s) "
        f"{', '.join(str(d) for d in high)} I'll send on the last day of the "
        "month when that date doesn't exist (e.g. day 31 → 28 Feb). "
        "No reminder is ever skipped."
    )


def _time_label(r: dict) -> str:
    return HOUR_LABELS.get(r.get("hour", 12), "12:00 PM")


def _reminder_label(r: dict) -> str:
    days_str = " & ".join(str(d) for d in sorted(r["days"]))
    msg = r["message"]
    preview = (msg[:18] + "…") if len(msg) > 18 else msg
    prefix = "⏸ " if r.get("paused") else ""
    return f"{prefix}Day {days_str}  ·  {_time_label(r)}  ·  {preview}"


def _menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    reminders = get_reminders(chat_id)
    all_paused = bool(reminders) and all(r.get("paused") for r in reminders)
    pause_label = "▶️ Resume all" if all_paused else "⏸ Pause all"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create new alert", callback_data=CB_NEW)],
        [InlineKeyboardButton("✏️ Change reminders", callback_data=CB_CHANGE)],
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
    buttons.append([InlineKeyboardButton("← Back", callback_data=CB_BACK_MENU)])
    return InlineKeyboardMarkup(buttons), "Select a reminder to edit:"


def _months_keyboard(back_cb: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(str(m), callback_data=f"setup:months:{m}") for m in range(r, r + 3)]
        for r in range(1, 12, 3)
    ]
    rows.append([InlineKeyboardButton("← Back", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def _time_keyboard(back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌅 9:00 AM",  callback_data=f"setup:time:9"),
            InlineKeyboardButton("☀️ 12:00 PM", callback_data=f"setup:time:12"),
            InlineKeyboardButton("🌙 9:00 PM",  callback_data=f"setup:time:21"),
        ],
        [InlineKeyboardButton("← Back", callback_data=back_cb)],
    ])


def _duration_keyboard(rid: int, back_cb: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(str(m), callback_data=f"rem:{rid}:set_months:{m}")
            for m in range(r, r + 3)
        ]
        for r in range(1, 12, 3)
    ]
    rows.append([InlineKeyboardButton("← Back", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def _edit_done_keyboard(rid) -> InlineKeyboardMarkup:
    rows = []
    if rid is not None:
        rows.append([InlineKeyboardButton("← Back to reminder", callback_data=f"rem:{rid}:view")])
    rows.append([InlineKeyboardButton("⚙️ Back to menu", callback_data=CB_BACK_MENU)])
    return InlineKeyboardMarkup(rows)


def _reminder_detail(r: dict):
    rid = r["id"]
    today = datetime.now(SGT).date()
    days_str = ", ".join(str(d) for d in sorted(r["days"]))
    end = r.get("end_date")
    end_str = datetime.fromisoformat(end).strftime("%d %b %Y") if end else "no end date"
    expired = is_expired(r, today)
    if r.get("paused"):
        status_str = "⏸ Paused"
    elif expired:
        status_str = "⌛ Ended"
    else:
        status_str = "▶️ Active"
    nxt = next_fire_date(r, today)
    next_str = nxt.strftime("%a %d %b %Y") if nxt else "—"
    toggle_label = "▶️ Resume" if r.get("paused") else "⏸ Pause"
    text = (
        f"Days: {days_str}\n"
        f"Time: {_time_label(r)} SGT\n"
        f"Message: {r['message']}\n"
        f"Until: {end_str}\n"
        f"Next fire: {next_str}\n"
        f"Status: {status_str}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Change days",     callback_data=f"rem:{rid}:change_days")],
        [InlineKeyboardButton("🕐 Change time",     callback_data=f"rem:{rid}:change_time")],
        [InlineKeyboardButton("💬 Change message",  callback_data=f"rem:{rid}:change_msg")],
        [InlineKeyboardButton("🗓 Change duration", callback_data=f"rem:{rid}:change_duration")],
        [InlineKeyboardButton(toggle_label,         callback_data=f"rem:{rid}:toggle")],
        [InlineKeyboardButton("🗑 Delete",          callback_data=f"rem:{rid}:delete")],
        [InlineKeyboardButton("← Back",             callback_data=CB_CHANGE)],
    ])
    return text, keyboard


# ---------------------------------------------------------------------------
# Admin guard
# ---------------------------------------------------------------------------

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        return True
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in ("administrator", "creator")


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not await is_admin(update, context):
        await update.message.reply_text("Only group admins can use this command.")
        return False
    return True


# ---------------------------------------------------------------------------
# /setup
# ---------------------------------------------------------------------------

async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

async def setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            "Which days of the month should I send reminders?\n"
            "Reply with day numbers separated by spaces — e.g. 1 15 28",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data=CB_BACK_MENU)]]
            ),
        )

    elif data == CB_CHANGE:
        keyboard, text = _reminder_list_keyboard(chat_id)
        back = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK_MENU)]])
        await query.edit_message_text(text, reply_markup=keyboard or back)

    elif data == CB_PAUSE_ALL:
        reminders = get_reminders(chat_id)
        if not reminders:
            await query.edit_message_text(
                "No reminders to pause. Use 'Create new alert' first.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK_MENU)]]),
            )
            return
        all_paused = all(r.get("paused") for r in reminders)
        new_state = not all_paused
        for r in reminders:
            r["paused"] = new_state
        _save_reminders(chat_id, reminders)
        word = "paused" if new_state else "resumed"
        await query.edit_message_text(
            f"All reminders {word}.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data=CB_BACK_MENU)]]
            ),
        )

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
            "Which days of the month should I send reminders?\n"
            "Reply with day numbers separated by spaces — e.g. 1 15 28",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data=CB_BACK_MENU)]]
            ),
        )

    elif data == "setup:back_time":
        # Back to time picker (days already stored)
        days = context.chat_data.get("pending_days", [])
        context.chat_data.pop(AWAIT, None)
        await query.edit_message_text(
            f"Days noted: {', '.join(str(d) for d in days)}\n\nWhat time should I send the reminder?",
            reply_markup=_time_keyboard("setup:back_days"),
        )

    elif data == "setup:back_months":
        context.chat_data.pop(AWAIT, None)
        days = context.chat_data.get("pending_days", [])
        hour = context.chat_data.get("pending_hour", 12)
        await query.edit_message_text(
            f"Days noted: {', '.join(str(d) for d in days)} at {HOUR_LABELS[hour]} SGT\n\n"
            "How many months should this reminder persist?",
            reply_markup=_months_keyboard("setup:back_time"),
        )

    elif data.startswith("setup:time:"):
        hour = int(data.split(":")[2])
        context.chat_data["pending_hour"] = hour
        days = context.chat_data.get("pending_days", [])
        await query.edit_message_text(
            f"Days noted: {', '.join(str(d) for d in days)} at {HOUR_LABELS[hour]} SGT\n\n"
            "How many months should this reminder persist?",
            reply_markup=_months_keyboard("setup:back_time"),
        )

    elif data.startswith("setup:months:"):
        months = int(data.split(":")[2])
        end_date = add_months(datetime.now(SGT).date(), months)
        context.chat_data["pending_months"] = months
        context.chat_data["pending_end_date"] = end_date.isoformat()
        context.chat_data[AWAIT] = AWAIT_MSG_NEW
        days = context.chat_data.get("pending_days", [])
        hour = context.chat_data.get("pending_hour", 12)
        await query.edit_message_text(
            f"Got it — {months} month(s), running until {end_date.strftime('%d %b %Y')}.\n\n"
            "What message should I send to this group on those days?",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data="setup:back_months")]]
            ),
        )


# ---------------------------------------------------------------------------
# Per-reminder callback handler (rem:{id}:{action})
# ---------------------------------------------------------------------------

async def reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await is_admin(update, context):
        await query.answer("Only admins can do this.", show_alert=True)
        return
    chat_id = query.message.chat.id

    parts = query.data.split(":", 2)
    _, rid_str, action = parts
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
            f"Current days: {days_str}\n\nReply with the new days — e.g. 1 15 28",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data=f"rem:{rid}:view")]]
            ),
        )

    elif action == "change_time":
        # Show 3 time buttons; selecting one uses rem:{id}:set_time:{hour}
        current = _time_label(r)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🌅 9:00 AM",  callback_data=f"rem:{rid}:set_time:9"),
                InlineKeyboardButton("☀️ 12:00 PM", callback_data=f"rem:{rid}:set_time:12"),
                InlineKeyboardButton("🌙 9:00 PM",  callback_data=f"rem:{rid}:set_time:21"),
            ],
            [InlineKeyboardButton("← Back", callback_data=f"rem:{rid}:view")],
        ])
        await query.edit_message_text(
            f"Current time: {current} SGT\n\nChoose a new send time:",
            reply_markup=keyboard,
        )

    elif action.startswith("set_time:"):
        new_hour = int(action.split(":")[1])
        update_reminder(chat_id, rid, hour=new_hour)
        r["hour"] = new_hour
        text, keyboard = _reminder_detail(r)
        await query.edit_message_text(text, reply_markup=keyboard)

    elif action == "change_msg":
        context.chat_data[AWAIT] = AWAIT_MSG_EDIT
        context.chat_data["editing_id"] = rid
        await query.edit_message_text(
            f"Current message:\n{r['message']}\n\nReply with the new message.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data=f"rem:{rid}:view")]]
            ),
        )

    elif action == "change_duration":
        today = datetime.now(SGT).date()
        end = r.get("end_date")
        end_str = datetime.fromisoformat(end).strftime("%d %b %Y") if end else "no end date"
        await query.edit_message_text(
            f"Current end date: {end_str}\n\n"
            "How many months from today should this reminder run?",
            reply_markup=_duration_keyboard(rid, f"rem:{rid}:view"),
        )

    elif action.startswith("set_months:"):
        months = int(action.split(":")[1])
        new_end = add_months(datetime.now(SGT).date(), months)
        # Extending re-activates an ended/paused reminder and clears the
        # stale last_sent so it can fire again today if applicable.
        update_reminder(chat_id, rid, end_date=new_end.isoformat(),
                        paused=False, last_sent=None)
        r = get_reminder_by_id(chat_id, rid)
        text, keyboard = _reminder_detail(r)
        await query.edit_message_text(
            f"Duration updated — now running until {new_end.strftime('%d %b %Y')}.\n\n"
            + text,
            reply_markup=keyboard,
        )

    elif action == "toggle":
        today = datetime.now(SGT).date()
        currently_paused = r.get("paused", False)
        # Resuming a reminder whose end date has already passed would do
        # nothing (it stays skipped), so send the user to pick a new duration.
        if currently_paused and is_expired(r, today):
            await query.edit_message_text(
                "This reminder's end date has already passed, so resuming it "
                "wouldn't fire anything.\n\nPick a new duration to restart it:",
                reply_markup=_duration_keyboard(rid, f"rem:{rid}:view"),
            )
            return
        new_paused = not currently_paused
        update_reminder(chat_id, rid, paused=new_paused)
        r["paused"] = new_paused
        text, keyboard = _reminder_detail(r)
        await query.edit_message_text(text, reply_markup=keyboard)

    elif action == "delete":
        days_str = ", ".join(str(d) for d in sorted(r["days"]))
        preview = (r["message"][:30] + "…") if len(r["message"]) > 30 else r["message"]
        await query.edit_message_text(
            f"Delete this reminder?\n\nDays: {days_str}\nTime: {_time_label(r)} SGT\nMessage: {preview}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Yes, delete", callback_data=f"rem:{rid}:confirm_delete")],
                [InlineKeyboardButton("← Keep it",     callback_data=f"rem:{rid}:view")],
            ]),
        )

    elif action == "confirm_delete":
        delete_reminder(chat_id, rid)
        keyboard, text = _reminder_list_keyboard(chat_id)
        back = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=CB_BACK_MENU)]])
        await query.edit_message_text(
            "Reminder deleted.\n\n" + text,
            reply_markup=keyboard or back,
        )


# ---------------------------------------------------------------------------
# Text reply handler
# ---------------------------------------------------------------------------

async def handle_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        context.chat_data.pop(AWAIT, None)
        await update.message.reply_text(
            f"Days noted: {', '.join(str(d) for d in days)}{_short_month_note(days)}"
            "\n\nWhat time should I send the reminder?",
            reply_markup=_time_keyboard("setup:back_days"),
        )

    elif awaiting == AWAIT_MSG_NEW:
        days = context.chat_data.pop("pending_days", [])
        hour = context.chat_data.pop("pending_hour", 12)
        context.chat_data.pop("pending_months", None)
        end_date_iso = context.chat_data.pop("pending_end_date", None)
        context.chat_data.pop(AWAIT, None)
        add_reminder(chat_id, days, hour, text, end_date_iso)
        end_str = (
            datetime.fromisoformat(end_date_iso).strftime("%d %b %Y")
            if end_date_iso else "no end date"
        )
        await update.message.reply_text(
            f"Reminder added! Day(s) {', '.join(str(d) for d in days)} "
            f"at {HOUR_LABELS[hour]} SGT until {end_str}.\n\nMessage:\n{text}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⚙️ Back to menu", callback_data=CB_BACK_MENU)]]
            ),
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
        await update.message.reply_text(
            f"Days updated to: {', '.join(str(d) for d in days)}{_short_month_note(days)}",
            reply_markup=_edit_done_keyboard(rid),
        )

    elif awaiting == AWAIT_MSG_EDIT:
        rid = context.chat_data.pop("editing_id", None)
        context.chat_data.pop(AWAIT, None)
        if rid is not None:
            update_reminder(chat_id, rid, message=text)
        await update.message.reply_text(
            f"Message updated to:\n\n{text}",
            reply_markup=_edit_done_keyboard(rid),
        )


# ---------------------------------------------------------------------------
# /start and /help
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reminders = get_reminders(update.effective_chat.id)
    if not reminders:
        await update.message.reply_text("No reminders configured. Use /setup to create one.")
        return
    today = datetime.now(SGT).date()
    lines = ["Your reminders:\n"]
    for i, r in enumerate(reminders, 1):
        days_str = ", ".join(str(d) for d in sorted(r["days"]))
        end = r.get("end_date")
        end_str = datetime.fromisoformat(end).strftime("%d %b %Y") if end else "no end date"
        if r.get("paused"):
            status_str = "⏸ Paused"
        elif is_expired(r, today):
            status_str = "⌛ Ended"
        else:
            status_str = "▶️ Active"
        nxt = next_fire_date(r, today)
        next_str = nxt.strftime("%a %d %b %Y") if nxt else "—"
        lines.append(
            f"{i}. {status_str}\n"
            f"   Days: {days_str} · Time: {_time_label(r)} SGT · Until: {end_str}\n"
            f"   Next fire: {next_str}\n"
            f"   Message: {r['message']}"
        )
    await update.message.reply_text("\n\n".join(lines))


# ---------------------------------------------------------------------------
# Scheduled job — fires at 9 AM, 12 PM, and 9 PM SGT
# Each invocation only sends reminders matching its hour.
#
# Every reminder records `last_sent` (an ISO date). A reminder is sent at most
# once per calendar day, so re-running a slot (e.g. the startup catch-up below,
# or an overlapping restart) never double-sends.
# ---------------------------------------------------------------------------

async def _send_for_hour(bot, job_hour: int) -> None:
    today = datetime.now(SGT).date()
    today_iso = today.isoformat()
    config = load_config()

    for chat_id_str, chat_cfg in list(config.items()):
        reminders = chat_cfg.get("reminders", [])
        changed = False
        for r in reminders:
            if r.get("paused"):
                continue
            if r.get("hour", 12) != job_hour:
                continue
            days = r.get("days", [])
            message = r.get("message", "")
            end_date_str = r.get("end_date")
            if not days or not message:
                continue
            if end_date_str and today > date.fromisoformat(end_date_str):
                continue
            if not day_fires_today(days, today):
                continue
            if r.get("last_sent") == today_iso:  # already sent today — dedupe
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
                await bot.send_message(int(chat_id_str), send_text)
                logger.info("Sent reminder %d to chat %s (hour=%d)", r["id"], chat_id_str, job_hour)
                r["last_sent"] = today_iso
                changed = True
                if is_final:
                    r["paused"] = True
            except Exception as exc:
                logger.error("Failed to send reminder %d to %s: %s", r["id"], chat_id_str, exc)

        if changed:
            config[chat_id_str] = chat_cfg
            save_config(config)


async def send_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_for_hour(context.bot, context.job.data["hour"])


async def catch_up(context: ContextTypes.DEFAULT_TYPE) -> None:
    """On startup, fire any of today's slots that already passed while the bot
    was offline. Dedupe via `last_sent` means anything already delivered today
    is skipped, so this is safe to run on every boot."""
    now = datetime.now(SGT)
    missed = [h for h in SEND_HOURS if h <= now.hour]
    if missed:
        logger.info("Startup catch-up: checking slots %s for today", missed)
    for hour in missed:
        await _send_for_hour(context.bot, hour)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("setup", "Create or manage reminders"),
        BotCommand("status", "View active reminders"),
        BotCommand("help", "How to use this bot"),
    ])


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setup", setup_start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(setup_callback, pattern=r"^setup:"))
    app.add_handler(CallbackQueryHandler(reminder_callback, pattern=r"^rem:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply))

    for hour in SEND_HOURS:
        app.job_queue.run_daily(
            send_reminders,
            time=time(hour=hour, minute=0, tzinfo=SGT),
            name=f"daily_reminder_{hour}",
            data={"hour": hour},
        )

    # Catch up on any of today's slots missed while the bot was offline.
    app.job_queue.run_once(catch_up, when=5, name="startup_catch_up")

    logger.info("Bot started. Reminders stored at %s", DATA_FILE)
    if not os.path.isabs(DATA_FILE) or DATA_FILE.startswith("/tmp"):
        logger.warning(
            "DATA_FILE=%s may be ephemeral — set it to a mounted volume path "
            "so reminders survive restarts/redeploys.", DATA_FILE
        )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
