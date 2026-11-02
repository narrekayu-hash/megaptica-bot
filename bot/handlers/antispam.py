import logging
import time
from collections import defaultdict, deque
from contextlib import suppress
from html import escape
from urllib.parse import urlparse

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import Message

from .. import settings
from .. import antibot
from . import achievements, helpers
from ..actions import punish
from ..texts import t
from . import guard
from ..db import db
from ..utils import ephemeral, fmt_duration, is_admin, later, remember_user, thread_of, user_mention

log = logging.getLogger(__name__)

_flood = defaultdict(deque)  # (chat_id, user_id) -> [(время, message_id)]
_last_post = {}              # (chat_id, тема, user_id) -> (время, media_group_id)
_slow_notified = {}          # (chat_id, тема, user_id) -> когда последний раз предупреждали
_topics = {}                 # (chat_id, тема) -> название, чтобы не писать в базу каждый раз


def topic_key(m: Message) -> int:
    return m.message_thread_id if m.is_topic_message else 0


async def remember_topic(m: Message):
    created = m.forum_topic_created or (m.reply_to_message and m.reply_to_message.forum_topic_created)
    name = (m.forum_topic_edited and m.forum_topic_edited.name) or (created and created.name)
    if name and m.message_thread_id and _topics.get((m.chat.id, m.message_thread_id)) != name:
        _topics[(m.chat.id, m.message_thread_id)] = name
        await db.save_topic(m.chat.id, m.message_thread_id, name)


def topic_name(chat_id: int, key: int) -> str:
    return "Общая" if key == 0 else _topics.get((chat_id, key), f"тема #{key}")


def _urls(m: Message):
    text = m.text or m.caption or ""
    for e in m.entities or m.caption_entities or []:
        if e.type == "url":
            yield e.extract_from(text)
        elif e.type == "text_link":
            yield e.url
    if m.reply_markup and m.reply_markup.inline_keyboard:
        for row in m.reply_markup.inline_keyboard:
            for b in row:
                if b.url:
                    yield b.url


def _allowed(url: str, whitelist) -> bool:
    host = (urlparse(url if "://" in url else "http://" + url).hostname or "").removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in whitelist)


async def tell_author(bot: Bot, m: Message, text: str, short_key: str, kind: str, left: str = ""):
    """Сообщает автору так, чтобы видел только он, и возвращает текст удалённого сообщения.

    Основной путь — эфемерное сообщение в теме. Запасные: ЛС, а если и туда нельзя — короткая
    подсказка в теме на 5 секунд.
    """
    key = (m.chat.id, topic_key(m), m.from_user.id, kind)
    now = time.monotonic()
    if now - _slow_notified.get(key, 0) < 15:  # не заваливаем уведомлениями
        return
    _slow_notified[key] = now
    original = m.text or m.caption
    if original:
        text += (f"\n{await t(m.chat.id, 'topic.original')}\n"
                 f"<blockquote expandable>{escape(original[:3500])}</blockquote>")
    try:
        return await bot.send_message(m.chat.id, text, message_thread_id=thread_of(m),
                                      ephemeral_message_parameters=ephemeral(m.from_user.id))
    except TelegramAPIError as e:
        log.info("Эфемерное уведомление не отправилось, пробую ЛС: %s", e)
    where = await t(m.chat.id, "topic.dm_where", chat=escape(m.chat.title or ""),
                    topic=escape(topic_name(m.chat.id, topic_key(m)))) + "\n"
    try:
        await bot.send_message(m.from_user.id, where + text)
    except TelegramForbiddenError:
        with suppress(TelegramAPIError):
            hint = await bot.send_message(m.chat.id, await t(m.chat.id, short_key, user=user_mention(m.from_user), left=left),
                                          message_thread_id=thread_of(m))
            later(5, hint.delete())
    except TelegramAPIError as e:
        log.warning("Не удалось предупредить автора: %s", e)


def _is_command(m: Message) -> bool:
    text = m.text or ""
    return len(text) > 1 and text[0] in "/!" and text[1] not in " /!"


