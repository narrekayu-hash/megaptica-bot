import re
from contextlib import suppress
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import time

from .. import achievements, antibot, menu, settings, sky, texts
from ..db import db
from ..settings import FIELDS, HINTS, SECTION_OF, SECTIONS
from ..config import config
from ..utils import can_manage, fmt_duration, is_chief, parse_duration, send_content, serialize
from .moderation import COMMANDS, HELP
from .achievements import NAMES as ACH_NAMES
from .helpers import NAMES as HELPER_NAMES
from .sky import NAMES as SKY_NAMES

router = Router(name="panel")
router.message.filter(F.chat.type == "private")

# вид: (кнопка, пояснение, что спросить первым шагом)
KINDS = {
    "cmd": ("❓ Команды-ответы",
            "Участник пишет в чате, например, <code>/faq</code>, и получает ответ, который видит только он "
            "(Telegram помечает такие сообщения как видимые одному человеку). Чат не засоряется. "
            "Удобно для частых вопросов.",
            "название команды без «/». Если написать латиницей (например, <code>faq</code>), "
            "команда появится в меню Telegram"),
    "filter": ("🔁 Автоответы",
               "Бот отвечает в чате, когда в сообщении встречается слово-триггер.",
               "слово или фразу-триггер"),
    "note": ("📌 Заметки",
             "Участники вызывают заметку в чате через <code>#название</code> или /get название.",
             "название заметки (одно слово)"),
}
PREFIX = {"cmd": "/", "note": "#", "filter": ""}
NO_ACCESS = ("Привет! Я бот-модератор чата.\n\nНастраивать меня могут только создатель чата и админы "
             "с правом «Изменение профиля группы».")


class P(CallbackData, prefix="p"):
    a: str
    c: int = 0
    k: str = ""


class Form(StatesGroup):
    value = State()
    topic = State()
    item_name = State()
    item_body = State()
    ach_title = State()
    ach_new_title = State()
    ach_new_desc = State()
    text_value = State()
    access_user = State()
    post_channel = State()
    post_tag = State()


def btn(text: str, **kw) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=P(**kw).pack())


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def show(key: str, v) -> str:
    _, typ, opt = FIELDS[key]
    if typ == "bool":
        return "✅" if v else "❌"
    if typ == "enum":
        return opt[v]
    if typ == "list":
        return f"{len(v)} шт." if v else "пусто"
    if typ == "text":
        return "задан" if v else "пусто"
    if typ == "chat":
        return str(v) if v else "выкл"
    if typ == "topic":
        return "выкл" if v is None else ("Общая" if v == "0" else f"тема #{v}")
    return str(v)


# --- экраны ---
async def home_view(bot: Bot, uid: int):
    rows = [r for r in await db.all("SELECT chat_id, title FROM chats WHERE active=1 ORDER BY title")
            if await can_manage(bot, uid, r["chat_id"])]
    if not rows:
        return NO_ACCESS, None
    return "Выбери чат для настройки:", kb([[btn(r["title"] or str(r["chat_id"]), a="chat", c=r["chat_id"])]
                                            for r in rows])


async def chat_view(c: int):
    row = await db.one("SELECT title FROM chats WHERE chat_id=?", c)
    rows = [[btn(title, a="sec", c=c, k=key)] for key, (title, _) in SECTIONS.items()]
    rows.append([btn(KINDS["cmd"][0], a="list", c=c, k="cmd"), btn("⏱ Темы", a="topics", c=c)])
    rows.append([btn(KINDS["filter"][0], a="list", c=c, k="filter"), btn(KINDS["note"][0], a="list", c=c, k="note")])
    rows.append([btn("🤖 Метки спама", a="mk", c=c), btn("✏️ Тексты", a="txs", c=c)])
    rows.append([btn("👥 Доступ к панели", a="acc", c=c)])
    rows.append([btn("« Чаты", a="home")])
    return f"⚙️ <b>{escape(row['title'] if row else str(c))}</b>\nЧто настраиваем?", kb(rows)


async def section_view(c: int, sec: str):
    s = await settings.get(c)
    title, keys = SECTIONS[sec]
    rows, lines = [], []
    for k in keys:
        name, typ, _ = FIELDS[k]
        rows.append([btn(f"{name}: {show(k, s[k])}", a={"bool": "tog", "enum": "cyc"}.get(typ, "inp"), c=c, k=k)])
        lines.append(f"• <b>{name}</b> — {HINTS[k]}")
    if sec == "posts":
        topics = {r["thread_id"]: r["name"] for r in await db.topics(c)}
        for i, r in enumerate(s["post_rules"]):
            place = "Общая" if not r["thread"] else topics.get(r["thread"], f"тема #{r['thread']}")
            rows.append([btn(f"🗑 #{r['tag']} → {place} ({r['title']})"[:60], a="prd", c=c, k=str(i))])
        rows.append([btn("➕ Добавить правило", a="pra", c=c)])
        lines.append("\nПост с тегом из правила бот публикует в теме без звука и без закрепа. Правка на канале обновляет "
                     "копию, а если тег убрать, копия удалится. Посты, вышедшие до появления правила, не копируются. "
                     "Об удалении поста Telegram бота не извещает: удалённый на канале пост убери из темы вручную. "
                     "Нажми на правило, чтобы удалить его: уже опубликованные копии останутся.")
    if sec == "ach":
        rows.append([btn("📋 Список ачивок", a="achl", c=c), btn("🗂 Темы для ачивок", a="acht", c=c)])
    rows.append([btn("« Назад", a="chat", c=c)])
    return (f"<b>{title}</b>\n\n" + "\n".join(lines) +
            "\n\n✅/❌ и варианты переключаются нажатием, остальное бот попросит прислать."), kb(rows)


