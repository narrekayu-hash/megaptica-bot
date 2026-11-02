"""Ачивки: за что даются, как считаются и как их показать."""
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram.types import Message, User

from . import settings, sky
from .db import db
from .texts import t
from .utils import mention


@dataclass(frozen=True)
class Ach:
    code: str
    title: str
    desc: str
    need: int = 1
    kind: str = "auto"  # auto — бот выдаёт сам, manual — выдают админы, secret — условие скрыто
    rare: bool = False  # с такой поздравляем публично
    topic: str = ""     # без темы с такой ролью ачивку не получить: help | friends | chat | photo


BUILTIN = [
    Ach("first", "🕯 Первый свет", "написать первое сообщение в чате"),
    Ach("wings", "🪽 Крылья расправлены", "написать в трёх разных темах", 3),
    Ach("flame", "🔥 Свой огонёк", "писать в чате 10 разных дней за месяц", 10),
    Ach("oldtimer", "🌳 Старожил", "полгода в чате и всё ещё пишет", 182, rare=True),
    Ach("hint", "💡 Подсказка", "первый засчитанный ответ на чужой вопрос", topic="help"),
    Ach("elder", "🗿 Старейшина", "25 ответов на чужие вопросы", 25, topic="help"),
    Ach("keeper", "📚 Хранитель знаний", "100 ответов на чужие вопросы", 100, rare=True, topic="help"),
    Ach("week", "🏅 Помощник недели", "попасть в тройку помощников недели", topic="help"),
    Ach("thrice", "🌟 Свет трёх недель", "три раза попасть в тройку помощников недели", 3, rare=True, topic="help"),
    Ach("seek", "🔎 Ищу свет", "первое объявление в поиске друзей", topic="friends"),
    Ach("hand", "🤝 Протянутая рука", "ответить на 5 чужих объявлений в поиске друзей", 5, topic="friends"),
    Ach("fire", "🏕 У костра", "писать в теме для общения 7 дней подряд", 7, topic="chat"),
    Ach("reset", "🔄 Встреча сброса", "первым написать в чате после сброса дня в Sky"),
    Ach("night", "🌙 Ночной полёт", "написать сообщение между 3 и 5 часами ночи"),
    Ach("photo", "📸 Фотограф Долины", "10 фото в теме для фото", 10, topic="photo"),
    Ach("star", "✨ Звезда галереи", "фото, на которое отреагировали 20 человек", 20, rare=True, topic="photo"),
    Ach("chronicler", "📜 Летописец", "за хороший гайд", kind="manual"),
    Ach("artist", "🎨 Художник", "за фан-арт", kind="manual"),
    Ach("host", "🎪 Ведущий", "за встречу или событие, которое провёл участник", kind="manual"),
    Ach("heart", "💛 Тёплое сердце", "за помощь новичку или поддержку", kind="manual"),
    Ach("crab", "🦀 Крабик", "первым ответить новичку на его самое первое сообщение", kind="secret", rare=True),
    Ach("candle", "🏮 Свеча в темноте", "ответить на вопрос, который ждал ответа больше часа", kind="secret",
        rare=True, topic="help"),
]
ROLES = {"photo": "📸 фото", "friends": "🤝 поиск друзей", "chat": "🔥 общение"}
STAR_REACTIONS = 20
PHOTOS_PER_HOUR = 3   # чтобы ачивку за фото не набивали пачкой картинок
NEWCOMER_DAYS = 7
_launch_day = datetime.now(sky.SKY_TZ).date()  # в день запуска «первым после сброса» быть нельзя: сброс бот не видел


def catalog(s: dict) -> list[Ach]:
    """Все ачивки чата: встроенные (с учётом переименований) и свои."""
    names = s["ach_names"]
    items = [replace(a, title=names.get(a.code, a.title)) for a in BUILTIN]
    items += [Ach(code, d["t"], d["d"], kind="manual") for code, d in s["ach_custom"].items()]
    return items


def available(a: Ach, s: dict) -> bool:
    """Ачивку можно получить: она включена, и в чате есть нужные для неё темы."""
    if a.code in s["ach_off"]:
        return False
    if a.topic == "help":
        return bool(s["helpers"] and s["helper_topics"])
    return not a.topic or a.topic in s["ach_topics"].values()


def enabled(s: dict) -> dict[str, Ach]:
    return {a.code: a for a in catalog(s) if available(a, s)}


