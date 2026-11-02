import asyncio
import json
import re
import time
from contextlib import suppress
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions, EphemeralMessageParameters, Message, ReplyParameters, User

from .config import config
from .db import db

SEND_FIELDS = (
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos", "can_send_videos",
    "can_send_video_notes", "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
    "can_add_web_page_previews",
)
MUTED = ChatPermissions(**{f: False for f in SEND_FIELDS})

# --- время: 30m, 2h, 1d, 1w, 10м, 3ч, 2д, 1н ---
_UNITS = {"s": 1, "с": 1, "m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400, "w": 604800, "н": 604800}
_DUR = re.compile(r"^(\d+)([a-zа-яё]*)$")


def parse_duration(token: str, default_unit: str = "m"):
    m = _DUR.match(token.lower())
    if not m:
        return None
    unit = m.group(2)[:1] or default_unit
    return int(m.group(1)) * _UNITS[unit] if unit in _UNITS else None


def fmt_duration(sec: int) -> str:
    for size, name in ((604800, "нед"), (86400, "д"), (3600, "ч"), (60, "мин")):
        if sec % size == 0:
            return f"{sec // size} {name}"
    return f"{sec} сек"


# --- упоминания ---
def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(name)}</a>'


def user_mention(user: User) -> str:
    return mention(user.id, user.full_name)


def thread_of(message: Message):
    return message.message_thread_id if message.is_topic_message else None


_known_users = {}


async def remember_user(user: User):
    if _known_users.get(user.id, "") != user.username:
        _known_users[user.id] = user.username
        await db.save_user(user.id, user.username)


# --- администраторы (кэш на 5 минут, сбрасывается при смене прав) ---
_admins = {}


async def get_admins(bot: Bot, chat_id: int) -> dict:
    hit = _admins.get(chat_id)
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    members = {m.user.id: m for m in await bot.get_chat_administrators(chat_id)}
    _admins[chat_id] = (time.monotonic(), members)
    return members


def drop_admins(chat_id: int):
    _admins.pop(chat_id, None)


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    return user_id in config.owners or user_id in await get_admins(bot, chat_id)


async def is_moderator(bot: Bot, message: Message) -> bool:
    if message.sender_chat and message.sender_chat.id == message.chat.id:  # анонимный админ
        return True
    user = message.from_user
    if not user:
        return False
    if user.id in config.owners:
        return True
    m = (await get_admins(bot, message.chat.id)).get(user.id)
    return m is not None and (m.status == "creator" or bool(getattr(m, "can_restrict_members", False)))


async def can_restrict(bot: Bot, user_id: int, chat_id: int) -> bool:
    """Может ли человек банить в этом чате (для кнопок в логе, которые нажимают вне чата)."""
    if user_id in config.owners:
        return True
    try:
        m = (await get_admins(bot, chat_id)).get(user_id)
    except TelegramAPIError:
        return False
    return m is not None and (m.status == "creator" or bool(getattr(m, "can_restrict_members", False)))


async def can_manage(bot: Bot, user_id: int, chat_id: int) -> bool:
    """Доступ к панели: владелец бота, создатель чата, люди из списка «👥 Доступ»
    и (если это не выключено) админы с правом менять профиль группы."""
    from . import settings
    if user_id in config.owners:
        return True
    s = await settings.get(chat_id)
    if str(user_id) in s["panel_users"]:
        return True
    try:
        m = (await get_admins(bot, chat_id)).get(user_id)
    except TelegramAPIError:
        return False
    return m is not None and (m.status == "creator" or (s["panel_by_rights"] and bool(getattr(m, "can_change_info", False))))


async def is_chief(bot: Bot, user_id: int, chat_id: int) -> bool:
    """Владелец бота или создатель чата — только они раздают доступ к панели."""
    if user_id in config.owners:
        return True
    try:
        m = (await get_admins(bot, chat_id)).get(user_id)
    except TelegramAPIError:
        return False
    return m is not None and m.status == "creator"


_tasks = set()


def later(delay: float, coro):
    """Выполнить корутину через delay секунд, не блокируя обработчик (например, удалить сообщение)."""
    async def run():
        await asyncio.sleep(delay)
        with suppress(TelegramAPIError):
            await coro

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def default_permissions(bot: Bot, chat_id: int) -> ChatPermissions:
    chat = await bot.get_chat(chat_id)
    return chat.permissions or ChatPermissions(**{f: True for f in SEND_FIELDS})


# --- сохранённые сообщения (фильтры, заметки) ---
_MEDIA = ("photo", "video", "animation", "document", "audio", "voice", "sticker")


def serialize(message: Message):
    for kind in _MEDIA:
        obj = getattr(message, kind)
        if obj:
            file_id = obj[-1].file_id if kind == "photo" else obj.file_id
            return {"kind": kind, "file": file_id, "text": message.html_text if message.caption else None}
    if message.text:
        return {"kind": "text", "text": message.html_text}
    return None


def ephemeral(user_id: int) -> EphemeralMessageParameters:
    """Параметры сообщения, которое в группе увидит только этот участник (Bot API 10.3)."""
    return EphemeralMessageParameters(receiver_user_id=user_id)


async def send_content(bot: Bot, chat_id: int, data: str, reply_to=None, thread=None,
                       only_for=None, reply_to_ephemeral=None):
    d = json.loads(data)
    kw = {"chat_id": chat_id, "message_thread_id": thread}
    if only_for:
        kw["ephemeral_message_parameters"] = ephemeral(only_for)
    if reply_to_ephemeral:
        kw["reply_parameters"] = ReplyParameters(ephemeral_message_id=reply_to_ephemeral)
    elif reply_to:
        kw["reply_parameters"] = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
    kind = d["kind"]
    if kind == "text":
        await bot.send_message(text=d["text"], **kw)
    elif kind == "sticker":
        await bot.send_sticker(sticker=d["file"], **kw)
    else:
        await getattr(bot, f"send_{kind}")(**{kind: d["file"]}, caption=d.get("text"), **kw)