async def list_view(c: int, kind: str):
    title, hint, _ = KINDS[kind]
    rows = [[btn(f"🗑 {PREFIX[kind]}{r['name']}", a="ask", c=c, k=str(r["id"]))]
            for r in await db.content_list(c, kind)]
    rows.append([btn("➕ Добавить", a="add", c=c, k=kind)])
    rows.append([btn("« Назад", a="chat", c=c)])
    tail = "\nНажми на элемент, чтобы удалить его." if len(rows) > 2 else ""
    return f"<b>{title}</b>\n{hint}{tail}", kb(rows)


async def topics_view(c: int):
    s = await settings.get(c)
    slow, noreply, helped = s["topic_slow"], s["topic_noreply"], s["helper_topics"]
    topics = [(0, "Общая")] + [(r["thread_id"], r["name"]) for r in await db.topics(c)]
    rows = []
    for tid, name in topics:
        t = str(tid)
        rows.append([btn(f"⏱ {name}: {fmt_duration(slow[t]) if slow.get(t) else 'выкл'}", a="tinp", c=c, k=t),
                     btn("↩️ нельзя" if t in noreply else "↩️ можно", a="nrt", c=c, k=t),
                     btn("🏅 ✅" if t in helped else "🏅 ❌", a="hlp", c=c, k=t)])
    rows.append([btn("« Назад", a="chat", c=c)])
    return ("<b>⏱ Темы</b>\n"
            "• <b>⏱ Таймер</b> — сколько каждый участник ждёт между своими сообщениями в теме. Если поторопится, "
            "сообщение удалится, а бот покажет только ему, сколько осталось, и вернёт текст.\n"
            "• <b>↩️ Ответы</b> — можно ли отвечать (реплаем) на сообщения других участников. Если «нельзя», "
            "такие ответы удаляются, а автор получает подсказку, которую видит только он.\n\n"
            "• <b>🏅</b> — учитывать ли ответы в этой теме для «Помощника недели».\n\n"
            "Тема появится в списке, когда в ней кто-нибудь напишет. Таймер можно поставить и прямо в теме: "
            "<code>/timer 30s</code> или <code>таймер 2м</code>."), kb(rows)


KIND_NAMES = {"auto": "бот выдаёт сам", "manual": "выдают админы командой /ачивка ответом на сообщение",
              "secret": "секретная: условие видно только тем, кто её получил"}


async def ach_list_view(c: int):
    s = await settings.get(c)
    rows = []
    for a in achievements.catalog(s):
        mark = "✅" if achievements.available(a, s) else ("❌" if a.code in s["ach_off"] else "💤")
        rows.append([btn(f"{mark} {a.title}", a="achi", c=c, k=a.code)])
    rows.append([btn("➕ Своя ачивка", a="achn", c=c)])
    rows.append([btn("« Назад", a="sec", c=c, k="ach")])
    return ("<b>📋 Ачивки</b>\n✅ — работает, ❌ — выключена, 💤 — в чате нет нужной темы (настраивается в "
            "«🗂 Темы для ачивок» и значком 🏅 в «⏱ Темы»).\nНажми на ачивку, чтобы выключить или переименовать её. "
            "«➕ Своя ачивка» — новая награда, которую будут выдавать админы."), kb(rows)


async def ach_item_view(c: int, code: str):
    s = await settings.get(c)
    a = next((x for x in achievements.catalog(s) if x.code == code), None)
    if a is None:
        return await ach_list_view(c)
    off = code in s["ach_off"]
    rows = [[btn("❌ Выключена — включить" if off else "✅ Включена — выключить", a="acho", c=c, k=code)],
            [btn("✏️ Переименовать", a="achr", c=c, k=code)]]
    if code in s["ach_custom"]:
        rows.append([btn("🗑 Удалить", a="achd", c=c, k=code)])
    rows.append([btn("« Назад", a="achl", c=c)])
    need = {"help": "темы с 🏅 в «⏱ Темы»", "friends": "темы для поиска друзей", "chat": "темы для общения",
            "photo": "темы для фото"}.get(a.topic)
    tail = f"\nНужны {need}." if need else ""
    return (f"<b>{a.title}</b>\nЗа что: {a.desc}\nКак выдаётся: {KIND_NAMES[a.kind]}.{tail}\n"
            f"Уже получили: {await db.ach_holders(c, code)}"), kb(rows)


ROLE_ORDER = [None, "photo", "friends", "chat"]


