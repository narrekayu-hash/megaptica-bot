from contextlib import suppress
from html import escape

from aiogram import Bot, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from .. import settings
from .. import antibot, spamscore
from ..actions import Undo, admin_line, log, punish, spam_ban, warn
from ..texts import t
from ..db import db
from ..utils import (can_restrict, default_permissions, ephemeral, fmt_duration, later, is_admin, is_moderator, mention, parse_duration, send_content,
                     thread_of, user_mention)

router = Router(name="moderation")

COMMANDS = {
    "ban": "ban", "бан": "ban",
    "unban": "unban", "разбан": "unban",
    "mute": "mute", "мут": "mute",
    "unmute": "unmute", "размут": "unmute",
    "kick": "kick", "кик": "kick",
    "warn": "warn", "варн": "warn", "пред": "warn",
    "unwarn": "unwarn", "разварн": "unwarn",
    "warns": "warns", "варны": "warns",
    "del": "del", "дел": "del",
    "purge": "purge", "чистка": "purge",
    "help": "help", "помощь": "help",
    "rules": "rules", "правила": "rules",
    "notes": "notes", "заметки": "notes",
    "get": "get",
    "bot": "spambot", "бот": "spambot", "спамбот": "spambot",
    "timer": "timer", "таймер": "timer", "slowmode": "timer",
}
PUBLIC = {"rules", "notes", "get"}
# эти команды работают только со «/» или «!», чтобы случайное «бот ...» в ответе никого не забанило
PREFIX_ONLY = {"spambot"}

HELP = """<b>Команды модерации</b> (ответом на сообщение, или с @username / ID):
/ban [время] [причина] — бан (бан)
/unban — разбан (разбан)
/mute [время] [причина] — мут (мут)
/unmute — размут (размут)
/kick [причина] — исключить (кик)
/warn [причина] — предупреждение (варн, пред)
/unwarn — снять одно предупреждение (разварн)
/warns — сколько предупреждений (варны)
/del — удалить сообщение (дел)
/бот — спам-бот: бан навсегда, удалить его сообщения за 48 ч и запомнить метки спама для автобана
/purge — удалить всё от отвеченного сообщения до команды (чистка)
/ачивка — выдать ачивку (ответом на сообщение участника), /снятьачивку название — снять
/achhere — публиковать в этой теме поздравления с редкими ачивками
/helperhere — публиковать в этой теме итоги «Помощника недели»
/skyhere — присылать в эту тему напоминания о событиях Sky (<code>/skyhere off</code> — выключить)
/gameshere — проводить в этой теме мини-игры (<code>/gameshere off</code> — убрать тему); сами игры собираются и запускаются в панели
/timer 30s — таймер в текущей теме форума, <code>/timer 0</code> — выключить (таймер)

Время: <code>30m</code>, <code>2h</code>, <code>3d</code>, <code>1w</code> или <code>30м</code>, <code>2ч</code>, <code>3д</code>, <code>1н</code>. Без времени — навсегда.
Команды работают с <code>/</code>, с <code>!</code> и русскими словами без знака.

<b>Для всех:</b> /события — ближайшие события Sky (видно только тебе), /помощники — рейтинг помощников недели, /ачивки — свои ачивки (ответом на чужое сообщение — чужие), /rules — правила, /notes — заметки, <code>#название</code> или /get название — показать заметку."""


class Cmd(Filter):
    """Распознаёт /ban, !ban, /ban@bot и русские «бан», «мут» без префикса."""

    async def __call__(self, message: Message, bot: Bot):
        if not message.text:
            return False
        parts = message.text.split(maxsplit=1)
        head, args = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        plain = head[0] not in "/!"
        if plain:
            cmd = None if head.isascii() else COMMANDS.get(head)
            if cmd in PUBLIC or cmd in PREFIX_ONLY:
                cmd = None
        else:
            name, _, at = head[1:].partition("@")
            if at and at != (await bot.me()).username.lower():
                return False
            cmd = COMMANDS.get(name)
        return {"cmd": cmd, "args": args.strip(), "plain": plain} if cmd else False


