"""Посты с каналов: копии в темах чата по тегам. Правка поста на канале обновляет копию.

Настоящая пересылка не подходит: пересланное сообщение нельзя редактировать. Поэтому бот публикует копию
от своего имени и запоминает в таблице reposts, какая копия какому посту соответствует.
"""
import asyncio
import logging
import re
from collections import defaultdict
from contextlib import suppress
from html import escape

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (Chat, InlineKeyboardMarkup, InputMediaAnimation, InputMediaAudio, InputMediaDocument,
                           InputMediaPhoto, InputMediaVideo, LinkPreviewOptions, Message)

from .. import settings
from ..actions import log
from ..db import db
from ..texts import t

router = Router(name="reposts")
logger = logging.getLogger(__name__)

ALBUM_WAIT = 2  # части альбома приходят отдельными обновлениями, ждём их все
TEXT_LIMIT, CAPTION_LIMIT = 4096, 1024
INPUT_MEDIA = {"photo": InputMediaPhoto, "video": InputMediaVideo, "animation": InputMediaAnimation,
               "document": InputMediaDocument, "audio": InputMediaAudio}
_albums = {}                    # (канал, альбом) -> {номер поста: пост}, пока собираем части
_locks = defaultdict(asyncio.Lock)  # посты одного канала обрабатываем по очереди


def tags_of(m: Message) -> set[str]:
    text = m.text or m.caption or ""
    return {e.extract_from(text)[1:].split("@")[0].lower()
            for e in (m.entities or m.caption_entities or []) if e.type == "hashtag"}


def media_of(m: Message):
    """Вид файла, его file_id и постоянный номер, по которому видно, что файл заменили."""
    for kind in INPUT_MEDIA:
        obj = getattr(m, kind)
        if obj:
            obj = obj[-1] if kind == "photo" else obj
            return kind, obj.file_id, obj.file_unique_id
    return None


def _length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2  # Telegram считает длину в единицах UTF-16


async def body(chat_id: int, m: Message, footer: bool = True) -> str:
    """Текст копии: оформление поста и ссылка на оригинал, если она помещается."""
    raw = m.text or m.caption
    html = m.html_text if raw else ""
    url = m.get_url()
    if not raw or not footer or not url or not (await settings.get(chat_id))["posts_link"]:
        return html
    line = await t(chat_id, "post.footer", channel=escape(m.chat.title or ""))
    line = re.sub(r"</?a\b[^>]*>", "", line)  # вся строка и так ссылка
    limit = CAPTION_LIMIT if m.caption else TEXT_LIMIT
    if _length(raw) + _length(re.sub(r"<[^>]+>", "", line)) + 2 > limit:
        return html
    return f'{html}\n\n<a href="{escape(url)}">{line}</a>'


def buttons(m: Message):
    """Только кнопки-ссылки: кнопки других ботов в чужом сообщении работать не будут."""
    if not m.reply_markup:
        return None
    rows = [[b for b in row if b.url] for row in m.reply_markup.inline_keyboard]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def preview(m: Message) -> LinkPreviewOptions:
    return m.link_preview_options or LinkPreviewOptions(is_disabled=False)


async def _active_chats() -> list[int]:
    return [r["chat_id"] for r in await db.all("SELECT chat_id FROM chats WHERE active=1")]


async def wanted(channel: int, tags: set[str], date: float) -> tuple[dict, bool]:
    """Куда пост должен попасть: {(чат, тема): тег}, и есть ли вообще правила для этого канала.

    Посты старше правила не копируем, иначе исправление опечатки в старом посте вдруг опубликует его в теме.
    """
    targets, known = {}, False
    for chat_id in await _active_chats():
        s = await settings.get(chat_id)
        if not s["posts_on"]:
            continue
        for rule in s["post_rules"]:
            if rule["channel"] == channel:
                known = True
                if rule["tag"] in tags and date >= rule.get("since", 0):
                    targets.setdefault((chat_id, rule["thread"]), rule["tag"])
    return targets, known