async def ach_topics_view(c: int):
    s = await settings.get(c)
    roles = s["ach_topics"]
    topics = [(0, "Общая")] + [(r["thread_id"], r["name"]) for r in await db.topics(c)]
    rows = [[btn(f"{name}: {achievements.ROLES.get(roles.get(str(tid)), '—')}", a="achtc", c=c, k=str(tid))]
            for tid, name in topics]
    rows.append([btn("« Назад", a="sec", c=c, k="ach")])
    return ("<b>🗂 Темы для ачивок</b>\nНажимай на тему, чтобы выбрать, какие ачивки в ней можно заработать:\n"
            "• 📸 фото — «Фотограф Долины» и «Звезда галереи»\n"
            "• 🤝 поиск друзей — «Ищу свет» и «Протянутая рука»\n"
            "• 🔥 общение — «У костра»\n\n"
            "Ачивки за ответы на вопросы считаются в темах, отмеченных 🏅 в «⏱ Темы»."), kb(rows)


# --- тексты ---
async def texts_view(c: int):
    custom = (await settings.get(c))["texts"]
    rows = []
    for g, title in texts.GROUPS.items():
        keys = [k for k, v in texts.TEXTS.items() if v.group == g]
        mine = sum(1 for k in keys if k in custom)
        rows.append([btn(f"{title} ({mine}/{len(keys)} изменено)" if mine else title, a="txg", c=c, k=g)])
    rows.append([btn("« Назад", a="chat", c=c)])
    return ("<b>✏️ Тексты</b>\nЗдесь можно переписать всё, что бот пишет в чат, участникам и админам. "
            "В каждом тексте указано, где его видно и какое форматирование там работает.\n\n"
            "Приветствие и правила меняются в «👋 Новички», ачивки переименовываются в «🏆 Ачивки». "
            "Таблица /события собирается автоматически и не меняется."), kb(rows)


async def text_group_view(c: int, g: str):
    custom = (await settings.get(c))["texts"]
    rows = [[btn(("✏️ " if k in custom else "") + v.title, a="txi", c=c, k=k)]
            for k, v in texts.TEXTS.items() if v.group == g]
    rows.append([btn("« Назад", a="txs", c=c)])
    return f"<b>{texts.GROUPS[g]}</b>\n✏️ — текст уже изменён. Нажми на текст, чтобы посмотреть или поменять его.", kb(rows)


def _shown(key: str, raw: str) -> str:
    return escape(raw) if texts.TEXTS[key].fmt in ("plain", "button") else raw


async def text_item_view(c: int, key: str):
    item = texts.TEXTS[key]
    custom = (await settings.get(c))["texts"].get(key)
    lines = [f"<b>{item.title}</b>", f"<b>Где:</b> {item.where}", f"<b>Форматирование:</b> {texts.FORMATS[item.fmt]}"]
    if item.vars:
        lines.append("<b>Подстановки</b> (бот заменит их сам):")
        lines += [f"<code>{{{name}}}</code> — {desc}" for name, (desc, _) in item.vars.items()]
    lines += ["", f"<b>Сейчас ({'свой' if custom else 'стандартный'}):</b>", _shown(key, custom or item.default)]
    rows = [[btn("✏️ Изменить", a="txe", c=c, k=key), btn("👁 Пример", a="txp", c=c, k=key)]]
    if custom:
        rows.append([btn("↩️ Вернуть стандартный", a="txr", c=c, k=key)])
    rows.append([btn("« Назад", a="txg", c=c, k=item.group)])
    return "\n".join(lines), kb(rows)


async def preview(bot: Bot, chat_id: int, key: str, raw: str):
    """Отправляет текст с примерами вместо подстановок. Ошибку Telegram пробрасывает наверх."""
    item = texts.TEXTS[key]
    shown = texts.sample(key, raw)
    if item.fmt == "button":
        await bot.send_message(chat_id, "👁 Так будет выглядеть кнопка:", reply_markup=kb(
            [[InlineKeyboardButton(text=shown, callback_data="noop")]]))
    elif item.fmt == "plain":
        await bot.send_message(chat_id, "👁 Так будет выглядеть всплывающее окно:\n\n" + escape(shown))
    else:
        await bot.send_message(chat_id, shown)


# --- доступ ---
async def access_view(c: int):
    s = await settings.get(c)
    rows = [[btn(f"Админы с правом «Изменение профиля»: {'✅' if s['panel_by_rights'] else '❌'}", a="accr", c=c)]]
    rows += [[btn(f"🗑 {name}", a="accd", c=c, k=uid)] for uid, name in s["panel_users"].items()]
    rows.append([btn("➕ Дать доступ человеку", a="acca", c=c)])
    rows.append([btn("« Назад", a="chat", c=c)])
    return ("<b>👥 Доступ к панели</b>\nНастраивать бота всегда могут владелец бота и создатель чата. "
            "Ещё доступ можно дать конкретным людям (они не обязаны быть админами) и, если включено, "
            "всем админам с правом «Изменение профиля группы».\n"
            "Наказывать командами могут админы с правом «Блокировка пользователей» — это настраивается в самом Telegram.\n\n"
            "Менять этот список могут только владелец бота и создатель чата. Нажми на человека, чтобы забрать доступ."), kb(rows)