async def resolve_target(message: Message, args: str):
    """Возвращает (user_id, упоминание, остаток аргументов) или None."""
    reply = message.reply_to_message
    if reply and not reply.forum_topic_created and reply.from_user and not reply.sender_chat:
        return reply.from_user.id, user_mention(reply.from_user), args
    for e in message.entities or []:
        if e.type == "text_mention" and e.user:
            return e.user.id, user_mention(e.user), args.replace(e.extract_from(message.text), "", 1).strip()
    token, _, rest = args.partition(" ")
    if token.startswith("@") and len(token) > 1:
        uid = await db.find_user(token[1:])
        return (uid, mention(uid, token), rest.strip()) if uid else None
    if token.isdigit():
        return int(token), mention(int(token), token), rest.strip()
    return None


@router.message(Cmd())
async def on_command(message: Message, bot: Bot, cmd: str, args: str, plain: bool):
    chat = message.chat
    thread = thread_of(message)
    s = await settings.get(chat.id)
    if plain and not s["mod_plain"]:
        raise SkipHandler
    if (cmd == "rules" and not s["cmd_rules"]) or (cmd in ("notes", "get") and not s["cmd_notes"]) \
            or (cmd == "spambot" and not s["spambot_cmd"]):
        return

    if cmd == "rules":
        return await message.reply(s["rules"] or await t(chat.id, "cmd.no_rules"))
    if cmd == "notes":
        names = [f"<code>#{r['name']}</code>" for r in await db.content_list(chat.id, "note")]
        return await message.reply(await t(chat.id, "cmd.notes", list=", ".join(names)) if names
                                   else await t(chat.id, "cmd.no_notes"))
    if cmd == "get":
        row = await db.content_get(chat.id, "note", args.lower().lstrip("#"))
        if row:
            await send_content(bot, chat.id, row["data"], reply_to=message.message_id, thread=thread)
        return

    if not await is_moderator(bot, message):
        if plain:
            raise SkipHandler
        return
    try:
        await _moderate(message, bot, cmd, args, plain)
    except TelegramAPIError as e:
        await message.reply(f"⚠️ Telegram не дал это сделать: {escape(e.message)}\n"
                            "Проверь, что у меня есть права администратора на баны и удаление сообщений.")