async def _save(channel, post_id, target, message_id, tag, album, media):
    await db.execute("INSERT OR REPLACE INTO reposts VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                     channel, post_id, target[0], target[1], message_id, tag, album or "", media)


async def _send_copy(bot: Bot, m: Message, target) -> int:
    chat_id, thread = target
    text = await body(chat_id, m)
    if m.text:
        sent = await bot.send_message(chat_id, text, message_thread_id=thread or None, disable_notification=True,
                                      link_preview_options=preview(m), reply_markup=buttons(m))
        return sent.message_id
    sent = await bot.copy_message(chat_id, m.chat.id, m.message_id, message_thread_id=thread or None,
                                  caption=text or None, show_caption_above_media=m.show_caption_above_media,
                                  disable_notification=True, reply_markup=buttons(m))
    return sent.message_id


async def _publish(bot: Bot, m: Message, target, tag: str):
    message_id = await _send_copy(bot, m, target)
    media = media_of(m)
    await _save(m.chat.id, m.message_id, target, message_id, tag, "", media[2] if media else "")


async def _publish_album(bot: Bot, channel: int, album: str, known: dict, target, tag: str):
    """Копирует альбом целиком: Telegram сам сохраняет группировку и подписи."""
    chat_id, thread = target
    parts = await db.all("SELECT post_id, caption, media FROM channel_albums WHERE channel_id=? AND album=? "
                         "ORDER BY post_id", channel, album)
    ids = [p["post_id"] for p in parts]
    sent = await bot.copy_messages(chat_id, channel, ids, message_thread_id=thread or None, disable_notification=True)
    if len(sent) != len(ids):  # часть альбома удалили на канале: не понять, какая копия какому посту соответствует
        logger.warning("Альбом %s канала %s скопирован не полностью: %s из %s", album, channel, len(sent), len(ids))
    for part, copy in zip(parts, sent):
        await _save(channel, part["post_id"], target, copy.message_id, tag, album, part["media"])
        holder = known.get(part["post_id"]) if part["caption"] else None
        if holder:  # copy_messages не умеет менять подпись, ссылку на оригинал дописываем отдельно
            with suppress(TelegramAPIError):
                await bot.edit_message_caption(chat_id=chat_id, message_id=copy.message_id,
                                               caption=await body(chat_id, holder))


async def _update(bot: Bot, row, m: Message, footer: bool):
    """Приводит копию к текущему виду поста."""
    chat_id, message_id = row["chat_id"], row["message_id"]
    text = await body(chat_id, m, footer)
    media = media_of(m)
    try:
        if m.text and row["media"]:
            # с картинки на чистый текст копию не переделать: Telegram умеет только наоборот
            with suppress(TelegramAPIError):
                await bot.delete_messages(chat_id, [message_id])
            target = (chat_id, row["thread_id"])
            await _save(m.chat.id, m.message_id, target, await _send_copy(bot, m, target), row["tag"], row["album"], "")
        elif m.text:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, link_preview_options=preview(m),
                                        reply_markup=buttons(m))
        elif media and media[2] != row["media"]:
            extra = {}
            if media[0] in ("photo", "video", "animation"):
                extra = {"show_caption_above_media": m.show_caption_above_media, "has_spoiler": m.has_media_spoiler}
            await bot.edit_message_media(chat_id=chat_id, message_id=message_id, reply_markup=buttons(m),
                                         media=INPUT_MEDIA[media[0]](media=media[1], caption=text or None, **extra))
            await db.execute("UPDATE reposts SET media=? WHERE channel_id=? AND post_id=? AND chat_id=? AND thread_id=?",
                             media[2], m.chat.id, m.message_id, chat_id, row["thread_id"])
        else:
            await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text or None,
                                           show_caption_above_media=m.show_caption_above_media, reply_markup=buttons(m))
    except TelegramBadRequest as e:
        problem = e.message.lower()
        if "not modified" in problem:
            return
        if "not found" not in problem:
            raise
        # админ удалил копию в теме: запоминаем это, чтобы следующая правка поста не опубликовала её снова
        await db.execute("UPDATE reposts SET message_id=0 WHERE channel_id=? AND post_id=? AND chat_id=? AND thread_id=?",
                         m.chat.id, m.message_id, chat_id, row["thread_id"])


async def _remove(bot: Bot, channel: int, items):
    chat_id, thread = items[0]["chat_id"], items[0]["thread_id"]
    ids = [r["message_id"] for r in items if r["message_id"]]
    if ids:
        with suppress(TelegramAPIError):
            await bot.delete_messages(chat_id, ids)
    for r in items:
        await db.execute("DELETE FROM reposts WHERE channel_id=? AND post_id=? AND chat_id=? AND thread_id=?",
                         channel, r["post_id"], chat_id, thread)