MARK_ICONS = {"text": "📝", "link": "🔗", "mention": "👤", "media": "🖼", "forward": "↪️"}


async def markers_view(c: int):
    rows = await db.markers()
    buttons = [[btn(f"🗑 {MARK_ICONS.get(r['kind'], '•')} {r['value'][:40]}", a="mkdel", c=c, k=str(r["id"]))]
               for r in rows[:40]]
    buttons.append([btn("« Назад", a="chat", c=c)])
    more = f"\nПоказаны последние 40 из {len(rows)}." if len(rows) > 40 else ""
    return ("<b>🤖 Метки спама</b>\n"
            "Когда админ отвечает на спам командой <code>/бот</code>, бот банит автора и запоминает, по чему узнать "
            "такой спам: 📝 текст, 🔗 ссылки, 👤 упоминания, 🖼 картинки и стикеры, ↪️ откуда переслано. "
            "Совпадение с меткой добавляет сообщению баллы подозрительности (настройка «Умный антиспам»). "
            "Сама по себе метка банит только новичков и только вместе с признаками новичка.\n\n"
            f"Всего меток: {len(rows)}.{more} Если метка ловит нормальных людей, нажми на неё, чтобы удалить."), kb(buttons)


TOPIC_CMD = {"sky_topic": "/skyhere", "ach_topic": "/achhere"}


def prompt(key: str, value) -> str:
    name, typ, opt = FIELDS[key]
    if typ == "int":
        hint = f"Отправь число от {opt[0]} до {opt[1]}."
        current = str(value)
    elif typ == "list":
        hint = "Отправь список: каждый пункт с новой строки или через запятую. «-» — очистить."
        current = escape("\n".join(value)) or "пусто"
    elif typ == "topic":
        hint = (f"Зайди в нужную тему чата и напиши там <code>{TOPIC_CMD[key]}</code>. "
                "Отправь «-», чтобы выключить.")
        current = show(key, value)
    elif typ == "chat":
        hint = ("Перешли сюда любое сообщение из канала или группы для логов (я должен быть там админом) "
                "или отправь их ID. «я» — присылать логи тебе сюда, «-» — отключить.")
        current = show(key, value)
    else:
        hint = "Отправь новый текст, форматирование сохранится. «-» — очистить."
        if key == "welcome_text":
            hint += ("\nПодстановки: <code>{mention}</code> — упоминание, <code>{name}</code> — имя, "
                     "<code>{chat}</code> — название чата.")
        current = value or "пусто"
    return f"<b>{name}</b> — {HINTS[key]}\n{hint}\n\nСейчас:\n{current}"


async def parse_value(bot: Bot, message: Message, typ: str, opt):
    raw = (message.text or "").strip()
    if typ == "int":
        if not raw.isdigit():
            raise ValueError("Нужно целое число.")
        if not opt[0] <= int(raw) <= opt[1]:
            raise ValueError(f"Число должно быть от {opt[0]} до {opt[1]}.")
        return int(raw)
    if typ == "list":
        return [] if raw == "-" else list(dict.fromkeys(x.strip().lower() for x in re.split(r"[,\n]", raw) if x.strip()))
    if typ == "text":
        if not raw:
            raise ValueError("Нужен текст.")
        return "" if raw == "-" else message.html_text
    if typ == "topic":
        if raw == "-":
            return None
        raise ValueError("Тему выбирают командой прямо в ней (подсказка выше). «-» — выключить.")
    # chat
    if raw == "-":
        return None
    origin = message.forward_origin
    if raw.lower() in ("я", "me"):
        target = message.from_user.id
    elif origin is not None and (getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)):
        target = (getattr(origin, "chat", None) or origin.sender_chat).id
    elif re.fullmatch(r"-?\d+", raw):
        target = int(raw)
    else:
        raise ValueError("Не получилось определить чат. Перешли сообщение из него или отправь ID.")
    try:
        await bot.send_message(target, "✅ Сюда будут приходить логи модерации.")
    except TelegramAPIError as e:
        raise ValueError(f"Не могу писать в этот чат: {escape(e.message)}")
    return target


async def _cancel(c: int, back: str, k: str = ""):
    return kb([[btn("Отмена", a=back, c=c, k=k)]])


# --- обработчики ---
@router.message(CommandStart(deep_link=True, magic=F.args.regexp(r"^sky-?\d+$")))
async def on_sky_link(message: Message, command: CommandObject):
    """Кнопка «Получить ответ» для /события из чата: время показываем в поясе того чата."""
    tz = (await settings.get(int(command.args[3:])))["sky_tz"]
    await message.answer(sky.overview(datetime.now(timezone.utc), tz))


@router.message(CommandStart(deep_link=True, magic=F.args.regexp(r"^hlp-?\d+$")))
async def on_helpers_link(message: Message, command: CommandObject):
    from .helpers import standings_text, week_of
    chat_id = int(command.args[3:])
    tz = (await settings.get(chat_id))["sky_tz"]
    await message.answer(await standings_text(chat_id, week_of(datetime.now(timezone.utc).astimezone(ZoneInfo(tz))), 10))


