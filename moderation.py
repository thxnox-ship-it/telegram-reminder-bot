"""Comment-section moderation for the linked discussion groups behind
daywasg / handfulofleaves.

Two independent hard gates, either one bans immediately (no message-content
or account-age gating — both signals are direct evidence, not heuristics):

  - avatar: profile photo trips Sightengine's nudity-2.1 model
  - bio: profile bio text contains an NSFW keyword/site name

On a ban: the user is banned, their triggering message (if any) is deleted,
and a short notice is posted in the chat. Each (chat, user) pair is only
checked once, ever, via the same per-chat JSON store `bot.py` already uses
for reminders (no separate database).
"""

import logging
import os
from urllib.parse import urlencode

import httpx
from telegram import Update, User
from telegram.ext import ApplicationHandlerStop, ContextTypes

# Imported as a module (not `from bot import ...`) to avoid a circular
# import at load time — bot.py imports this module too, so the reference to
# bot.load_config/save_config is only resolved lazily, inside functions.
import bot as _bot

logger = logging.getLogger(__name__)

SIGHTENGINE_ENDPOINT = "https://api.sightengine.com/1.0/check.json"

ACTIVE_MEMBER_STATUSES = {"member", "administrator", "creator", "restricted"}

# Substring match is intentional: it catches both the bare keyword
# ("onlyfans") and the same word inside a URL a spam bio links out to
# ("onlyfans.com/...").
NSFW_BIO_KEYWORDS = [
    "onlyfans",
    "only fans",
    "fansly",
    "chaturbate",
    "pornhub",
    "xvideos",
    "xnxx",
    "xxx",
    "nsfw",
    "18+",
    "nude",
    "nudes",
    "sexcam",
    "sex cam",
    "escort",
    "camgirl",
    "cam girl",
    "livejasmin",
    "myfreecams",
    "stripchat",
    "bongacams",
]


def _moderated_chat_ids() -> set:
    raw = os.environ.get("MODERATION_CHAT_IDS", "")
    return {int(s.strip()) for s in raw.split(",") if s.strip()}


def _bio_is_nsfw(bio) -> bool:
    if not bio:
        return False
    lower = bio.lower()
    return any(keyword in lower for keyword in NSFW_BIO_KEYWORDS)


async def _avatar_is_nsfw(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    api_user = os.environ.get("SIGHTENGINE_API_USER")
    api_secret = os.environ.get("SIGHTENGINE_API_SECRET")
    if not api_user or not api_secret:
        logger.warning("SIGHTENGINE_API_USER/SECRET not set — skipping avatar check for %s", user_id)
        return False

    threshold = float(os.environ.get("NSFW_THRESHOLD", "0.6"))

    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count == 0:
            return False

        largest = photos.photos[0][-1]
        file = await context.bot.get_file(largest.file_id)
        # PTB's get_file() already rewrites file_path into a full
        # https://api.telegram.org/file/bot<token>/... URL.
        image_url = file.file_path

        params = {
            "url": image_url,
            "models": "nudity-2.1",
            "api_user": api_user,
            "api_secret": api_secret,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{SIGHTENGINE_ENDPOINT}?{urlencode(params)}")
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Sightengine error: {data.get('error', data)}")

        none_score = float(data.get("nudity", {}).get("none", 1))
        risk_score = 1 - none_score
        return risk_score >= threshold
    except Exception:
        # Fail open: an API outage shouldn't ban legitimate members.
        logger.exception("Avatar NSFW check failed for user %s", user_id)
        return False


def _get_checked(chat_id: int, user_id: int):
    cfg = _bot.load_config()
    chat_cfg = cfg.get(str(chat_id), {})
    return chat_cfg.get("moderation_checked", {}).get(str(user_id))


def _mark_checked(chat_id: int, user_id: int, flagged: bool, reason) -> None:
    cfg = _bot.load_config()
    key = str(chat_id)
    chat_cfg = cfg.get(key, {})
    checked = chat_cfg.get("moderation_checked", {})
    checked[str(user_id)] = {"flagged": flagged, "reason": reason}
    chat_cfg["moderation_checked"] = checked
    cfg[key] = chat_cfg
    _bot.save_config(cfg)


async def _check_and_act(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user: User,
    triggering_message_id=None,
) -> bool:
    existing = _get_checked(chat_id, user.id)
    if existing is not None:
        return existing["flagged"]

    avatar_flagged = await _avatar_is_nsfw(context, user.id)

    bio_flagged = False
    try:
        chat_full = await context.bot.get_chat(user.id)
        bio_flagged = _bio_is_nsfw(getattr(chat_full, "bio", None))
    except Exception:
        logger.exception("Bio check failed for user %s", user.id)

    flagged = avatar_flagged or bio_flagged
    reasons = []
    if avatar_flagged:
        reasons.append("avatar")
    if bio_flagged:
        reasons.append("bio")
    reason = ",".join(reasons) if reasons else None

    if flagged:
        try:
            await context.bot.ban_chat_member(chat_id, user.id)
            if triggering_message_id:
                try:
                    await context.bot.delete_message(chat_id, triggering_message_id)
                except Exception:
                    pass
            display_name = f"@{user.username}" if user.username else user.first_name
            await context.bot.send_message(
                chat_id, f"\U0001f6ab Banned {display_name} for an inappropriate profile."
            )
            logger.info("Banned user %s from chat %s — reason: %s", user.id, chat_id, reason)
        except Exception:
            logger.exception("Failed to ban user %s in chat %s", user.id, chat_id)

    _mark_checked(chat_id, user.id, flagged, reason)
    return flagged


async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Primary path: react the moment Telegram tells us someone joined,
    before they get a chance to post anything."""
    moderated = _moderated_chat_ids()
    if not moderated or update.effective_chat.id not in moderated:
        return

    cm = update.chat_member
    was_active = cm.old_chat_member.status in ACTIVE_MEMBER_STATUSES
    is_active = cm.new_chat_member.status in ACTIVE_MEMBER_STATUSES
    if was_active or not is_active:
        return  # only react to a fresh join

    await _check_and_act(context, update.effective_chat.id, cm.new_chat_member.user)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback path: covers members who joined before the bot could see
    chat_member updates for them (e.g. the bot was just added as admin)."""
    moderated = _moderated_chat_ids()
    chat = update.effective_chat
    if not moderated or not chat or chat.id not in moderated or not update.effective_user:
        return

    message_id = update.message.message_id if update.message else None
    flagged = await _check_and_act(context, chat.id, update.effective_user, message_id)
    if flagged:
        raise ApplicationHandlerStop  # message already deleted and user banned