async def _report(bot: Bot, chat_id: int, m: Message, e: TelegramAPIError):
    logger.warning("Пост %s канала %s не дошёл до чата %s: %s", m.message_id, m.chat.id, chat_id, e)
    row = await db.one("SELECT title FROM chats WHERE chat_id=?", chat_id)
    chat = Chat(id=chat_id, type="supergroup", title=row["title"] if row else None)
    await log(bot, chat, f"⚠️ Пост с канала «{escape(m.chat.title or '')}» не удалось опубликовать или обновить: "
                         f"<code>{escape(e.message)}</code>", fallback_to_owners=True)


async def sync(bot: Bot, posts: list[Message]):
    """Один пост или части альбома: публикует, обновляет или убирает копии по текущим тегам."""
    first = posts[0]
    channel, album = first.chat.id, first.media_group_id
    async with _locks[channel]:
        if album:
            for p in posts:
                media = media_of(p)
                await db.execute("INSERT OR REPLACE INTO channel_albums VALUES(?, ?, ?, ?, ?, ?)", channel, album,
                                 p.message_id, int(bool(p.caption)), " ".join(sorted(tags_of(p))), media[2] if media else "")
            parts = await db.all("SELECT post_id, caption, tags FROM channel_albums WHERE channel_id=? AND album=?",
                                 channel, album)
            tags = {tag for part in parts for tag in part["tags"].split()}
            holders = [part["post_id"] for part in parts if part["caption"]]
            rows = await db.all("SELECT * FROM reposts WHERE channel_id=? AND album=?", channel, album)
        else:
            tags, holders = tags_of(first), []
            rows = await db.all("SELECT * FROM reposts WHERE channel_id=? AND post_id=?", channel, first.message_id)
        targets, known = await wanted(channel, tags, first.date.timestamp())
        if not known and not rows:
            return
        existing = defaultdict(list)
        for row in rows:
            existing[(row["chat_id"], row["thread_id"])].append(row)
        for target, items in existing.items():
            if not (await settings.get(target[0]))["posts_on"]:
                continue
            tag = items[0]["tag"]
            try:
                if tag not in tags:
                    if target not in targets:  # тег убрали с поста — копия больше не нужна
                        await _remove(bot, channel, items)
                        continue
                    for r in items:
                        await db.execute("UPDATE reposts SET tag=? WHERE channel_id=? AND post_id=? AND chat_id=? AND thread_id=?",
                                         targets[target], channel, r["post_id"], target[0], target[1])
                for p in posts:
                    for r in items:
                        if r["post_id"] == p.message_id and r["message_id"]:
                            await _update(bot, r, p, not album or p.message_id == min(holders, default=0))
            except TelegramAPIError as e:
                await _report(bot, target[0], first, e)
        known_posts = {p.message_id: p for p in posts}
        for target, tag in targets.items():
            if target in existing:
                continue
            try:
                if album:
                    await _publish_album(bot, channel, album, {min(holders): known_posts[min(holders)]}
                                         if holders and min(holders) in known_posts else {}, target, tag)
                else:
                    await _publish(bot, first, target, tag)
            except TelegramAPIError as e:
                await _report(bot, target[0], first, e)


async def _flush_album(bot: Bot, key):
    await asyncio.sleep(ALBUM_WAIT)
    await sync(bot, sorted(_albums.pop(key).values(), key=lambda p: p.message_id))


@router.channel_post()
async def on_post(post: Message, bot: Bot):
    if not post.media_group_id:
        return await sync(bot, [post])
    key = (post.chat.id, post.media_group_id)
    if key not in _albums:
        _albums[key] = {}
        asyncio.create_task(_flush_album(bot, key))
    _albums[key][post.message_id] = post


@router.edited_channel_post()
async def on_edit(post: Message, bot: Bot):
    key = (post.chat.id, post.media_group_id)
    if key in _albums:  # альбом ещё не опубликован: копия сразу выйдет с правкой
        _albums[key][post.message_id] = post
        return
    await sync(bot, [post])
