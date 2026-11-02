import logging
from contextlib import suppress
from datetime import timedelta
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import Chat

from . import antibot, settings
from .db import db
from .config import config
from .texts import t
from .utils import MUTED, fmt_duration

log_ = logging.getLogger(__name__)

async def reason_line(chat_id: int, reason: str) -> str:
    return "\n" + await t(chat_id, "mod.reason", reason=escape(reason)) if reason else ""


async def admin_line(chat_id: int, by) -> str:
    return "\n" + await t(chat_id, "mod.admin", admin=by) if by else ""


async def announce(bot: Bot, chat_id: int, text: str, thread=None):
    with suppress(TelegramAPIError):
        await bot.send_message(chat_id, text, message_thread_id=thread)


async def log(bot: Bot, chat: Chat, text: str, markup=None, fallback_to_owners=False):
    """Пишет в лог-чат. Важные отчёты (с кнопками) без лог-чата уходят владельцам в ЛС."""
    target = (await settings.get(chat.id))["log_chat"]
    targets = [target] if target else (sorted(config.owners) if fallback_to_owners else [])
    for target_chat in targets:
        try:
            await bot.send_message(target_chat, f"<b>{escape(chat.title or str(chat.id))}</b>\n{text}", reply_markup=markup)
        except TelegramAPIError as e:
            log_.warning("Не удалось отправить лог в %s: %s", target_chat, e)


async def punish(bot: Bot, chat: Chat, uid: int, name: str, action: str, seconds=None,
                 reason="", by=None, thread=None):
    """Банит/мутит/кикает/варнит. Ошибки Telegram пробрасывает наверх."""
    if action == "warn":
        return await warn(bot, chat, uid, name, reason, by, thread)
    if seconds and seconds < 60:
        seconds = 60  # меньше 30 секунд Telegram считает «навсегда»
    until = timedelta(seconds=seconds) if seconds else None
    if action == "ban":
        await bot.ban_chat_member(chat.id, uid, until_date=until)
    elif action == "mute":
        await bot.restrict_chat_member(chat.id, uid, MUTED, until_date=until)
    elif action == "kick":
        await bot.ban_chat_member(chat.id, uid)
        await bot.unban_chat_member(chat.id, uid)
    duration = " " + await t(chat.id, "mod.duration", time=fmt_duration(seconds)) if seconds and action != "kick" else ""
    text = await t(chat.id, f"mod.{action}", user=name, duration=duration,
                   reason=await reason_line(chat.id, reason), admin=await admin_line(chat.id, by))
    await announce(bot, chat.id, text, thread)
    await log(bot, chat, text)


async def warn(bot: Bot, chat: Chat, uid: int, name: str, reason="", by=None, thread=None):
    s = await settings.get(chat.id)
    n, limit = await db.add_warn(chat.id, uid, 1), s["warn_limit"]
    text = await t(chat.id, "mod.warn", user=name, count=n, limit=limit,
                   reason=await reason_line(chat.id, reason), admin=await admin_line(chat.id, by))
    await announce(bot, chat.id, text, thread)
    await log(bot, chat, text)
    if n >= limit:
        await db.reset_warns(chat.id, uid)
        await punish(bot, chat, uid, name, s["warn_action"], s["mute_minutes"] * 60,
                     "лимит предупреждений", thread=thread)


async def delete_recent(bot: Bot, chat_id: int, uid: int, extra_ids=()) -> list[int]:
    """Удаляет сообщения человека, которые бот видел за 48 часов. Бан сам их в супергруппе не удаляет."""
    ids = sorted(set(antibot.recent(chat_id, uid)) | {i for i in extra_ids if i})
    for i in range(0, len(ids), 100):
        with suppress(TelegramAPIError):
            await bot.delete_messages(chat_id, ids[i:i + 100])
    return ids


async def spam_ban(bot: Bot, chat: Chat, uid: int, name: str, extra_ids=(), reason="спам-бот", by=None,
                   log_it=True) -> int:
    """Бан навсегда + удаление всех его сообщений, которые бот видел за последние 48 часов."""
    await bot.ban_chat_member(chat.id, uid, revoke_messages=True)
    ids = await delete_recent(bot, chat.id, uid, extra_ids)
    if log_it:
        await log(bot, chat, f"🤖 {name} забанен(а) как спам-бот, удалено сообщений: {len(ids)}"
                  + await reason_line(chat.id, reason) + await admin_line(chat.id, by))
    return len(ids)


class Undo(CallbackData, prefix="undo"):
    """Кнопка в отчёте об автобане: c — чат, u — кого разбанить (0 — никого), m — какую метку удалить."""
    c: int
    u: int = 0
    m: int = 0


MARKER_NAMES = {"text": "текст", "link": "ссылка", "mention": "упоминание", "media": "картинка/файл",
                "forward": "пересылка из"}