async def _moderate(message: Message, bot: Bot, cmd: str, args: str, plain: bool):
    chat = message.chat
    thread = thread_of(message)
    reply = message.reply_to_message
    if reply and reply.forum_topic_created:
        reply = None

    if cmd == "help":
        return await message.reply(HELP)
    if cmd == "timer":
        if not chat.is_forum:
            return await message.reply("Таймеры по темам работают только в чатах с темами.")
        key = str(message.message_thread_id if message.is_topic_message else 0)
        if not args:
            current = (await settings.get(chat.id))["topic_slow"].get(key)
            return await message.reply(f"⏱ Таймер в этой теме: {fmt_duration(current) if current else 'выключен'}.\n"
                                       "Поставить: <code>/timer 30s</code>, <code>таймер 2м</code>. Выключить: <code>/timer 0</code>.")
        seconds = parse_duration(args.split()[0], default_unit="s")
        if seconds is None or seconds > 86400:
            return await message.reply("Пример: <code>/timer 30s</code>, <code>таймер 2м</code> (не больше суток).")
        await settings.set_topic_timer(chat.id, key, seconds)
        return await message.reply(await t(chat.id, "topic.timer_set", time=fmt_duration(seconds)) if seconds
                                   else await t(chat.id, "topic.timer_off"))
    if cmd == "del":
        if reply:
            await bot.delete_messages(chat.id, [reply.message_id, message.message_id])
        return
    if cmd == "purge":
        if not reply:
            return await message.reply("Ответь этой командой на сообщение, с которого начать чистку.")
        ids = list(range(max(reply.message_id, message.message_id - 2000), message.message_id + 1))
        for i in range(0, len(ids), 100):
            await bot.delete_messages(chat.id, ids[i:i + 100])
        return

    target = await resolve_target(message, args)
    if target is None:
        if plain:
            raise SkipHandler
        return await message.reply(await t(chat.id, "mod.no_target"))
    uid, name, rest = target
    by = "анонимный админ" if message.sender_chat else user_mention(message.from_user)
    s = await settings.get(chat.id)

    if cmd == "warns":
        return await message.reply(await t(chat.id, "mod.warns", user=name, count=await db.get_warns(chat.id, uid),
                                           limit=s["warn_limit"]))
    if cmd == "unwarn":
        n = await db.add_warn(chat.id, uid, -1)
        return await message.reply(await t(chat.id, "mod.unwarn", user=name, count=n, limit=s["warn_limit"]))
    if cmd == "unban":
        await bot.unban_chat_member(chat.id, uid, only_if_banned=True)
        text = await t(chat.id, "mod.unban", user=name, admin=await admin_line(chat.id, by))
        await message.reply(text)
        return await log(bot, chat, text)
    if cmd == "unmute":
        await bot.restrict_chat_member(chat.id, uid, await default_permissions(bot, chat.id))
        text = await t(chat.id, "mod.unmute", user=name, admin=await admin_line(chat.id, by))
        await message.reply(text)
        return await log(bot, chat, text)

    if uid == bot.id or await is_admin(bot, chat.id, uid):
        return await message.reply(await t(chat.id, "mod.immune"))
    if cmd == "spambot":
        learned = 0
        if reply and reply.from_user and reply.from_user.id == uid:
            learned = await antibot.learn(reply, s["link_whitelist"])
            await spamscore.learn(spamscore.text_of(reply), spam=True)
        ids = [message.message_id] + ([reply.message_id] if reply else [])
        n, problem = await spam_ban(bot, chat, uid, name, ids, "помечен(а) командой /бот", by)
        report = await t(chat.id, "mod.spambot", user=name, deleted=n, markers=learned) + problem
        try:
            await bot.send_message(chat.id, report, message_thread_id=thread,
                                   ephemeral_message_parameters=ephemeral(message.from_user.id))
        except TelegramAPIError:
            note = await bot.send_message(chat.id, report, message_thread_id=thread)
            later(10, note.delete())
        return

    seconds = None
    if cmd in ("ban", "mute") and rest:
        token, _, tail = rest.partition(" ")
        d = parse_duration(token)
        if d is not None:
            seconds, rest = d, tail.strip()
    if cmd == "warn":
        await warn(bot, chat, uid, name, rest, by, thread)
    else:
        await punish(bot, chat, uid, name, cmd, seconds, rest, by, thread)


@router.callback_query(Undo.filter())
async def on_undo(cb: CallbackQuery, callback_data: Undo, bot: Bot):
    d = callback_data
    if not await can_restrict(bot, cb.from_user.id, d.c):
        return await cb.answer("Это могут только админы чата с правом банить.", show_alert=True)
    done = []
    try:
        if d.u:
            await bot.unban_chat_member(d.c, d.u, only_if_banned=True)
            done.append("разбанен(а)")
        if d.m:
            await db.del_marker(d.m)
            antibot.invalidate()
            done.append("метка удалена")
    except TelegramAPIError as e:
        return await cb.answer(f"Не получилось: {e.message}", show_alert=True)
    await cb.answer("Готово")
    with suppress(TelegramAPIError):
        await cb.message.edit_text(cb.message.html_text + f"\n\n✅ {', '.join(done).capitalize()} — "
                                   f"{user_mention(cb.from_user)}", reply_markup=None)
