"""«Помощник недели»: благодарим участников, которые отвечают на чужие вопросы."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.filters import Filter
from aiogram.types import Chat, Message

from .. import achievements, settings
from ..actions import announce, log
from ..db import db
from ..texts import t
from ..utils import is_admin, is_moderator, mention
from .content import answer_privately

router = Router(name="helpers")
logger = logging.getLogger(__name__)
NAMES = {"helpers": "top", "помощники": "top", "helperhere": "here"}
MEDALS = ["🥇", "🥈", "🥉"]


def week_of(local: datetime) -> str:
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


def answers_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "ответ"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "ответа"
    return "ответов"


async def track(bot: Bot, m: Message) -> bool:
    """Засчитывает ответ, если это реплай на чужой вопрос в отмеченной теме. True — засчитан впервые."""
    s = await settings.get(m.chat.id)
    user, q = m.from_user, m.reply_to_message
    if not s["helpers"] or not user or user.is_bot or not q or q.forum_topic_created:
        return False
    if str(m.message_thread_id if m.is_topic_message else 0) not in s["helper_topics"]:
        return False
    if not q.from_user or q.from_user.id == user.id or q.from_user.is_bot or "?" not in (q.text or q.caption or ""):
        return False
    if len((m.text or m.caption or "").strip()) < 8 and not (m.photo or m.video or m.animation):
        return False  # «да», «+», «хз» ответом не считаем
    if await is_admin(bot, m.chat.id, user.id):
        return False
    week = week_of(datetime.now(timezone.utc).astimezone(ZoneInfo(s["sky_tz"])))
    return await db.add_help(m.chat.id, user.id, user.full_name, q.message_id, week)


async def standings_text(chat_id: int, week: str, limit: int) -> str:
    rows = await db.help_top(chat_id, week, limit)
    if not rows:
        return await t(chat_id, "help.empty")
    lines = [await t(chat_id, "help.standings")]
    for i, r in enumerate(rows):
        place = MEDALS[i] if i < 3 else f"{i + 1}."
        lines.append(f"{place} {mention(r['user_id'], r['name'])} — {r['n']} {answers_word(r['n'])}")
    return "\n".join(lines)


class HelperCmd(Filter):
    async def __call__(self, message: Message, bot: Bot):
        text = message.text or ""
        if len(text) < 2 or text[0] not in "/!":
            return False
        name, _, at = text[1:].split(maxsplit=1)[0].lower().partition("@")
        if at and at != (await bot.me()).username.lower():
            return False
        cmd = NAMES.get(name)
        return {"helper_cmd": cmd} if cmd else False


@router.message(HelperCmd())
async def on_helpers(message: Message, bot: Bot, helper_cmd: str):
    chat = message.chat
    s = await settings.get(chat.id)
    if helper_cmd == "here":
        if not await is_moderator(bot, message):
            return
        await settings.put(chat.id, "helper_post_topic", str(message.message_thread_id if message.is_topic_message else 0))
        return await message.reply(await t(chat.id, "help.here"))
    if not s["helpers"]:
        return
    week = week_of(datetime.now(timezone.utc).astimezone(ZoneInfo(s["sky_tz"])))
    text = await standings_text(chat.id, week, 10)
    await answer_privately(bot, message, json.dumps({"kind": "text", "text": text}), f"hlp{chat.id}")


async def announce_week(bot: Bot, chat_id: int, week: str, s: dict):
    rows = [r for r in await db.help_top(chat_id, week, 10) if r["n"] >= s["helper_min"]]
    row = await db.one("SELECT title FROM chats WHERE chat_id=?", chat_id)
    chat = Chat(id=chat_id, type="supergroup", title=row["title"] if row else str(chat_id))
    if rows:
        lines = [await t(chat_id, "help.title"), ""]
        for i, r in enumerate(rows[:3]):
            lines.append(await t(chat_id, "help.line", medal=MEDALS[i], user=mention(r["user_id"], r["name"]),
                                 answers=f"{r['n']} {answers_word(r['n'])}"))
        for r in rows[:3]:
            await achievements.week_top(chat_id, r["user_id"], r["name"], week, s)
        if achievements.enabled(s).get("week"):
            lines += ["", await t(chat_id, "help.ach", achievement=achievements.enabled(s)["week"].title)]
        lines += ["", await t(chat_id, "help.footer")]
        topic = s["helper_post_topic"] or s["helper_topics"][0]
        await announce(bot, chat_id, "\n".join(lines), int(topic) or None)
    # полный список — только админам: из кого звать помощников и ведущих
    full = await standings_text(chat_id, week, 10)
    await log(bot, chat, full + "\n\nЭтим людям можно предложить стать помощниками или ведущими событий.",
              fallback_to_owners=True)


async def weekly(bot: Bot):
    """Раз в минуту: в воскресенье после 20:00 (по часовому поясу чата) подводим итоги, один раз за неделю."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            for row in await db.all("SELECT chat_id FROM chats WHERE active=1"):
                s = await settings.get(row["chat_id"])
                if not s["helpers"] or not s["helper_topics"]:
                    continue
                local = now.astimezone(ZoneInfo(s["sky_tz"]))
                week = week_of(local)
                if local.isoweekday() != 7 or local.hour < 20 or s["helper_last_week"] == week:
                    continue
                await settings.put(row["chat_id"], "helper_last_week", week)  # сначала отмечаем, чтобы не повториться
                await announce_week(bot, row["chat_id"], week, s)
        except Exception:
            logger.exception("Ошибка в итогах «Помощника недели»")
        await asyncio.sleep(60)
