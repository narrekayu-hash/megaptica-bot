import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Filter
from aiogram.types import Message

from .. import settings, sky
from ..db import db
from ..texts import t
from ..utils import is_moderator, later
from .content import answer_privately

router = Router(name="sky")
logger = logging.getLogger(__name__)
NAMES = {"events": "events", "события": "events", "sky": "events", "skyhere": "here"}


class SkyCmd(Filter):
    async def __call__(self, message: Message, bot: Bot):
        text = message.text or ""
        if len(text) < 2 or text[0] not in "/!":
            return False
        head, _, args = text[1:].partition(" ")
        name, _, at = head.lower().partition("@")
        if at and at != (await bot.me()).username.lower():
            return False
        cmd = NAMES.get(name)
        return {"sky_cmd": cmd, "args": args.strip().lower()} if cmd else False


@router.message(SkyCmd())
async def on_sky(message: Message, bot: Bot, sky_cmd: str, args: str):
    chat = message.chat
    if sky_cmd == "here":
        if not await is_moderator(bot, message):
            return
        if args in ("off", "выкл", "0", "-"):
            await settings.put(chat.id, "sky_topic", None)
            return await message.reply(await t(chat.id, "sky.off"))
        await settings.put(chat.id, "sky_topic", str(message.message_thread_id if message.is_topic_message else 0))
        return await message.reply(await t(chat.id, "sky.here"))
    if not (await settings.get(chat.id))["cmd_events"]:
        return
    text = sky.overview(datetime.now(timezone.utc), (await settings.get(chat.id))["sky_tz"])
    await answer_privately(bot, message, json.dumps({"kind": "text", "text": text}), f"sky{chat.id}")


async def reminder_text(chat_id: int, key: str, start: datetime, info, now: datetime, tz: str) -> str:
    left = sky.left(start - now)
    if key == "shard":
        land = next(l for s, l, _ in info.occurrences if s == start)
        color = "🔴 красный" if info.red else "⚫ чёрный"
        reward = f", вознесённых свечей: {info.reward:g}" if info.reward else ""
        return await t(chat_id, "sky.shard", left=left, color=color, place=info.place, reward=reward,
                       start=sky.clock(start, tz), land=sky.clock(land, tz))
    name, emoji, _, _ = sky.REGULAR[key]
    return await t(chat_id, "sky.event", left=left, emoji=emoji, event=name, time=sky.clock(start, tz))


_sent = {}  # (чат, событие, начало) -> когда отправили; чтобы не повторяться


async def tick(bot: Bot, now: datetime):
    for row in await db.all("SELECT chat_id FROM chats WHERE active=1"):
        chat_id = row["chat_id"]
        s = await settings.get(chat_id)
        if s["sky_topic"] is None:
            continue
        for key, start, info in sky.upcoming(now, timedelta(minutes=s["sky_before"])):
            mark = (chat_id, key, int(start.timestamp()))
            if not s[f"sky_{key}"] or mark in _sent:
                continue
            _sent[mark] = now
            try:
                msg = await bot.send_message(chat_id, await reminder_text(chat_id, key, start, info, now, s["sky_tz"]),
                                             message_thread_id=int(s["sky_topic"]) or None)
            except TelegramAPIError as e:
                logger.warning("Не удалось отправить напоминание Sky в %s: %s", chat_id, e)
                continue
            if s["sky_cleanup"]:  # убираем напоминание, когда событие уже идёт, чтобы тема не копила их
                later((start - now).total_seconds() + 15 * 60, msg.delete())
    for mark, sent_at in list(_sent.items()):
        if now - sent_at > timedelta(days=1):
            del _sent[mark]


async def scheduler(bot: Bot):
    while True:
        try:
            await tick(bot, datetime.now(timezone.utc))
        except Exception:
            logger.exception("Ошибка в напоминаниях Sky")
        await asyncio.sleep(20)
