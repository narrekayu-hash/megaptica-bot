from aiogram import F, Router

from . import achievements, antispam, guard, captcha, chats, content, helpers, moderation, panel, sky


def build():
    """Порядок важен: служебные события → капча → панель в ЛС → всё, что в группах."""
    group = Router(name="group")
    group.message.filter(F.chat.type.in_({"group", "supergroup"}))
    group.message.outer_middleware(antispam.Antispam())
    group.edited_message.outer_middleware(antispam.Antispam(edited=True))
    group.include_routers(moderation.router, sky.router, helpers.router, achievements.router, content.router)
    return [chats.router, captcha.router, panel.router, guard.router, group]
