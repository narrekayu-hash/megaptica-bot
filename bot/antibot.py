"""Распознавание спам-ботов по «меткам», сохранённым командой /бот.

Метки бывают нескольких видов:
  text    — нормализованный текст спама (ловит копии даже с подменой букв и эмодзи);
  link    — t.me/канал или домен сайта из ссылок и кнопок;
  mention — @юзернейм из текста;
  media   — конкретная картинка, видео, стикер или файл;
  forward — канал/чат, из которого переслано.
"""
import re
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from aiogram.types import Message

from .db import db

# Домены, по которым банить нельзя никогда — ими пользуются все
SAFE_HOSTS = {"youtube.com", "youtu.be", "google.com", "telegram.org", "wikipedia.org", "github.com"}
TG_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
# латинские буквы, которыми спамеры подменяют кириллицу (и наоборот)
_HOMO = str.maketrans("aeopcxykmthbё", "аеорсхукмтнве")
MIN_TEXT = 25          # короче — не сравниваем, слишком много совпадений с нормальными сообщениями
SIMILARITY = 0.6       # доля общих кусочков текста, начиная с которой считаем сообщение копией


def normalize(text: str) -> str:
    text = re.sub(r"https?://\S+|t\.me/\S+|@\w+", " ", text.lower())  # ссылки — отдельные метки
    return re.sub(r"[^\w]+", " ", text.translate(_HOMO)).strip()


def shingles(norm: str) -> frozenset:
    s = norm.replace(" ", "")
    return frozenset(s[i:i + 5] for i in range(max(1, len(s) - 4)))


def _link(url: str):
    p = urlparse(url if "://" in url else "http://" + url)
    host = (p.hostname or "").removeprefix("www.")
    if not host:
        return None
    if host in TG_HOSTS:
        part = p.path.strip("/").split("/")[0].lower()
        return f"t.me/{part}" if part else None
    return host


def _links(m: Message):
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


def features(m: Message, whitelist=()):
    """Все метки, которые можно достать из сообщения: список (вид, значение)."""
    out = []
    text = m.text or m.caption or ""
    norm = normalize(text)
    if len(norm) >= MIN_TEXT:
        out.append(("text", norm))
    for url in _links(m):
        v = _link(url)
        if v and v not in SAFE_HOSTS and not any(v == d or v.endswith("." + d) for d in whitelist):
            out.append(("link", v))
    for e in m.entities or m.caption_entities or []:
        if e.type == "mention":
            out.append(("mention", e.extract_from(text).lower()))
    media = (m.photo[-1] if m.photo else None) or m.video or m.animation or m.sticker or m.document
    if media:
        out.append(("media", media.file_unique_id))
    origin = m.forward_origin
    chat = origin and (getattr(origin, "chat", None) or getattr(origin, "sender_chat", None))
    if chat:
        out.append(("forward", str(chat.id)))
    return list(dict.fromkeys(out))


# --- кэш меток в памяти ---
_exact = None   # {(вид, значение): id}
_texts = None   # [(id, shingles)]


async def _load():
    global _exact, _texts
    rows = await db.markers()
    _exact = {(r["kind"], r["value"]): r["id"] for r in rows if r["kind"] != "text"}
    _texts = [(r["id"], shingles(r["value"])) for r in rows if r["kind"] == "text"]


def invalidate():
    global _exact
    _exact = None


async def match(m: Message, whitelist=()):
    """Возвращает (вид, значение, id метки) первой совпавшей метки или None."""
    if _exact is None:
        await _load()
    if not _exact and not _texts:
        return None
    for kind, value in features(m, whitelist):
        if kind == "text":
            sh = shingles(value)
            for marker_id, other in _texts:
                if len(sh & other) / max(1, len(sh | other)) >= SIMILARITY:
                    return kind, value, marker_id
        elif (kind, value) in _exact:
            return kind, value, _exact[(kind, value)]
    return None


async def learn(m: Message, whitelist=()) -> int:
    """Сохраняет метки из сообщения спамера. Возвращает, сколько новых добавилось."""
    added = 0
    for kind, value in features(m, whitelist):
        added += await db.add_marker(kind, value)
    invalidate()
    return added


# --- последние сообщения каждого участника, чтобы /бот мог удалить всё за 48 часов ---
_recent = defaultdict(lambda: deque(maxlen=300))  # (chat_id, user_id) -> [(время, message_id)]
_WINDOW = 48 * 3600


def remember(chat_id: int, user_id: int, message_id: int):
    _recent[(chat_id, user_id)].append((time.time(), message_id))


def recent(chat_id: int, user_id: int):
    now = time.time()
    return [i for t, i in _recent.pop((chat_id, user_id), ()) if now - t < _WINDOW]
