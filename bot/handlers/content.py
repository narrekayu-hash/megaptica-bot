import logging
import re
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import settings
from ..db import db
from ..texts import t
from ..utils import later, send_content, thread_of, user_mention

router = Router(name="content")
log = logging.getLogger(__name__)


async def answer_privately(bot: Bot, message: Message, data: str, link_payload: str):
    """Команда-ответ, которую видит только спросивший.

    Основной путь — эфемерное сообщение прямо в чате. Если команда пришла из меню, она сама эфемерная
    и её никто, кроме человека, не видел; иначе удаляем её. Если эфемерное не прошло — пишем в ЛС,
    а если и туда нельзя — показываем на 20 секунд кнопку «Получить ответ».
    """
    eph_id = message.ephemeral_message_id
    if not eph_id:
        with suppress(TelegramAPIError):
            await message.delete()
    user = message.from_user
    if not user:
        return
    where = f"чат {message.chat.id}, тема {thread_of(message)}, участник {user.id}, из меню: {bool(eph_id)}"
    # ответ на эфемерную команду возможен только 15 секунд — если не вышло, шлём просто эфемерное в тему
    for reply in ([eph_id, None] if eph_id else [None]):
        try:
            await send_content(bot, message.chat.id, data, thread=thread_of(message),
                               only_for=user.id, reply_to_ephemeral=reply)
            return log.info("Личный ответ показан в чате (%s)", where)
        except TelegramAPIError as e:
            log.warning("Эфемерный ответ не отправился (%s): %s", where, e)
    try:
        await send_content(bot, user.id, data)
        return log.info("Личный ответ отправлен в ЛС (%s)", where)
    except TelegramForbiddenError:
        pass  # человек ещё ни разу не писал боту — Telegram не даёт написать первым
    except TelegramAPIError as e:
        log.warning("Ответ в ЛС не отправился (%s): %s", where, e)
    log.info("Показываю кнопку «Получить ответ» (%s)", where)
    me = await bot.me()
    button = InlineKeyboardButton(text=await t(message.chat.id, "cmd.dm_button"),
                                  url=f"https://t.me/{me.username}?start={link_payload}")
    hint = await bot.send_message(
        message.chat.id, await t(message.chat.id, "cmd.dm_hint", user=user_mention(user)),
        message_thread_id=thread_of(message), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )
    later(20, hint.delete())


@router.message(F.text | F.caption)
async def on_text(message: Message, bot: Bot):
    chat_id = message.chat.id
    low = (message.text or message.caption).lower()
    reply = dict(reply_to=message.message_id, thread=thread_of(message))
    s = await settings.get(chat_id)

    if low[0] in "/!" and len(low) > 1 and s["cmd_custom"]:
        name, _, at = low[1:].split(maxsplit=1)[0].partition("@")
        if not at or at == (await bot.me()).username.lower():
            row = await db.content_get(chat_id, "cmd", name)
            if row:
                return await answer_privately(bot, message, row["data"], f"a{row['id']}")

    if low.startswith("#") and len(low) > 1 and s["cmd_notes"]:
        row = await db.content_get(chat_id, "note", low[1:].split(maxsplit=1)[0])
        if row:
            return await send_content(bot, chat_id, row["data"], **reply)

    if not s["cmd_filters"]:
        return
    for row in await db.content_list(chat_id, "filter"):
        if re.search(rf"(?<!\w){re.escape(row['name'])}(?!\w)", low):
            return await send_content(bot, chat_id, row["data"], **reply)


@router.edited_message()
async def on_edit(message: Message):
    """Пустой обработчик: правки проверяет антиспам в middleware, здесь делать нечего."""