@router.message(CommandStart(deep_link=True, magic=F.args.regexp(r"^ach-?\d+_\d+$")))
async def on_ach_link(message: Message, command: CommandObject):
    chat_id, uid = map(int, command.args[3:].split("_"))
    own = uid == message.from_user.id
    await message.answer(await achievements.collection_text(chat_id, uid, "участника", own))


@router.message(Command("events", "sky", "события"))
async def on_sky_private(message: Message):
    await message.answer(sky.overview(datetime.now(timezone.utc), "Europe/Moscow"))


@router.message(CommandStart(deep_link=True, magic=F.args.regexp(r"^a\d+$")))
async def on_answer_link(message: Message, command: CommandObject, bot: Bot):
    """Кнопка «Получить ответ» из чата ведёт сюда."""
    row = await db.content_item(int(command.args[1:]))
    if row and row["kind"] == "cmd":
        await send_content(bot, message.chat.id, row["data"])
    else:
        await message.answer(await texts.t(0, "cmd.gone"))


@router.message(CommandStart())
@router.message(Command("panel", "cancel"))
async def on_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    text, markup = await home_view(bot, message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("help"))
async def on_help(message: Message):
    await message.answer(HELP + "\n\nВсе настройки — в /start.")


@router.callback_query(P.filter())
async def on_button(cb: CallbackQuery, callback_data: P, state: FSMContext, bot: Bot):
    a, c, k = callback_data.a, callback_data.c, callback_data.k
    form = await state.get_data()
    await state.clear()
    if c and not await can_manage(bot, cb.from_user.id, c):
        return await cb.answer("У тебя больше нет прав настраивать этот чат.", show_alert=True)
    if a == "home":
        view = await home_view(bot, cb.from_user.id)
    elif a == "chat":
        view = await chat_view(c)
    elif a == "sec":
        view = await section_view(c, k)
    elif a in ("tog", "cyc"):
        value = (await settings.get(c))[k]
        if a == "tog":
            value = not value
        else:
            opts = list(FIELDS[k][2])
            value = opts[(opts.index(value) + 1) % len(opts)]
        await settings.put(c, k, value)
        if k in ("cmd_events", "cmd_rules", "cmd_notes", "cmd_custom", "helpers", "achievements", "spambot_cmd"):
            await menu.sync_chat(bot, c)
        view = await section_view(c, SECTION_OF[k])
    elif a == "inp":
        await state.set_state(Form.value)
        await state.update_data(c=c, k=k)
        await cb.message.answer(prompt(k, (await settings.get(c))[k]),
                                reply_markup=await _cancel(c, "sec", SECTION_OF[k]))
        return await cb.answer()
    elif a == "topics":
        view = await topics_view(c)
    elif a == "tinp":
        await state.set_state(Form.topic)
        await state.update_data(c=c, k=k)
        await cb.message.answer("Отправь таймер для темы: например <code>30</code> (секунды), <code>2м</code>, "
                                "<code>1ч</code>. «0» — выключить.", reply_markup=await _cancel(c, "topics"))
        return await cb.answer()
    elif a == "nrt":
        on = await settings.toggle_noreply(c, k)
        await cb.answer("Ответы в теме запрещены" if on else "Ответы в теме разрешены")
        view = await topics_view(c)
    elif a == "hlp":
        on = await settings.toggle_list(c, "helper_topics", k)
        await cb.answer("Ответы в теме считаются" if on else "Ответы в теме не считаются")
        view = await topics_view(c)
    elif a == "txs":
        view = await texts_view(c)
    elif a == "txg":
        view = await text_group_view(c, k)
    elif a == "txi":
        view = await text_item_view(c, k)
    elif a == "txe":
        await state.set_state(Form.text_value)
        await state.update_data(c=c, k=k)
        item = texts.TEXTS[k]
        how = ("Отправь новый текст обычным сообщением, без оформления." if item.fmt in ("plain", "button") else
               "Отправь новый текст. Оформи его прямо в Telegram (жирный, курсив, ссылки…) — оформление сохранится.")
        await cb.message.answer(f"{how}\nПодстановки вроде <code>{{user}}</code> пиши как есть, в фигурных скобках.",
                                reply_markup=await _cancel(c, "txi", k))
        return await cb.answer()
    elif a == "txp":
        raw = (await settings.get(c))["texts"].get(k) or texts.TEXTS[k].default
        with suppress(TelegramAPIError):
            await preview(bot, cb.from_user.id, k, raw)
        return await cb.answer()
    elif a == "txr":
        custom = dict((await settings.get(c))["texts"])
        custom.pop(k, None)
        await settings.put(c, "texts", custom)
        view = await text_item_view(c, k)
    elif a in ("acc", "accr", "accd", "acca"):
        if not await is_chief(bot, cb.from_user.id, c):
            return await cb.answer("Доступ раздают только владелец бота и создатель чата.", show_alert=True)
        s = await settings.get(c)
        if a == "accr":
            await settings.put(c, "panel_by_rights", not s["panel_by_rights"])
        elif a == "accd":
            users = dict(s["panel_users"])
            users.pop(k, None)
            await settings.put(c, "panel_users", users)
        elif a == "acca":
            await state.set_state(Form.access_user)
            await state.update_data(c=c)
            await cb.message.answer("Перешли сюда любое сообщение этого человека, или отправь его @username "
                                    "(если он писал в чате) или числовой ID.", reply_markup=await _cancel(c, "acc"))
            return await cb.answer()
        view = await access_view(c)
    elif a == "pra":
        await state.set_state(Form.post_channel)
        await state.update_data(c=c)
        await cb.message.answer("Сначала добавь меня админом в канал (права можно не давать), потом перешли сюда любой "
                                "пост оттуда или отправь @username канала.", reply_markup=await _cancel(c, "sec", "posts"))
        return await cb.answer()
    elif a == "prd":
        rules = list((await settings.get(c))["post_rules"])
        if k.isdigit() and int(k) < len(rules):
            rules.pop(int(k))
            await settings.put(c, "post_rules", rules)
        view = await section_view(c, "posts")
    elif a == "prt":
        if form.get("c") != c or not form.get("tag"):
            return await cb.answer("Этот выбор устарел, добавь правило заново.", show_alert=True)
        rule = {"channel": form["channel"], "title": form["title"], "tag": form["tag"], "thread": int(k),
                "since": int(time.time())}
        same = lambda r: (r["channel"], r["tag"], r["thread"]) == (rule["channel"], rule["tag"], rule["thread"])
        rules = [r for r in (await settings.get(c))["post_rules"] if not same(r)] + [rule]
        await settings.put(c, "post_rules", rules)
        view = await section_view(c, "posts")
    elif a == "achl":
        view = await ach_list_view(c)
    elif a == "achi":
        view = await ach_item_view(c, k)
    elif a == "acho":
        await settings.toggle_list(c, "ach_off", k)
        view = await ach_item_view(c, k)
    elif a in ("achr", "achn"):
        await state.set_state(Form.ach_title if a == "achr" else Form.ach_new_title)
        await state.update_data(c=c, k=k)
        hint = "«-» — вернуть стандартное." if a == "achr" else "Например: «🌸 Душа чата»."
        await cb.message.answer(f"Отправь название ачивки с эмодзи в начале. {hint}",
                                reply_markup=await _cancel(c, "achl"))
        return await cb.answer()
    elif a == "achd":
        custom = dict((await settings.get(c))["ach_custom"])
        custom.pop(k, None)
        await settings.put(c, "ach_custom", custom)
        await db.ach_del_code(c, k)
        view = await ach_list_view(c)
    elif a == "acht":
        view = await ach_topics_view(c)
    elif a == "achtc":
        roles = dict((await settings.get(c))["ach_topics"])
        role = ROLE_ORDER[(ROLE_ORDER.index(roles.get(k)) + 1) % len(ROLE_ORDER)]
        if role:
            roles[k] = role
        else:
            roles.pop(k, None)
        await settings.put(c, "ach_topics", roles)
        view = await ach_topics_view(c)
    elif a == "mk":
        view = await markers_view(c)
    elif a == "mkdel":
        await db.del_marker(int(k))
        antibot.invalidate()
        await cb.answer("Метка удалена")
        view = await markers_view(c)
    elif a == "list":
        view = await list_view(c, k)
    elif a == "add":
        await state.set_state(Form.item_name)
        await state.update_data(c=c, k=k)
        await cb.message.answer(f"Отправь {KINDS[k][2]}.", reply_markup=await _cancel(c, "list", k))
        return await cb.answer()
    elif a == "ask":
        row = await db.content_item(int(k))
        if not row:
            return await cb.answer("Уже удалено")
        view = (f"Удалить «{escape(PREFIX[row['kind']] + row['name'])}»?",
                kb([[btn("🗑 Да, удалить", a="rm", c=c, k=k), btn("Нет", a="list", c=c, k=row["kind"])]]))
    elif a == "rm":
        row = await db.content_del(int(k))
        kind = row["kind"] if row else "filter"
        if kind == "cmd":
            await menu.sync_chat(bot, c)
        view = await list_view(c, kind)
    else:
        return await cb.answer()
    if a not in ("nrt", "mkdel", "hlp"):
        await cb.answer()
    with suppress(TelegramBadRequest):  # «message is not modified» и старые сообщения
        await cb.message.edit_text(view[0], reply_markup=view[1])


