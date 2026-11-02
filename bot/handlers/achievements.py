"""Ачивки в чате: /ачивки, выдача админами, реакции на фото и публичные поздравления."""
import asyncio
import json
import logging
import re
from collections import defaultdict
from contextlib import suppress

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           MessageReactionCountUpdated, MessageReactionUpdated)

from .. import achievements as A
from .. import settings
from ..actions import announce
from ..db import db
from ..texts import t
from ..utils import can_restrict, ephemeral, is_moderator, later, mention, thread_of, user_mention
from .content import answer_privately

router = Router(name="achievements")
logger = logging.getLogger(__name__)
NAMES = {"achievements": "list", "ачивки": "list", "ach": "give", "ачивка": "give",
         "unach": "take", "снятьачивку": "take", "achhere": "here"}


class Give(CallbackData, prefix="ag"):
    c: int
    u: int
    k: str
    t: int = 0


class AchCmd(Filter):
    async def __call__(self, message: Message, bot: Bot):
        text = message.text or ""
        if len(text) < 2 or text[0] not in "/!":
            return False
        head, _, args = text[1:].partition(" ")
        name, _, at = head.lower().partition("@")
        if at and at != (await bot.me()).username.lower():
            return False
        cmd = NAMES.get(name)
        return {"ach_cmd": cmd, "args": args.strip()} if cmd else False


async def notify(bot: Bot, m: Message, helped: bool):
    """Вызывается антиспамом для каждого принятого сообщения: выдаёт ачивки и тихо сообщает о них автору."""
    try:
        new = await A.track(m, helped)
    except Exception:
        logger.exception("Ошибка при подсчёте ачивок")
        return
    if new:
        later(0, bot.send_message(m.chat.id, await A.congrats(m.chat.id, new), message_thread_id=thread_of(m),
                                  ephemeral_message_parameters=ephemeral(m.from_user.id)))


async def tell_admin(bot: Bot, message: Message, text: str, markup=None):
    """Ответ админу, который видит только он; если не вышло — обычное сообщение на минуту."""
    if message.from_user and not message.sender_chat:
        try:
            return await bot.send_message(message.chat.id, text, message_thread_id=thread_of(message), reply_markup=markup,
                                          ephemeral_message_parameters=ephemeral(message.from_user.id))
        except TelegramAPIError as e:
            logger.info("Эфемерный ответ админу не отправился: %s", e)
    note = await bot.send_message(message.chat.id, text, message_thread_id=thread_of(message), reply_markup=markup)
    later(60, note.delete())


def _plain(title: str) -> str:
    return re.sub(r"[^\w ]", "", title.lower()).strip()


def find(items, query: str):
    q = _plain(query)
    if not q:
        return None
    for a in items:
        if a.code == q or _plain(a.title) == q:
            return a
    hits = [a for a in items if q in _plain(a.title)]
    return hits[0] if len(hits) == 1 else None


async def award_public(bot: Bot, chat_id: int, uid: int, name: str, a, thread):
    await announce(bot, chat_id, await t(chat_id, "ach.award", user=mention(uid, name), achievement=a.title,
                                         description=a.desc[0].upper() + a.desc[1:]), thread)


@router.message(AchCmd())
async def on_ach(message: Message, bot: Bot, ach_cmd: str, args: str):
    chat = message.chat
    s = await settings.get(chat.id)
    reply = message.reply_to_message
    target = reply.from_user if reply and not reply.forum_topic_created and not reply.sender_chat else None
    if ach_cmd == "list":
        user = message.from_user
        if not user:
            return
        other = target if target and target.id != user.id and not target.is_bot else None
        who = other or user
        text = await A.collection_text(chat.id, who.id, who.full_name, own=other is None)
        return await answer_privately(bot, message, json.dumps({"kind": "text", "text": text}), f"ach{chat.id}_{who.id}")

    if not await is_moderator(bot, message):
        return
    if not message.ephemeral_message_id:
        with suppress(TelegramAPIError):
            await message.delete()
    if ach_cmd == "here":
        off = args.lower() in ("off", "выкл", "0", "-")
        await settings.put(chat.id, "ach_topic", None if off else str(message.message_thread_id if message.is_topic_message else 0))
        return await tell_admin(bot, message, await t(chat.id, "ach.here_off" if off else "ach.here"))
    if not s["achievements"]:
        return await tell_admin(bot, message, "🏆 Ачивки в этом чате выключены (панель: /start → 🏆 Ачивки).")
    if not target or target.is_bot:
        return await tell_admin(bot, message, "Ответь этой командой на сообщение участника.")
    cat = A.enabled(s)
    if ach_cmd == "take":
        a = find(cat.values(), args)
        if not a:
            return await tell_admin(bot, message, "Напиши, какую ачивку снять: <code>/снятьачивку художник</code>.")
        removed = await db.ach_del(chat.id, target.id, a.code)
        return await tell_admin(bot, message, f"✅ {a.title} снята с {user_mention(target)}." if removed else
                                f"У {user_mention(target)} нет ачивки {a.title}.")
    manual = [a for a in cat.values() if a.kind == "manual"]
    if not manual:
        return await tell_admin(bot, message, "Ачивок для выдачи нет: все выключены в панели.")
    if args:
        a = find(manual, args)
        if not a:
            names = ", ".join(a.title for a in manual)
            return await tell_admin(bot, message, f"Такой ачивки нет. Можно выдать: {names}.")
        return await _give(bot, chat.id, target.id, target.full_name, a, thread_of(message), s,
                           lambda text: tell_admin(bot, message, text))
    topic = thread_of(message) or 0
    rows = [[InlineKeyboardButton(text=a.title, callback_data=Give(c=chat.id, u=target.id, k=a.code, t=topic).pack())]
            for a in manual]
    await tell_admin(bot, message, f"Какую ачивку выдать {user_mention(target)}?", InlineKeyboardMarkup(inline_keyboard=rows))


