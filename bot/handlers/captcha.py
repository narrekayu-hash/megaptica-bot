import asyncio
import logging
import random
import time
from contextlib import suppress
from html import escape

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Chat, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, User

from .. import settings
from ..actions import log
from ..db import db
from ..texts import t
from ..utils import MUTED, default_permissions, drop_admins, user_mention

router = Router(name="captcha")
logger = logging.getLogger(__name__)

EMOJI = {"🍎": "яблоко", "🐶": "собаку", "🚗": "машину", "⚽": "мяч", "🌙": "луну", "🎸": "гитару", "🍕": "пиццу",
         "🐱": "кота"}
_last_welcome = {}  # chat_id -> message_id прошлого приветствия (удаляем, чтобы не копились)


class Cap(CallbackData, prefix="cap"):
    uid: int
    ok: int


def _in_chat(member) -> bool:
    return member.status in ("member", "administrator", "creator") or (
        member.status == "restricted" and member.is_member)


async def welcome(bot: Bot, chat: Chat, user: User):
    s = await settings.get(chat.id)
    if not s["welcome"] or not s["welcome_text"]:
        return
    text = (s["welcome_text"].replace("{mention}", user_mention(user))
            .replace("{name}", escape(user.first_name)).replace("{chat}", escape(chat.title or "")))
    old = _last_welcome.pop(chat.id, None)
    if old:
        with suppress(TelegramAPIError):
            await bot.delete_message(chat.id, old)
    with suppress(TelegramAPIError):
        _last_welcome[chat.id] = (await bot.send_message(chat.id, text)).message_id


async def _kick(bot: Bot, chat_id: int, user_id: int):
    with suppress(TelegramAPIError):
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)


@router.chat_member()
async def on_member(event: ChatMemberUpdated, bot: Bot):
    old, new = event.old_chat_member, event.new_chat_member
    if {"administrator", "creator"} & {old.status, new.status}:
        drop_admins(event.chat.id)
    user = new.user
    if _in_chat(old) and not _in_chat(new):  # ушёл, не пройдя капчу
        row = await db.captcha_pop(event.chat.id, user.id)
        if row:
            with suppress(TelegramAPIError):
                await bot.delete_message(event.chat.id, row["message_id"])
        return
    if _in_chat(old) or not _in_chat(new) or user.is_bot:
        return

    await db.set_joined(event.chat.id, user.id, int(time.time()))
    s = await settings.get(event.chat.id)
    if not s["captcha"]:
        return await welcome(bot, event.chat, user)
    try:
        await bot.restrict_chat_member(event.chat.id, user.id, MUTED)
    except TelegramAPIError as e:
        logger.warning("Не могу ограничить новичка для капчи: %s", e)
        return await welcome(bot, event.chat, user)

    options = random.sample(list(EMOJI), 4)
    right = random.choice(options)
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=e, callback_data=Cap(uid=user.id, ok=int(e == right)).pack()) for e in options
    ]])
    msg = await bot.send_message(
        event.chat.id,
        await t(event.chat.id, "new.captcha", user=user_mention(user), item=EMOJI[right], seconds=s["captcha_seconds"]),
        reply_markup=markup,
    )
    await db.captcha_add(event.chat.id, user.id, msg.message_id, int(time.time()) + s["captcha_seconds"])


@router.callback_query(Cap.filter())
async def on_answer(cb: CallbackQuery, callback_data: Cap, bot: Bot):
    chat = cb.message.chat
    if cb.from_user.id != callback_data.uid:
        return await cb.answer(await t(chat.id, "new.captcha_alien"), show_alert=True)
    if not await db.captcha_pop(chat.id, cb.from_user.id):
        return await cb.answer()
    with suppress(TelegramAPIError):
        await cb.message.delete()
    if callback_data.ok:
        await bot.restrict_chat_member(chat.id, cb.from_user.id, await default_permissions(bot, chat.id))
        await cb.answer(await t(chat.id, "new.captcha_ok"))
        await welcome(bot, chat, cb.from_user)
    else:
        await cb.answer(await t(chat.id, "new.captcha_wrong"), show_alert=True)
        await _kick(bot, chat.id, cb.from_user.id)
        await log(bot, chat, f"🤖 {user_mention(cb.from_user)} не прошёл(а) капчу")


async def watchdog(bot: Bot):
    """Раз в 10 секунд выгоняет тех, кто не успел пройти капчу. Переживает перезапуски — всё в базе."""
    while True:
        try:
            for row in await db.captcha_expired(int(time.time())):
                await db.captcha_pop(row["chat_id"], row["user_id"])
                with suppress(TelegramAPIError):
                    await bot.delete_message(row["chat_id"], row["message_id"])
                await _kick(bot, row["chat_id"], row["user_id"])
        except Exception:
            logger.exception("Ошибка в проверке капчи")
        await asyncio.sleep(10)