async def _form_chat(message: Message, state: FSMContext, bot: Bot):
    """Данные формы, если у человека всё ещё есть права на этот чат."""
    data = await state.get_data()
    if not await can_manage(bot, message.from_user.id, data["c"]):
        await state.clear()
        await message.answer("У тебя больше нет прав настраивать этот чат.")
        return None
    return data


@router.message(Form.value)
async def on_value(message: Message, state: FSMContext, bot: Bot):
    data = await _form_chat(message, state, bot)
    if not data:
        return
    c, k = data["c"], data["k"]
    _, typ, opt = FIELDS[k]
    try:
        value = await parse_value(bot, message, typ, opt)
    except ValueError as e:
        return await message.answer(f"⚠️ {e}")
    await settings.put(c, k, value)
    await state.clear()
    text, markup = await section_view(c, SECTION_OF[k])
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=markup)


@router.message(Form.topic)
async def on_topic_value(message: Message, state: FSMContext, bot: Bot):
    data = await _form_chat(message, state, bot)
    if not data:
        return
    seconds = parse_duration((message.text or "").strip(), default_unit="s")
    if seconds is None or seconds > 86400:
        return await message.answer("⚠️ Не получилось разобрать. Пример: <code>30</code>, <code>2м</code>, <code>1ч</code> "
                                    "(не больше суток), «0» — выключить.")
    await settings.set_topic_timer(data["c"], data["k"], seconds)
    await state.clear()
    text, markup = await topics_view(data["c"])
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=markup)


