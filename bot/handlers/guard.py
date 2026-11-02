"""Умный антиспам: складывает баллы подозрительности и решает — бан, карточка админам или ничего.

Банит только новичков. Давних участников никогда не трогает, а при очень высоких баллах присылает
админам карточку. Кнопки «🤖 Бот / 🙋 Человек» в карточках дообучают словарь спама.
"""
import asyncio
import itertools
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from html import escape

import aiohttp
from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import antibot, spamscore
from ..actions import MARKER_NAMES, delete_recent, log, spam_ban
from ..db import db
from ..texts import t
from ..utils import MUTED, can_restrict, default_permissions, user_mention

router = Router(name="guard")
logger = logging.getLogger(__name__)

# (бан, карточка) для каждого уровня строгости
LEVELS = {"soft": (12, 8), "normal": (10, 6), "strict": (8, 5)}
LEVEL_NAMES = {"soft": "мягко", "normal": "обычно", "strict": "строго"}
REVIEW_MUTE = 24 * 3600  # пока админы не решили, подозреваемый в муте на сутки


@dataclass
class Verdict:
    action: str                 # ban | review | report | ok
    score: int
    reasons: list = field(default_factory=list)
    newcomer: bool = False
    marker_id: int = 0


# --- профиль и база CAS (только для новичков, с кэшем) ---
_profiles = {}  # user_id -> (когда, признаки)
_cas = {}       # user_id -> (когда, в базе ли)


async def profile_signals(bot: Bot, uid: int):
    hit = _profiles.get(uid)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    signals = []
    try:
        info = await bot.get_chat(uid)
        ch = info.personal_chat
        signals = spamscore.profile(info.bio or "", ch.title if ch else "", ch.username if ch else "")
    except Exception as e:  # профиль — лишь подсказка, его недоступность не должна мешать модерации
        logger.info("Не удалось посмотреть профиль %s: %s", uid, e)
    _profiles[uid] = (time.time(), signals)
    return signals


async def in_cas(uid: int) -> bool:
    hit = _cas.get(uid)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    listed = False
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
            async with s.get("https://api.cas.chat/check", params={"user_id": uid}) as r:
                listed = bool((await r.json(content_type=None)).get("ok"))
    except Exception as e:  # база недоступна — просто не учитываем её
        logger.info("CAS не ответил: %s", e)
        return False
    _cas[uid] = (time.time(), listed)
    return listed


async def assess(bot: Bot, m: Message, s: dict, edited: bool) -> Verdict:
    c, uid = m.chat.id, m.from_user.id
    joined = await db.joined_at(c, uid)
    days = s["autoban_days"]
    now = time.time()
    newcomer = days == 0 or bool(joined and now - joined < days * 86400)
    first = not edited and await db.seen_add(c, uid, "posted", "", now)
    reasons = spamscore.content(m, s["link_whitelist"])
    text = spamscore.text_of(m)
    reasons += await spamscore.vocabulary(text)
    marker_id = 0
    hit = await antibot.match(m, s["link_whitelist"])
    if hit:
        kind, value, marker_id = hit
        reasons.append((6, f"совпала метка /бот — {MARKER_NAMES.get(kind, kind)}: {value[:40]}"))
    if newcomer:
        if not edited and first:
            reasons += await profile_signals(bot, uid)
        if s["cas"] and await in_cas(uid):
            reasons.append((10, "в общей базе спамеров CAS"))
    score = sum(p for p, _ in reasons)
    if newcomer and score >= 4:  # усилители — только когда в сообщении уже что-то есть
        if joined:
            reasons.append((2, "в чате меньше суток") if now - joined < 86400 else (1, "недавно в чате"))
            if now - joined < 600:
                reasons.append((1, "пишет сразу после входа"))
        if first:
            reasons.append((1, "первое сообщение в чате"))
        score = sum(p for p, _ in reasons)
    ban, review = LEVELS[s["spam_level"]]
    if newcomer:
        action = "ban" if score >= ban else "review" if score >= review else "ok"
    else:
        action = "report" if score >= ban else "ok"
        if score < 3 and not edited:
            await spamscore.learn(text, spam=False)  # обычное сообщение давнего участника — пример нормы
    return Verdict(action, score, reasons, newcomer, marker_id)


# --- карточки для админов ---
class Rev(CallbackData, prefix="rv"):
    """a: ban — это бот, забанить; ok — это человек, размутить/разбанить; c — чат; u — участник; r — запись;
    m — само подозрительное сообщение, если оно ещё в чате."""
    a: str
    c: int
    u: int
    r: int = 0
    m: int = 0


_texts = {}  # номер карточки -> (текст сообщения, id метки), чтобы кнопка могла дообучить словарь
_ids = itertools.count(1)


