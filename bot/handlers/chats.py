from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberUpdated, Message

from .. import settings
from ..config import config

router = Router(name="chats")


@router.my_chat_member(F.chat.type.in_({"group", "supergroup"}))
async def on_bot_status(event: ChatMemberUpdated, bot: Bot):
    old, new = event.old_chat_member.status, event.new_chat_member.status
    title = escape(event.chat.title or "")
    if new in ("left", "kicked"):
        return await settings.register(event.chat.id, event.chat.title, active=False)
    if old in ("left", "kicked"):
        # бот приватный: из чужих чатов сразу уходит
        if event.from_user.id not in config.owners:
            with suppress(TelegramAPIError):
                await bot.leave_chat(event.chat.id)
            return
        text = (f"Меня добавили в <b>{title}</b>. Выдай мне права администратора: удаление сообщений "
                "и блокировка участников. Настройки — /start")
    elif new == "administrator" and old != "administrator":
        text = f"✅ В <b>{title}</b> у меня теперь есть права администратора, можно работать."
    else:
        text = None
    await settings.register(event.chat.id, event.chat.title)
    if text:
        for owner in config.owners:
            with suppress(TelegramAPIError):
                await bot.send_message(owner, text)


@router.message(F.migrate_to_chat_id)
async def on_migrate(message: Message):
    await settings.migrate(message.chat.id, message.migrate_to_chat_id)


@router.message(F.new_chat_members | F.left_chat_member)
async def on_service(message: Message):
    if (await settings.get(message.chat.id))["clean_service"]:
        with suppress(TelegramAPIError):
            await message.delete()