@router.message(Form.item_name)
async def on_item_name(message: Message, state: FSMContext, bot: Bot):
    data = await _form_chat(message, state, bot)
    if not data:
        return
    kind = data["k"]
    name = (message.text or "").strip().lower().lstrip("#/!")
    if not name or (kind != "filter" and " " in name):
        return await message.answer("Нужен текст" + (" одним словом." if kind != "filter" else "."))
    if kind == "cmd" and (name in COMMANDS or name in SKY_NAMES or name in HELPER_NAMES or name in ACH_NAMES):
        return await message.answer("Это имя занято встроенной командой, выбери другое.")
    await state.update_data(name=name)
    await state.set_state(Form.item_body)
    await message.answer("Теперь отправь, что отвечать: текст, фото, видео, гифку, стикер, файл или голосовое. "
                         "Форматирование сохранится.")


@router.message(Form.item_body)
async def on_item_body(message: Message, state: FSMContext, bot: Bot):
    d = await _form_chat(message, state, bot)
    if not d:
        return
    data = serialize(message)
    if not data:
        return await message.answer("Такой тип сообщения не поддерживается, попробуй другой.")
    await db.content_set(d["c"], d["k"], d["name"], data)
    await state.clear()
    if d["k"] == "cmd":
        await menu.sync_chat(bot, d["c"])
    text, markup = await list_view(d["c"], d["k"])
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=markup)


def _title(message: Message):
    """Название ачивки из сообщения или None, если оно не подходит."""
    raw = (message.text or "").strip()
    return raw if raw and "\n" not in raw and len(raw) <= 40 else None


@router.message(Form.ach_title)
async def on_ach_title(message: Message, state: FSMContext, bot: Bot):
    data = await _form_chat(message, state, bot)
    if not data:
        return
    c, code = data["c"], data["k"]
    raw = (message.text or "").strip()
    title = None if raw == "-" else _title(message)
    if raw != "-" and not title:
        return await message.answer("⚠️ Нужна одна строка, не длиннее 40 символов.")
    s = await settings.get(c)
    if code in s["ach_custom"]:
        custom = dict(s["ach_custom"])
        if title:
            custom[code] = {**custom[code], "t": title}
            await settings.put(c, "ach_custom", custom)
    else:
        names = dict(s["ach_names"])
        if title:
            names[code] = title
        else:
            names.pop(code, None)
        await settings.put(c, "ach_names", names)
    await state.clear()
    text, markup = await ach_item_view(c, code)
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=markup)


@router.message(Form.ach_new_title)
async def on_ach_new_title(message: Message, state: FSMContext, bot: Bot):
    if not await _form_chat(message, state, bot):
        return
    title = _title(message)
    if not title:
        return await message.answer("⚠️ Нужна одна строка, не длиннее 40 символов.")
    await state.update_data(title=title)
    await state.set_state(Form.ach_new_desc)
    await message.answer("Теперь коротко напиши, за что её дают. Например: «за самые тёплые приветствия новичков».")


@router.message(Form.ach_new_desc)
async def on_ach_new_desc(message: Message, state: FSMContext, bot: Bot):
    data = await _form_chat(message, state, bot)
    if not data:
        return
    desc = (message.text or "").strip()
    if not desc or len(desc) > 150:
        return await message.answer("⚠️ Нужен текст, не длиннее 150 символов.")
    c = data["c"]
    custom = dict((await settings.get(c))["ach_custom"])
    code = f"c{int(time.time())}"
    custom[code] = {"t": data["title"], "d": desc}
    await settings.put(c, "ach_custom", custom)
    await state.clear()
    text, markup = await ach_item_view(c, code)
    await message.answer("✅ Ачивка создана. Админы выдают её командой <code>/ачивка</code> ответом на сообщение.\n\n"
                         + text, reply_markup=markup)


@router.callback_query(F.data == "noop")
async def on_noop(cb: CallbackQuery):
    await cb.answer()