async def give(chat_id: int, uid: int, name: str, codes, s: dict) -> list[Ach]:
    """Выдаёт ачивки, которых у человека ещё нет. Возвращает новые."""
    cat, new = enabled(s), []
    for code in dict.fromkeys(codes):
        a = cat.get(code)
        # редкие ждут публичного поздравления; выданные админом поздравляются сразу при выдаче
        quiet = a is None or a.kind == "manual" or not (a.rare and s["ach_announce"])
        if a and await db.ach_add(chat_id, uid, code, name, announced=quiet):
            new.append(a)
    return new


def _today(s: dict) -> date:
    return datetime.now(timezone.utc).astimezone(ZoneInfo(s["sky_tz"])).date()


async def _streak(chat_id: int, uid: int, kind: str, today: date) -> int:
    days = await db.seen_refs(chat_id, uid, kind, (today - timedelta(days=30)).isoformat())
    n = 0
    while (today - timedelta(days=n)).isoformat() in days:
        n += 1
    return n


async def _days_in_chat(chat_id: int, uid: int) -> int:
    first = await db.seen_ts(chat_id, uid, "first", "")
    joined = await db.joined_at(chat_id, uid)
    since = min(t for t in (first, joined, time.time()) if t)
    return int((time.time() - since) // 86400)


async def track(m: Message, helped: bool) -> list[Ach]:
    """Смотрит на принятое сообщение и выдаёт заработанные ачивки. helped — ответ засчитан «Помощнику недели»."""
    s = await settings.get(m.chat.id)
    user = m.from_user
    if not s["achievements"] or not user or user.is_bot or m.sender_chat:
        return []
    text = (m.text or m.caption or "").strip()
    media = m.photo or m.video or m.animation or m.sticker or m.voice or m.video_note or m.document or m.audio
    if not media and (len(text) < 3 or text[0] in "/!"):
        return []  # «+», «ок» и команды не считаем
    c, uid, now = m.chat.id, user.id, time.time()
    today = _today(s)
    key = str(m.message_thread_id if m.is_topic_message else 0)
    role = s["ach_topics"].get(key)
    r = m.reply_to_message
    if r and r.forum_topic_created:
        r = None  # в форуме «ответ» на создание темы — просто сообщение в теме
    got = []

    if await db.seen_add(c, uid, "first", "", now):
        got.append("first")
        joined = await db.joined_at(c, uid)
        if joined and now - joined < NEWCOMER_DAYS * 86400:
            await db.seen_add(c, uid, "newbie", str(m.message_id), now)
    if await db.seen_add(c, uid, "topic", key, now) and await db.seen_count(c, uid, "topic") >= 3:
        got.append("wings")
    if await db.seen_add(c, uid, "day", today.isoformat(), now):
        if await db.seen_count(c, uid, "day", (today - timedelta(days=29)).isoformat()) >= 10:
            got.append("flame")
        if await _days_in_chat(c, uid) >= 182:
            got.append("oldtimer")
    la_day = datetime.now(sky.SKY_TZ).date()
    if la_day != _launch_day and await db.seen_add(c, 0, "reset", la_day.isoformat(), now):
        got.append("reset")
    if 3 <= datetime.now(timezone.utc).astimezone(ZoneInfo(s["sky_tz"])).hour < 5:
        got.append("night")

    if r and r.from_user and r.from_user.id != uid:
        if await db.seen_owner(c, "newbie", str(r.message_id)) == r.from_user.id \
                and await db.seen_add(c, 0, "crab", str(r.message_id), now):
            got.append("crab")
        if helped:
            n = await db.help_count(c, uid)
            got += [code for code, need in (("hint", 1), ("elder", 25), ("keeper", 100)) if n >= need]
            if (m.date - r.date).total_seconds() >= 3600:
                got.append("candle")

    if role == "friends":
        if not r:
            got.append("seek")
        elif r.from_user and r.from_user.id != uid and len(text) >= 8:
            if await db.seen_add(c, uid, "friend", str(r.message_id), now) and await db.seen_count(c, uid, "friend") >= 5:
                got.append("hand")
    elif role == "chat":
        if await db.seen_add(c, uid, "chatday", today.isoformat(), now) and await _streak(c, uid, "chatday", today) >= 7:
            got.append("fire")
    elif role == "photo" and m.photo:
        await db.seen_add(c, uid, "pic", str(m.message_id), now)  # чтобы потом найти автора по реакциям
        post = m.media_group_id or str(m.message_id)               # альбом — один пост
        if await db.seen_count(c, uid, "photo", min_ts=now - 3600) < PHOTOS_PER_HOUR \
                and await db.seen_add(c, uid, "photo", post, now) and await db.seen_count(c, uid, "photo") >= 10:
            got.append("photo")

    return await give(c, uid, user.full_name, got, s) if got else []


async def week_top(chat_id: int, user_id: int, name: str, week: str, s: dict) -> list[Ach]:
    """Попал(а) в тройку «Помощника недели»."""
    await db.seen_add(chat_id, user_id, "top3", week, time.time())
    codes = ["week"] + (["thrice"] if await db.seen_count(chat_id, user_id, "top3") >= 3 else [])
    return await give(chat_id, user_id, name, codes, s)


async def reaction(chat_id: int, message_id: int, reactor: User | None, added: bool, total: int | None):
    """Реакция на фото. reactor/added — кто поставил или убрал реакцию; total — общее число (анонимные реакции).

    Возвращает автора фото, если реакций набралось на «Звезду галереи», иначе None.
    """
    s = await settings.get(chat_id)
    if not s["achievements"]:
        return None
    ref = str(message_id)
    author = await db.seen_owner(chat_id, "pic", ref)
    if author is None:
        return None
    if reactor is not None:
        if reactor.id == author or reactor.is_bot:
            return None
        if added:
            await db.seen_add(chat_id, reactor.id, "react", ref, time.time())
        else:
            await db.seen_del(chat_id, reactor.id, "react", ref)
        total = await db.seen_ref_count(chat_id, "react", ref)
    return author if (total or 0) >= STAR_REACTIONS else None


async def progress(chat_id: int, uid: int, s: dict) -> dict[str, int]:
    """Насколько человек близок к ачивкам со счётчиком."""
    today = _today(s)
    helps = await db.help_count(chat_id, uid)
    top3 = await db.seen_count(chat_id, uid, "top3")
    return {
        "wings": await db.seen_count(chat_id, uid, "topic"),
        "flame": await db.seen_count(chat_id, uid, "day", (today - timedelta(days=29)).isoformat()),
        "oldtimer": await _days_in_chat(chat_id, uid),
        "elder": helps, "keeper": helps, "thrice": top3,
        "hand": await db.seen_count(chat_id, uid, "friend"),
        "fire": await _streak(chat_id, uid, "chatday", today),
        "photo": await db.seen_count(chat_id, uid, "photo"),
    }


async def collection_text(chat_id: int, uid: int, name: str, own: bool) -> str:
    s = await settings.get(chat_id)
    if not s["achievements"]:
        return await t(chat_id, "ach.off")
    cat = {a.code: a for a in catalog(s)}
    have = [cat[r["code"]] for r in await db.ach_of(chat_id, uid) if r["code"] in cat]
    got = {a.code for a in have}
    open_ = [a for a in enabled(s).values() if a.kind != "secret"]
    secrets = [a for a in enabled(s).values() if a.kind == "secret"]
    who = "Твои ачивки" if own else f"Ачивки {mention(uid, name)}"
    lines = [f"🏆 <b>{who}</b>: {len(have)}"]
    if not own:
        lines += [f"✅ {a.title} — {a.desc}" for a in have] or ["Пока ни одной."]
        return "\n".join(lines)
    prog = await progress(chat_id, uid, s)
    for title, kinds in (("", ("auto",)), ("\n🎖 <b>Выдают админы</b>", ("manual",))):
        if title:
            lines.append(title)
        for a in open_:
            if a.kind not in kinds:
                continue
            if a.code in got:
                lines.append(f"✅ {a.title}")
            else:
                unit = " дней" if a.code == "oldtimer" else ""
                count = f" ({min(prog[a.code], a.need)}/{a.need}{unit})" if a.need > 1 and a.code in prog else ""
                lines.append(f"▫️ {a.title} — {a.desc}{count}")
    if secrets:
        found = [a for a in secrets if a.code in got]
        lines.append(f"\n❓ <b>Секретные</b>: найдено {len(found)} из {len(secrets)}")
        lines += [f"✅ {a.title} — {a.desc}" for a in found]
    return "\n".join(lines)


async def congrats(chat_id: int, items: list[Ach]) -> str:
    return await t(chat_id, "ach.new", list="\n".join(f"<b>{a.title}</b> — {a.desc}" for a in items))