async def _give(bot: Bot, chat_id: int, uid: int, name: str, a, thread, s, reply):
    if not await A.give(chat_id, uid, name, [a.code], s):
        return await reply(f"У {mention(uid, name)} уже есть {a.title}.")
    await award_public(bot, chat_id, uid, name, a, thread)


@router.callback_query(Give.filter())
async def on_give(cb: CallbackQuery, callback_data: Give, bot: Bot):
    d = callback_data
    if not await can_restrict(bot, cb.from_user.id, d.c):
        return await cb.answer("Выдавать ачивки могут только админы с правом банить.", show_alert=True)
    s = await settings.get(d.c)
    a = A.enabled(s).get(d.k)
    if not a:
        return await cb.answer("Эта ачивка уже выключена.", show_alert=True)
    try:
        name = (await bot.get_chat_member(d.c, d.u)).user.full_name
    except TelegramAPIError:
        name = str(d.u)
    answered = []

    async def reply(text):
        answered.append(text)

    await _give(bot, d.c, d.u, name, a, d.t or None, s, reply)
    await cb.answer("Уже есть" if answered else "Выдано 🎉")
    if cb.message and not answered:
        with suppress(TelegramAPIError):
            await cb.message.edit_text(f"✅ {mention(d.u, name)} получает {a.title}", reply_markup=None)


async def _star(bot: Bot, chat_id: int, author):
    if author is None:
        return
    s = await settings.get(chat_id)
    try:
        name = (await bot.get_chat_member(chat_id, author)).user.full_name
    except TelegramAPIError:
        name = str(author)
    await A.give(chat_id, author, name, ["star"], s)


@router.message_reaction()
async def on_reaction(event: MessageReactionUpdated, bot: Bot):
    if event.user:
        await _star(bot, event.chat.id, await A.reaction(event.chat.id, event.message_id, event.user,
                                                          bool(event.new_reaction), None))


@router.message_reaction_count()
async def on_reaction_count(event: MessageReactionCountUpdated, bot: Bot):
    total = sum(r.total_count for r in event.reactions)
    await _star(bot, event.chat.id, await A.reaction(event.chat.id, event.message_id, None, True, total))


async def flush(bot: Bot):
    by_chat = defaultdict(list)
    for r in await db.ach_pending():
        by_chat[r["chat_id"]].append(r)
    for chat_id, rows in by_chat.items():
        await db.ach_mark_announced(chat_id)  # сначала отмечаем, чтобы не повториться
        s = await settings.get(chat_id)
        if not (s["achievements"] and s["ach_announce"]) or s["ach_topic"] is None:
            continue
        cat = {a.code: a for a in A.catalog(s)}
        lines = [await t(chat_id, "ach.batch")]
        lines += [f"• {mention(r['user_id'], r['name'] or str(r['user_id']))} — {cat[r['code']].title}"
                  for r in rows if r["code"] in cat]
        if len(lines) > 1:
            await announce(bot, chat_id, "\n".join(lines + ["", await t(chat_id, "ach.batch_footer")]),
                           int(s["ach_topic"]) or None)


async def announcer(bot: Bot):
    """Раз в 5 минут собирает редкие ачивки в одно поздравление на чат."""
    while True:
        try:
            await flush(bot)
        except Exception:
            logger.exception("Ошибка в поздравлениях с ачивками")
        await asyncio.sleep(300)