def _remember(text: str, marker_id: int) -> int:
    rid = next(_ids)
    _texts[rid] = (text, marker_id)
    if len(_texts) > 1000:
        _texts.pop(next(iter(_texts)))
    return rid


def _why(v: Verdict) -> str:
    lines = [f"• {escape(why)} (+{p})" for p, why in sorted(v.reasons, key=lambda x: -x[0])]
    return f"Баллы: <b>{v.score}</b>\n" + "\n".join(lines)


def _quote(m: Message) -> str:
    text = spamscore.text_of(m)
    return f"\n<blockquote expandable>{escape(text[:800])}</blockquote>" if text else ""


async def punish(bot: Bot, m: Message, v: Verdict):
    """Применяет решение: бан, мут с карточкой или карточку без наказания."""
    user, chat = m.from_user, m.chat
    rid = _remember(spamscore.text_of(m), v.marker_id)
    who = f"{user_mention(user)} (ID <code>{user.id}</code>)"
    if v.action == "ban":
        n = await spam_ban(bot, chat, user.id, user_mention(user), [m.message_id], log_it=False)
        await spamscore.learn(spamscore.text_of(m), spam=True)
        text = await t(chat.id, "guard.ban", user=who, deleted=n, reasons=_why(v), message=_quote(m))
        rows = [[InlineKeyboardButton(text=await t(chat.id, "guard.btn_unban"),
                                      callback_data=Rev(a="ok", c=chat.id, u=user.id, r=rid).pack())]]
    elif v.action == "review":
        with suppress(TelegramAPIError):
            await bot.delete_messages(chat.id, [m.message_id])
        with suppress(TelegramAPIError):
            await bot.restrict_chat_member(chat.id, user.id, MUTED, until_date=timedelta(seconds=REVIEW_MUTE))
        text = await t(chat.id, "guard.review", user=who, reasons=_why(v), message=_quote(m))
        rows = [[InlineKeyboardButton(text=await t(chat.id, "guard.btn_ban"),
                                      callback_data=Rev(a="ban", c=chat.id, u=user.id, r=rid).pack()),
                 InlineKeyboardButton(text=await t(chat.id, "guard.btn_human"),
                                      callback_data=Rev(a="ok", c=chat.id, u=user.id, r=rid).pack())]]
    else:  # report: давний участник, бот его не трогает
        key = (chat.id, user.id)
        if time.time() - _reported.get(key, 0) < 3600:
            return
        _reported[key] = time.time()
        text = await t(chat.id, "guard.report", user=who, reasons=_why(v), message=_quote(m))
        rows = [[InlineKeyboardButton(text=await t(chat.id, "guard.btn_ban"),
                                      callback_data=Rev(a="ban", c=chat.id, u=user.id, r=rid, m=m.message_id).pack()),
                 InlineKeyboardButton(text=await t(chat.id, "guard.btn_ok"),
                                      callback_data=Rev(a="ok", c=chat.id, u=user.id, r=rid).pack())]]
    await log(bot, chat, text, InlineKeyboardMarkup(inline_keyboard=rows), fallback_to_owners=True)


_reported = {}


@router.callback_query(Rev.filter())
async def on_review(cb: CallbackQuery, callback_data: Rev, bot: Bot):
    d = callback_data
    if not await can_restrict(bot, cb.from_user.id, d.c):
        return await cb.answer(await t(d.c, "guard.no_rights"), show_alert=True)
    text, marker_id = _texts.pop(d.r, ("", 0))
    try:
        if d.a == "ban":
            await bot.ban_chat_member(d.c, d.u, revoke_messages=True)
            await delete_recent(bot, d.c, d.u, [d.m])
            await spamscore.learn(text, spam=True)
            done = await t(d.c, "guard.done_ban")
        else:
            await bot.unban_chat_member(d.c, d.u, only_if_banned=True)
            with suppress(TelegramAPIError):  # если человек уже не в чате, снимать мут не с кого
                await bot.restrict_chat_member(d.c, d.u, await default_permissions(bot, d.c))
            await spamscore.learn(text, spam=False)
            done = await t(d.c, "guard.done_human")
            if marker_id:  # метка поймала человека — она плохая
                await db.del_marker(marker_id)
                antibot.invalidate()
                done += ", метка удалена"
    except TelegramAPIError as e:
        return await cb.answer(f"Не получилось: {e.message}", show_alert=True)
    await cb.answer(await t(d.c, "guard.saved"))
    with suppress(TelegramAPIError):
        await cb.message.edit_text(cb.message.html_text + f"\n\n{done} — {user_mention(cb.from_user)}", reply_markup=None)


async def saver():
    """Раз в минуту записывает выученные слова в базу."""
    while True:
        await asyncio.sleep(60)
        try:
            await spamscore.flush()
        except Exception:
            logger.exception("Не удалось сохранить словарь спама")