@router.message(Form.text_value)
async def on_text_value(message: Message, state: FSMContext, bot: Bot):
    data = await _form_chat(message, state, bot)
    if not data:
        return
    c, key = data["c"], data["k"]
    item = texts.TEXTS[key]
    if not message.text:
        return await message.answer("⚠️ Нужен текст.")
    value = message.text.strip() if item.fmt in ("plain", "button") else message.html_text
    if len(value) > texts.LIMITS[item.fmt]:
        return await message.answer(f"⚠️ Слишком длинно: можно до {texts.LIMITS[item.fmt]} символов.")
    unknown = set(re.findall(r"\{(\w+)\}", value)) - set(item.vars)
    if unknown:
        allowed = ", ".join(f"<code>{{{v}}}</code>" for v in item.vars) or "нет"
        return await message.answer(f"⚠️ Таких подстановок нет: {', '.join(sorted(unknown))}. Можно: {allowed}.")
    try:
        await preview(bot, message.chat.id, key, value)
    except TelegramAPIError as e:
        return await message.answer(f"⚠️ Telegram не принял такой текст: {escape(e.message)}. Попробуй ещё раз.")
    custom = dict((await settings.get(c))["texts"])
    custom[key] = value
    await settings.put(c, "texts", custom)
    await state.clear()
    text, markup = await text_item_view(c, key)
    await message.answer("✅ Сохранено. Выше — как это будет выглядеть.\n\n" + text, reply_markup=markup)


@router.message(Form.post_channel)
async def on_post_channel(message: Message, state: FSMContext, bot: Bot):
    data = await _form_chat(message, state, bot)
    if not data:
        return
    origin = message.forward_origin
    name = (message.text or "").strip().replace("https://t.me/", "@")
    try:
        if origin and origin.type == "channel":
            channel = origin.chat
        elif name.startswith("@"):
            channel = await bot.get_chat(name)
        else:
            return await message.answer("⚠️ Перешли пост из канала или отправь @username канала.")
        if channel.type != "channel":
            return await message.answer("⚠️ Это не канал.")
        me = await bot.get_chat_member(channel.id, (await bot.me()).id)
        if me.status != "administrator":
            return await message.answer("⚠️ Я не админ этого канала. Добавь меня и пришли пост ещё раз.")
        who = await bot.get_chat_member(channel.id, message.from_user.id)
    except TelegramAPIError as e:
        return await message.answer(f"⚠️ Не вижу этот канал: {escape(e.message)}. Добавь меня туда админом и попробуй ещё раз.")
    if message.from_user.id not in config.owners and who.status not in ("administrator", "creator"):
        return await message.answer("⚠️ Настроить копирование может только админ этого канала.")
    await state.update_data(channel=channel.id, title=channel.title or str(channel.id))
    await state.set_state(Form.post_tag)
    await message.answer(f"Канал: <b>{escape(channel.title or '')}</b>. Теперь отправь тег, например <code>#гайд</code>.",
                         reply_markup=await _cancel(data["c"], "sec", "posts"))


@router.message(Form.post_tag)
async def on_post_tag(message: Message, state: FSMContext, bot: Bot):
    data = await _form_chat(message, state, bot)
    if not data:
        return
    tag = (message.text or "").strip().lstrip("#").lower()
    if not re.fullmatch(r"\w+", tag) or tag.isdigit():
        return await message.answer("⚠️ Нужен один тег из букв и цифр, например <code>#гайд</code>.")
    await state.update_data(tag=tag)
    c = data["c"]
    topics = [(0, "Общая")] + [(r["thread_id"], r["name"]) for r in await db.topics(c)]
    await message.answer(f"В какую тему публиковать посты с <code>#{escape(tag)}</code>? Тема появится в списке, когда в ней "
                         "кто-нибудь напишет.", reply_markup=kb([[btn(name[:60], a="prt", c=c, k=str(tid))] for tid, name in topics]
                                                                 + [[btn("« Отмена", a="sec", c=c, k="posts")]]))


@router.message(Form.access_user)
async def on_access_user(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    c = data["c"]
    if not await is_chief(bot, message.from_user.id, c):
        await state.clear()
        return await message.answer("Доступ раздают только владелец бота и создатель чата.")
    origin, raw = message.forward_origin, (message.text or "").strip()
    uid, name = None, None
    if origin is not None:
        if origin.type != "user":
            return await message.answer("⚠️ Этот человек скрыл аккаунт при пересылке. Отправь его @username или ID.")
        uid, name = origin.sender_user.id, origin.sender_user.full_name
    elif raw.startswith("@"):
        uid = await db.find_user(raw[1:])
        if not uid:
            return await message.answer("⚠️ Не знаю такого юзернейма: этот человек ещё не писал в чатах с ботом. "
                                        "Перешли его сообщение или отправь ID.")
        name = raw
    elif raw.isdigit():
        uid = int(raw)
    else:
        return await message.answer("⚠️ Перешли сообщение человека, или отправь @username или ID.")
    if not name:
        try:
            name = (await bot.get_chat_member(c, uid)).user.full_name
        except TelegramAPIError:
            name = str(uid)
    users = dict((await settings.get(c))["panel_users"])
    users[str(uid)] = name
    await settings.put(c, "panel_users", users)
    await state.clear()
    text, markup = await access_view(c)
    await message.answer(f"✅ {escape(name)} теперь может настраивать бота через /start.\n\n" + text, reply_markup=markup)
