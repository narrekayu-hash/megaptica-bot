"""Меню команд, которое Telegram показывает при вводе «/»."""
import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeAllGroupChats,
                           BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeChatAdministrators)

from . import settings
from .db import db

PUBLIC = [("events", "События Sky — увидишь только ты"), ("helpers", "Помощники недели"), ("achievements", "Мои ачивки — увидишь только ты"), ("rules", "Правила чата"), ("notes", "Список заметок")]
ALWAYS_EPHEMERAL = {"events", "helpers", "achievements"}
ADMIN = [("ban", "Забанить"), ("unban", "Разбанить"), ("mute", "Замутить"), ("unmute", "Размутить"),
         ("kick", "Исключить"), ("warn", "Предупреждение"), ("unwarn", "Снять предупреждение"),
         ("warns", "Сколько предупреждений"), ("del", "Удалить сообщение"), ("bot", "Спам-бот: бан, удалить всё, запомнить"), ("purge", "Удалить пачку сообщений"),
         ("timer", "Таймер в этой теме"), ("skyhere", "Напоминания Sky в эту тему"), ("helperhere", "Итоги «Помощника недели» сюда"),
         ("ach", "Выдать ачивку (ответом на сообщение)"), ("achhere", "Поздравления с ачивками сюда"), ("help", "Справка по командам")]
# Telegram принимает в меню только латиницу, цифры и «_»
MENU_NAME = re.compile(r"[a-z0-9_]{1,32}")


def _cmds(pairs, ephemeral=()):
    # эфемерную команду из меню видят только сам человек и бот
    return [BotCommand(command=c, description=d, is_ephemeral=(c in ephemeral or c in ALWAYS_EPHEMERAL) or None) for c, d in pairs]


async def setup(bot: Bot):
    await bot.set_my_commands(_cmds([("start", "Панель настроек"), ("help", "Справка")]),
                              BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(_cmds(PUBLIC), BotCommandScopeAllGroupChats())
    await bot.set_my_commands(_cmds(PUBLIC + ADMIN), BotCommandScopeAllChatAdministrators())
    for row in await db.all("SELECT chat_id FROM chats WHERE active=1"):
        await sync_chat(bot, row["chat_id"])


SWITCH = {"events": "cmd_events", "helpers": "helpers", "achievements": "achievements", "rules": "cmd_rules",
          "notes": "cmd_notes"}
ADMIN_SWITCH = {"bot": "spambot_cmd", "ach": "achievements", "achhere": "achievements", "helperhere": "helpers"}


async def sync_chat(bot: Bot, chat_id: int):
    """Меню конкретного чата: только включённые команды плюс его команды-ответы."""
    s = await settings.get(chat_id)
    public = [(c, d) for c, d in PUBLIC if s[SWITCH[c]]]
    admin = [(c, d) for c, d in ADMIN if c not in ADMIN_SWITCH or s[ADMIN_SWITCH[c]]]
    custom = [(r["name"], "Ответ увидишь только ты") for r in await db.content_list(chat_id, "cmd")
              if MENU_NAME.fullmatch(r["name"])] if s["cmd_custom"] else []
    eph = {c for c, _ in custom}
    try:
        if public:
            await bot.set_my_commands(_cmds(public + custom, eph), BotCommandScopeChat(chat_id=chat_id))
        else:
            await bot.delete_my_commands(BotCommandScopeChat(chat_id=chat_id))
        await bot.set_my_commands(_cmds(public + admin + custom, eph), BotCommandScopeChatAdministrators(chat_id=chat_id))
    except TelegramAPIError as e:
        logging.getLogger(__name__).warning("Не удалось обновить меню команд чата %s: %s", chat_id, e)