class Antispam(BaseMiddleware):
    """Проверяет каждое сообщение в группе до того, как его увидят остальные обработчики."""

    def __init__(self, edited: bool = False):
        self.edited = edited

    async def __call__(self, handler, event: Message, data):
        # эфемерные сообщения (например, команда из меню) никто, кроме бота, не видит — проверять нечего
        if event.chat.type not in ("group", "supergroup") or event.ephemeral_message_id:
            return await handler(event, data)
        bot: Bot = data["bot"]
        await settings.touch(event.chat)
        if event.from_user:
            await remember_user(event.from_user)
        if event.chat.is_forum:
            await remember_topic(event)
        try:
            verdict = await self.check(bot, event)
        except TelegramAPIError as e:
            log.warning("Антиспам не смог проверить сообщение: %s", e)
            verdict = None
        if verdict is None:
            if not self.edited:
                await achievements.notify(bot, event, await helpers.track(bot, event))
            return await handler(event, data)

        action, reason, extra_ids = verdict
        if action in ("slow", "noreply") and _is_command(event):
            # команду в медленной теме тихо убираем, но отвечаем на неё: ответ всё равно увидит только автор
            with suppress(TelegramAPIError):
                await bot.delete_message(event.chat.id, event.message_id)
            return await handler(event, data)
        try:
            if action == "guard":
                return await guard.punish(bot, event, reason)
            if action == "slow":
                left = fmt_duration(reason)
                await tell_author(bot, event, await t(event.chat.id, "topic.slow", left=left), "topic.slow_short",
                                  "slow", left)
            elif action == "noreply":
                await tell_author(bot, event, await t(event.chat.id, "topic.noreply"), "topic.noreply_short", "noreply")
            await bot.delete_messages(event.chat.id, [*extra_ids, event.message_id])
            if action not in ("delete", "slow", "noreply") and event.from_user:
                s = await settings.get(event.chat.id)
                await punish(bot, event.chat, event.from_user.id, user_mention(event.from_user), action,
                             s["mute_minutes"] * 60, reason, thread=thread_of(event))
        except TelegramAPIError as e:
            log.warning("Антиспам не смог наказать (%s): %s", reason, e)

    async def check(self, bot: Bot, m: Message):
        chat = m.chat
        s = await settings.get(chat.id)
        if m.sender_chat:
            # свой чат (анонимный админ) и автопост из привязанного канала не трогаем
            if m.sender_chat.id == chat.id or m.is_automatic_forward:
                return None
            return ("delete", "сообщение от имени канала", []) if s["antichannel"] else None
        user = m.from_user
        if user is None or await is_admin(bot, chat.id, user.id):
            return None

        if not self.edited:
            antibot.remember(chat.id, user.id, m.message_id)
        if s["autoban"]:
            v = await guard.assess(bot, m, s, self.edited)
            if v.action in ("ban", "review"):
                return "guard", v, []
            if v.action == "report":
                await guard.punish(bot, m, v)
        if chat.is_forum and str(topic_key(m)) in s["topic_noreply"]:
            r = m.reply_to_message
            if r and not r.forum_topic_created and (r.sender_chat or (r.from_user and r.from_user.id != user.id)):
                return "noreply", "ответ в теме без ответов", []

        text = (m.text or m.caption or "").lower()
        if s["stopwords"] and any(w in text for w in s["stopwords"]):
            return s["stop_action"], "запрещённое слово", []
        if s["antiforward"] and m.forward_origin:
            return s["spam_action"], "пересылка", []
        if s["antilink"] and any(not _allowed(u, s["link_whitelist"]) for u in _urls(m)):
            return s["spam_action"], "ссылка", []
        if self.edited:
            return None
        now = time.monotonic()
        if s["antiflood"]:
            q = _flood[(chat.id, user.id)]
            q.append((now, m.message_id))
            while q and now - q[0][0] > s["flood_seconds"]:
                q.popleft()
            if len(q) > s["flood_limit"]:
                ids = [i for _, i in q][:-1]
                q.clear()
                return s["flood_action"], "флуд", ids
        slow = s["topic_slow"].get(str(topic_key(m))) if chat.is_forum else None
        if slow:
            key = (chat.id, topic_key(m), user.id)
            last = _last_post.get(key)
            same_album = last and m.media_group_id and last[1] == m.media_group_id
            if last and not same_album and now - last[0] < slow:
                return "slow", max(1, int(slow - (now - last[0]))), []
            _last_post[key] = (now, m.media_group_id)
        return None
