from aiogram import F, Router

from . import (achievements, antispam, guard, captcha, chats, content, helpers, minigames, moderation, panel, reposts,
               sky)


def build():
    """Порядок важен: служебные события → капча → мини-игры и панель в ЛС → всё, что в группах."""
    group = Router(name="group")
    group.message.filter(F.chat.type.in_({"group", "supergroup"}))
    group.message.outer_middleware(antispam.Antispam())
    group.edited_message.outer_middleware(antispam.Antispam(edited=True))
    group.include_routers(moderation.router, sky.router, helpers.router, achievements.router, minigames.chat_router,
                          content.router)
    return [chats.router, reposts.router, captcha.router, minigames.router, panel.router, guard.router, group]
