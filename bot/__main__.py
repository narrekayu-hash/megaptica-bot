import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from . import handlers, menu
from .config import config
from .db import db
from . import spamscore
from .handlers import achievements, captcha, guard, helpers, sky

UPDATES = ["message", "edited_message", "callback_query", "chat_member", "my_chat_member",
           "message_reaction", "message_reaction_count"]


async def on_error(event: ErrorEvent):
    logging.getLogger("bot").error("Ошибка при обработке апдейта", exc_info=event.exception)
    return True


async def backups():
    """Резервная копия базы при запуске и раз в сутки (последние 7 — в data/backups)."""
    folder = os.path.join(os.path.dirname(config.db_path) or ".", "backups")
    while True:
        try:
            await db.backup(folder)
        except Exception:
            logging.getLogger("bot").exception("Не удалось сделать резервную копию базы")
        await asyncio.sleep(24 * 3600)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await db.connect(config.db_path)
    bot = Bot(config.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(*handlers.build())
    dp.errors.register(on_error)
    try:
        await menu.setup(bot)
    except Exception:  # меню команд — не главное, без него бот всё равно работает
        logging.getLogger("bot").exception("Не удалось обновить меню команд")
    watchdog = asyncio.create_task(captcha.watchdog(bot))
    reminders = asyncio.create_task(sky.scheduler(bot))
    weekly = asyncio.create_task(helpers.weekly(bot))
    congrats = asyncio.create_task(achievements.announcer(bot))
    words = asyncio.create_task(guard.saver())
    copies = asyncio.create_task(backups())
    try:
        await dp.start_polling(bot, allowed_updates=UPDATES)
    finally:
        watchdog.cancel()
        reminders.cancel()
        weekly.cancel()
        congrats.cancel()
        words.cancel()
        copies.cancel()
        await spamscore.flush()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
