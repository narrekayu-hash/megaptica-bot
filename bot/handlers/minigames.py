"""Мини-игры: редактор игр в личке (панель → 🎲 Мини-игры), лобби и раунды в чате, фото для «Стоп-кадра».

Игроки записываются и отвечают кнопками прямо под постом игры: результат нажатия видит только нажавший.
В «Стоп-кадре» кнопка ведёт в личку к боту, фото проверяет ведущий — тот, кто запустил игру.
"""
import asyncio
import json
import logging
import re
import time
from contextlib import suppress
from html import escape, unescape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import minigames as M
from .. import settings, texts
from ..db import db
from ..texts import t
from ..utils import can_manage, fmt_duration, is_moderator, later, mention

router = Router(name="minigames")
router.message.filter(F.chat.type == "private")
chat_router = Router(name="minigames_chat")
log = logging.getLogger(__name__)

_sessions = {}  # chat_id -> идущая игра
SHOWN_WINNERS = 10   # сколько имён помещается под вопросом
SHOWN_ROWS = 50      # сколько строк в итогах
MEDALS = ["🥇", "🥈", "🥉"]


class G(CallbackData, prefix="g"):
    """Кнопки редактора игр в личке."""
    a: str
    c: int
    i: int = 0  # id игры
    r: int = 0  # номер раунда, поля или вида игры


class Lobby(CallbackData, prefix="gl"):
    join: int


class Ans(CallbackData, prefix="ga"):
    r: int
    i: int


class Judge(CallbackData, prefix="gj"):
    c: int
    u: int
    r: int
    ok: int


class Edit(StatesGroup):
    field = State()
    q_photo = State()
    q_text = State()
    q_options = State()
    q_correct = State()


def btn(text: str, **kw) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=G(**kw).pack())


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _name(s: M.Session, uid: int) -> str:
    return escape(s.players[uid][:25])


# --- посты игры в чате ---
async def _send(bot: Bot, s: M.Session, text: str, photo=None, markup=None):
    kw = {"message_thread_id": s.thread, "reply_markup": markup}
    try:
        if photo:
            return await bot.send_photo(s.chat_id, photo, caption=text, **kw)
        return await bot.send_message(s.chat_id, text, **kw)
    except TelegramAPIError as e:
        log.warning("Мини-игра: не удалось отправить сообщение в %s: %s", s.chat_id, e)
        return None


async def _post(bot: Bot, s: M.Session, text: str, photo, markup) -> bool:
    """Главный пост игры (лобби или вопрос), который потом обновляется."""
    msg = await _send(bot, s, text, photo, markup)
    if msg is None and photo:  # картинка могла пропасть — играем без неё
        photo = None
        msg = await _send(bot, s, text, None, markup)
    s.post_id, s.post_photo = (msg.message_id if msg else None), bool(photo)
    return msg is not None


async def _edit(bot: Bot, s: M.Session, text: str, markup):
    if not s.post_id:
        return
    with suppress(TelegramAPIError):  # «не изменилось» и лимит на частые правки игре не мешают
        if s.post_photo:
            await bot.edit_message_caption(chat_id=s.chat_id, message_id=s.post_id, caption=text, reply_markup=markup)
        else:
            await bot.edit_message_text(text, chat_id=s.chat_id, message_id=s.post_id, reply_markup=markup)


async def _drop_post(bot: Bot, s: M.Session):
    if s.post_id:
        with suppress(TelegramAPIError):
            await bot.delete_message(s.chat_id, s.post_id)
    s.post_id = None


async def lobby_view(s: M.Session, live: bool = True):
    c, g = s.chat_id, s.game
    text = await t(c, "games.lobby", title=g["title"], about=g["about"] + "\n\n" if g["about"] else "",
                   prizes=g["prizes"] + "\n\n" if g["prizes"] else "", places=g["places"], now=len(s.players),
                   need=g["need"])
    join = Lobby(join=1).pack() if live else "noop"
    leave = Lobby(join=0).pack() if live else "noop"
    return text, kb([[InlineKeyboardButton(text=await t(c, "games.btn_join"), callback_data=join),
                      InlineKeyboardButton(text=await t(c, "games.btn_leave"), callback_data=leave)]])


def round_photo(s: M.Session):
    """Картинка раунда; в игре без записи анонса нет, поэтому первый раунд берёт картинку анонса, если своей нет."""
    if s.question["photo"] or s.signup or s.round:
        return s.question["photo"]
    return s.game["photo"]


async def round_view(bot: Bot, s: M.Session, live: bool = True):
    c, q, g = s.chat_id, s.question, s.game
    cfg = await settings.get(c)
    winners = ""
    if cfg["games_show_winners"] and s.right:
        order = sorted(s.right, key=s.right.get)
        lines = [f"<b>{i}.</b> {_name(s, uid)}" for i, uid in enumerate(order[:SHOWN_WINNERS], 1)]
        if len(order) > SHOWN_WINNERS:
            lines.append(f"…и ещё {len(order) - SHOWN_WINNERS}")
        winners = "\n\n" + await t(c, "games.winners") + "\n" + "\n".join(lines)
    limit = cfg["games_round_time"]
    # без записи анонса нет: условия и призы показываем над первым вопросом
    intro = "" if s.signup or s.round else "".join(x + "\n\n" for x in (g["about"], g["prizes"]) if x)
    text = await t(c, "games.round", title=g["title"], round=s.round + 1, rounds=len(g["rounds"]), intro=intro,
                   question=q["text"], answered=s.answered, total=f" / {len(s.players)}" if s.signup else "",
                   timer=f"\n⏱ На раунд: {fmt_duration(limit)}" if limit else "", winners=winners)
    if g["type"] == "quiz":
        rows = [[InlineKeyboardButton(text=opt, callback_data=Ans(r=s.round, i=i).pack() if live else "noop")]
                for i, opt in enumerate(q["options"])]
    elif live:
        me = await bot.me()
        rows = [[InlineKeyboardButton(text=await t(c, "games.btn_photo"),
                                      url=f"https://t.me/{me.username}?start=gp{c}_{s.round}")]]
    else:
        rows = [[InlineKeyboardButton(text=await t(c, "games.btn_photo"), callback_data="noop")]]
    return text, kb(rows)


# --- ход игры ---
def _alive(s: M.Session) -> bool:
    return _sessions.get(s.chat_id) is s


async def launch(bot: Bot, c: int, game_id: int, game: dict, host: int, topic: str) -> bool:
    s = M.Session(chat_id=c, game_id=game_id, game=game, host=host, thread=int(topic) or None)
    _sessions[c] = s
    if s.signup:
        text, markup = await lobby_view(s)
        posted = await _post(bot, s, text, game["photo"], markup)
    else:
        s.state = "pause"
        posted = await next_round(bot, s)
    if not posted:
        del _sessions[c]
    return posted


async def begin(bot: Bot, s: M.Session):
    """Лобби закрывается, начинается первый раунд."""
    if not _alive(s) or s.state != "lobby":
        return
    s.state = "pause"
    await _send(bot, s, await t(s.chat_id, "games.start"))
    await _drop_post(bot, s)
    await next_round(bot, s)


async def next_round(bot: Bot, s: M.Session):
    s.open(time.time())
    text, markup = await round_view(bot, s)
    if not await _post(bot, s, text, round_photo(s), markup):
        return False
    limit = (await settings.get(s.chat_id))["games_round_time"]
    if limit:
        later(limit, end_round(bot, s, s.round))
    return True


async def end_round(bot: Bot, s: M.Session, rnd: int):
    """Итоги раунда. Вызывается, когда ответили все, вышло время или ведущий нажал «Завершить раунд»."""
    if not _alive(s) or s.state != "round" or s.round != rnd:
        return
    s.state = "pause"  # сразу, чтобы два вызова подряд не подвели итоги дважды
    c = s.chat_id
    cfg = await settings.get(c)
    late = list(s.pending)  # «Стоп-кадр»: фото, которые ведущий не успел проверить, уже не засчитаются
    rows, others = s.close(cfg["games_first_bonus"])
    for uid in late:
        with suppress(TelegramAPIError):
            await bot.send_message(uid, await t(c, "games.photo_late"))
    if cfg["games_cleanup"]:
        await _drop_post(bot, s)
    else:
        await _edit(bot, s, (await round_view(bot, s))[0], None)
    if len(s.game["rounds"]) > 1:  # в игре из одного раунда итоги раунда повторили бы финал
        await _send(bot, s, await results_text(s, rnd, rows, others))
    if not s.has_next:
        return await finish(bot, s)
    pause = cfg["games_pause"]
    note = await _send(bot, s, await t(c, "games.next", left=fmt_duration(pause))) if pause else None
    await asyncio.sleep(pause)
    if note:
        with suppress(TelegramAPIError):
            await note.delete()
    if _alive(s) and s.state == "pause":
        await next_round(bot, s)


async def results_text(s: M.Session, rnd: int, rows, others) -> str:
    c = s.chat_id
    if rows:
        lines = [await t(c, "games.res_line", place=i, user=_name(s, uid), points=points, time=f"{seconds:.1f}")
                 for i, (uid, seconds, points) in enumerate(rows[:SHOWN_ROWS], 1)]
        winners = await t(c, "games.res_head") + "\n" + "\n".join(lines)
    else:
        winners = await t(c, "games.res_none")
    rest = ""
    if others and s.signup:  # без записи «остальные» — случайные прохожие, их не перечисляем
        names = [f"• {_name(s, uid)}" for uid in others[:SHOWN_ROWS]]
        rest = "\n\n" + await t(c, "games.res_others") + "\n" + "\n".join(names)
    return await t(c, "games.results", title=s.game["title"], round=rnd + 1, winners=winners, others=rest)


async def final_text(s: M.Session) -> str:
    c = s.chat_id
    lines = []
    # без записи в таблице только те, кто набрал очки: остальные просто нажимали кнопку
    table = [row for row in s.standings() if s.signup or row[1]]
    for i, (uid, score, seconds) in enumerate(table[:SHOWN_ROWS]):
        medal = (MEDALS[i] if i < 3 else "🏅") if i < s.game["places"] else "🎗"
        lines.append(await t(c, "games.final_line", medal=medal, user=mention(uid, s.players[uid][:25]), score=score,
                             time=f"{seconds:.1f}"))
    table = "\n".join(lines) or await t(c, "games.res_none")
    return await t(c, "games.final", title=s.game["title"], table=table)


async def finish(bot: Bot, s: M.Session):
    s.state = "done"
    _sessions.pop(s.chat_id, None)
    await _send(bot, s, await final_text(s))
    with suppress(TelegramAPIError):
        await bot.send_message(s.host, f"✅ Игра «{s.game['title']}» завершена, итоги опубликованы.")


async def stop(bot: Bot, s: M.Session):
    """Ведущий остановил игру: итоги не подводятся."""
    was_lobby = s.state == "lobby"
    s.state = "done"
    _sessions.pop(s.chat_id, None)
    if was_lobby:
        await _drop_post(bot, s)
    elif s.post_id:
        with suppress(TelegramAPIError):
            await bot.edit_message_reply_markup(chat_id=s.chat_id, message_id=s.post_id, reply_markup=None)
    await _send(bot, s, await t(s.chat_id, "games.stopped"))


async def _after_answer(bot: Bot, s: M.Session):
    """Кто-то ответил верно или выбыл: обновляем счётчик под вопросом и, если ответили все, подводим итоги."""
    rnd = s.round
    await _edit(bot, s, *await round_view(bot, s))
    if s.state == "round" and s.round == rnd and s.complete:
        later(0, end_round(bot, s, rnd))


# --- кнопки под постами игры ---
@router.callback_query(Lobby.filter())
async def on_lobby(cb: CallbackQuery, callback_data: Lobby, bot: Bot):
    c = cb.message.chat.id
    s = _sessions.get(c)
    if not s or cb.message.message_id != s.post_id:
        return await cb.answer(await t(c, "games.over"), show_alert=True)
    user = cb.from_user
    result = s.join(user.id, user.full_name) if callback_data.join else s.leave(user.id)
    await cb.answer(await t(c, f"games.{result}"), show_alert=result == "over")
    if result in ("joined", "left"):
        await _edit(bot, s, *await lobby_view(s))
        if s.state == "lobby" and s.full:
            later(0, begin(bot, s))


@router.callback_query(Ans.filter())
async def on_answer(cb: CallbackQuery, callback_data: Ans, bot: Bot):
    c = cb.message.chat.id
    s = _sessions.get(c)
    if not s or s.game["type"] != "quiz" or (callback_data.r == s.round and cb.message.message_id != s.post_id):
        return await cb.answer(await t(c, "games.over"), show_alert=True)
    s.enter(cb.from_user.id, cb.from_user.full_name)
    result = s.answer(cb.from_user.id, callback_data.r, callback_data.i, time.time())
    await cb.answer(await t(c, f"games.{result}"), show_alert=True)
    if result in ("right", "wrong"):
        await _after_answer(bot, s)


# --- «Стоп-кадр»: фото в личке ---
@router.message(CommandStart(deep_link=True, magic=F.args.regexp(r"^gp-?\d+_\d+$")))
async def on_photo_link(message: Message, command: CommandObject):
    c, rnd = map(int, command.args[2:].split("_"))
    s = _sessions.get(c)
    if s and s.game["type"] == "photo" and rnd == s.round:
        s.enter(message.from_user.id, message.from_user.full_name)
    result = s.want_photo(message.from_user.id, rnd) if s and s.game["type"] == "photo" else "over"
    c = c if s else 0  # номер чата из чужой ссылки может быть любым — настройки для него не заводим
    if result in ("ask", "pending"):
        return await message.answer(await t(c, f"games.photo_{result}"))
    await message.answer(escape(await t(c, f"games.{result}")))  # это тексты всплывающих окон, в них нет разметки


class PhotoAnswer(Filter):
    """Фото в личке от игрока, который нажал «Отправить скриншот» и ещё не ответил."""

    async def __call__(self, message: Message):
        if not message.photo:
            return False
        for s in _sessions.values():
            if message.from_user.id in s.waiting:
                return {"s": s}
        return False


@router.message(PhotoAnswer())
async def on_photo(message: Message, bot: Bot, s: M.Session):
    user, c, rnd = message.from_user, s.chat_id, s.round
    if not s.submit(user.id, time.time()):
        return await message.answer(escape(await t(c, "games.over")))
    who = mention(user.id, user.full_name) + (f" (@{user.username})" if user.username else "")
    markup = kb([[InlineKeyboardButton(text="✅ Одобрить", callback_data=Judge(c=c, u=user.id, r=rnd, ok=1).pack()),
                  InlineKeyboardButton(text="❌ Отклонить", callback_data=Judge(c=c, u=user.id, r=rnd, ok=0).pack())]])
    try:
        await bot.send_photo(s.host, message.photo[-1].file_id, reply_markup=markup,
                             caption=f"📸 «{s.game['title']}», раунд {rnd + 1}\nИгрок: {who}")
    except TelegramAPIError as e:
        log.warning("Мини-игра: фото не дошло до ведущего %s: %s", s.host, e)
        s.unsubmit(user.id)
        return await message.answer("⚠️ Не получилось передать фото ведущему. Пришли его ещё раз чуть позже.")
    await message.answer(await t(c, "games.photo_sent"))


@router.callback_query(Judge.filter())
async def on_judge(cb: CallbackQuery, callback_data: Judge, bot: Bot):
    c, uid = callback_data.c, callback_data.u
    if not await can_manage(bot, cb.from_user.id, c):
        return await cb.answer("У тебя больше нет прав настраивать этот чат.", show_alert=True)
    s = _sessions.get(c)
    result = s.judge(uid, callback_data.r, bool(callback_data.ok)) if s else "over"
    verdict = {"right": "✅ Одобрено", "retry": "❌ Отклонено, игрок может прислать другое",
               "wrong": "❌ Отклонено, игрок выбыл из раунда", "over": "Раунд уже закрыт, фото не засчитано"}[result]
    await cb.answer(verdict)
    with suppress(TelegramAPIError):
        await cb.message.edit_caption(caption=f"{cb.message.html_text}\n\n<b>Решение:</b> {verdict}", reply_markup=None)
    if result == "over":
        return
    with suppress(TelegramAPIError):
        await bot.send_message(uid, await t(c, {"right": "games.photo_ok", "retry": "games.photo_retry",
                                               "wrong": "games.photo_out"}[result]))
    if result in ("right", "wrong"):
        await _after_answer(bot, s)


# --- тема для игр ---
@chat_router.message(Command("gameshere", prefix="/!"))
async def on_here(message: Message, command: CommandObject, bot: Bot):
    if not await is_moderator(bot, message):
        return
    chat = message.chat
    if (command.args or "").strip().lower() in ("off", "выкл", "0", "-"):
        await settings.put(chat.id, "games_topic", None)
        return await message.reply(await t(chat.id, "games.here_off"))
    await settings.put(chat.id, "games_topic", str(message.message_thread_id if message.is_topic_message else 0))
    await message.reply(await t(chat.id, "games.here"))


# --- редактор игр в личке ---
KIND_NAMES = {"quiz": "викторина: участники выбирают ответ кнопкой под вопросом",
              "photo": "«Стоп-кадр»: участники присылают боту в личку скриншот, ведущий его проверяет"}
EDITABLE = ["title", "need", "places", "about", "prizes", "photo"]
PROMPTS = {
    "title": "Отправь название игры: одна строка, до {title} знаков.",
    "need": "Сколько игроков нужно собрать, чтобы игра началась сама? Число от 1 до 200.",
    "places": "Сколько призовых мест будет в финальной таблице? Число от 1 до 50.",
    "about": "Отправь условия игры для анонса, до {about} знаков. Оформление и эмодзи сохранятся. «-» — убрать.",
    "prizes": ("Напиши, что получат победители, — бот покажет это в анонсе как есть. Например:\n"
               "<code>🎁 Каждому победителю — по 1 ❤️</code>\nили по местам:\n"
               "<code>🥇 3 ❤️\n🥈 2 ❤️\n🥉 1 ❤️</code>\n"
               "До {prizes} знаков, оформление и эмодзи сохранятся. «-» — убрать."),
    "photo": "Пришли картинку для анонса. «-» — без картинки.",
}


async def _load(c: int, game_id: int):
    """(ключ в базе, игра) или (None, None), если такой игры в этом чате нет."""
    row = await db.content_item(game_id)
    if not row or row["kind"] != M.KIND or row["chat_id"] != c:
        return None, None
    return row["name"], json.loads(row["data"])


def _short(html: str, size: int = 40) -> str:
    plain = unescape(re.sub(r"<[^>]+>", "", html)).replace("\n", " ")
    return escape(plain[:size] + ("…" if len(plain) > size else ""))


async def list_view(c: int):
    from .panel import P
    rows = []
    s = _sessions.get(c)
    if s:
        rows.append([btn(f"🔴 Сейчас идёт: {unescape(s.game['title'])}"[:60], a="live", c=c)])
    for r in await db.content_list(c, M.KIND):
        game = json.loads(r["data"])
        icon = "🎯" if game["type"] == "quiz" else "📸"
        rows.append([btn(f"{icon} {unescape(game['title'])} · раундов: {len(game['rounds'])}"[:60], a="card", c=c, i=r["id"])])
    rows.append([btn("➕ Викторина", a="new", c=c, r=0), btn("➕ Стоп-кадр", a="new", c=c, r=1)])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data=P(a="sec", c=c, k="games").pack())])
    return ("<b>🎲 Игры чата</b>\n"
            f"• <b>Викторина</b> — {KIND_NAMES['quiz']}.\n"
            f"• <b>Стоп-кадр</b> — {KIND_NAMES['photo']}.\n\n"
            "Собери игру один раз: она сохранится, и её можно запускать снова. Нажми на игру, чтобы изменить, "
            "посмотреть или запустить её."), kb(rows)


async def card_view(c: int, game_id: int):
    _, g = await _load(c, game_id)
    if g is None:
        return await list_view(c)
    quiz = g["type"] == "quiz"
    signup = M.signup(g)
    start = (f"Запись перед игрой: да, старт, когда соберётся игроков: {g['need']}." if signup else
             "Запись перед игрой: нет. Сразу публикуется первый раунд с условиями и призами, играть может любой. "
             f"Раунд закончится, когда верно ответят {g['places']} (сколько призовых мест), по «Времени на раунд» "
             "из раздела «🎲 Мини-игры» или по кнопке ведущего «Завершить раунд».")
    lines = [f"<b>{g['title']}</b>", f"Вид: {KIND_NAMES[g['type']]}.", start, f"Призовых мест: {g['places']}.",
             "При ошибке: " + ("можно пробовать ещё" if g["retries"] else "игрок выбывает из раунда без очков") + ".",
             f"Условия: {_short(g['about'], 60) if g['about'] else 'нет'}",
             f"Призы: {_short(g['prizes'], 60) if g['prizes'] else 'нет'}",
             f"Картинка анонса: {'есть' if g['photo'] else 'нет'}", "", f"<b>Раунды ({len(g['rounds'])}):</b>"]
    for n, q in enumerate(g["rounds"], 1):
        tail = f" — верный ответ: {escape(q['options'][q['correct']])}" if quiz else ""
        lines.append(f"{n}. {'🖼 ' if q['photo'] else ''}{_short(q['text'])}{tail}")
    if quiz and g["retries"]:
        lines.append("\n<i>В викторине с повторными попытками ответ можно просто перебрать. Чтобы игроки думали, "
                     "выключи попытки.</i>")
    e = EDITABLE.index
    rows = [[btn("✏️ Название", a="edit", c=c, i=game_id, r=e("title")),
             btn(f"📝 Запись: {'✅' if signup else '❌'}", a="sig", c=c, i=game_id)],
            *([[btn(f"👥 Игроков для старта: {g['need']}", a="edit", c=c, i=game_id, r=e("need"))]] if signup else []),
            [btn(f"🏆 Призовых мест: {g['places']}", a="edit", c=c, i=game_id, r=e("places")),
             btn(f"🔁 Попытки: {'✅' if g['retries'] else '❌'}", a="tog", c=c, i=game_id)],
            [btn("📝 Условия", a="edit", c=c, i=game_id, r=e("about")),
             btn("🎁 Призы", a="edit", c=c, i=game_id, r=e("prizes")),
             btn("🖼 Картинка", a="edit", c=c, i=game_id, r=e("photo"))]]
    rows += [[btn(f"🗑 Раунд {n}: {unescape(_short(q['text'], 30))}", a="rdel", c=c, i=game_id, r=n - 1)]
             for n, q in enumerate(g["rounds"], 1)]
    if len(g["rounds"]) < M.MAX_ROUNDS:
        rows.append([btn("➕ Добавить раунд", a="radd", c=c, i=game_id)])
    rows.append([btn("👁 Предпросмотр", a="prev", c=c, i=game_id), btn("▶️ Запустить", a="run", c=c, i=game_id)])
    rows.append([btn("🗑 Удалить игру", a="del", c=c, i=game_id), btn("« Назад", a="list", c=c)])
    return "\n".join(lines), kb(rows)


async def live_view(c: int):
    s = _sessions.get(c)
    if not s:
        return await list_view(c)
    lines = [f"🔴 <b>{s.game['title']}</b>"]
    if s.state == "lobby":
        lines.append(f"Идёт набор: {len(s.players)} из {s.game['need']}. Игра начнётся сама, когда наберутся все, "
                     "или по кнопке «Начать сейчас».")
        rows = [[btn("▶️ Начать сейчас", a="go", c=c)], [btn("⏹ Отменить игру", a="stop", c=c)]]
    else:
        total = f" из {len(s.players)}" if s.signup else ""
        lines.append(f"Раунд {s.round + 1} из {len(s.game['rounds'])}. Ответили: {s.answered}{total}.")
        if not s.signup:
            lines.append(f"Игра без записи: раунд закончится, когда наберётся победителей: {s.game['places']} "
                         f"(сейчас {len(s.right)}), по таймеру или по кнопке «Завершить раунд».")
        if s.pending:
            lines.append(f"Фото ждут твоей проверки: {len(s.pending)}. Непроверенные к концу раунда не засчитаются.")
        rows = [[btn("⏭ Завершить раунд", a="skip", c=c)], [btn("⏹ Остановить игру", a="stop", c=c)]]
    rows.append([btn("🔄 Обновить", a="live", c=c), btn("« Назад", a="list", c=c)])
    return "\n".join(lines), kb(rows)


SAMPLE_NAMES = ["Аня", "Боря", "Вика", "Гоша", "Даша", "Егор"]
SAMPLE_TIMES = [4.2, 7.9, 12.5, 15.1, 21.8, 30.4]


def _note(text: str):
    """Подпись между постами предпросмотра: что это за пост и когда он появится."""
    return None, f"<i>👁 {text}</i>", None


async def preview_views(bot: Bot, uid: int, c: int, game_id: int, g: dict):
    """Все посты игры по порядку, как их увидит тема, с подписями. Имена и время ответов — примеры."""
    cfg = await settings.get(c)
    s = M.Session(chat_id=c, game_id=game_id, game=g, host=uid, thread=None)
    rounds = len(g["rounds"])
    views = [_note(f"Предпросмотр «{g['title']}». Ниже посты в том порядке, в каком они появятся в теме. "
                   "Кнопки здесь не работают, имена игроков и время — примеры.")]
    if s.signup:
        views.append(_note("1. Анонс с набором игроков. Счётчик растёт, когда участники нажимают «Участвовать»."))
        views.append((g["photo"], *await lobby_view(s, live=False)))
        views.append(_note(f"2. Набралось {g['need']}: анонс удаляется, появляется это сообщение и сразу первый раунд."))
        views.append((None, await t(c, "games.start"), None))
    else:
        views.append(_note("Записи нет: после «▶️ Запустить» сразу появляется первый раунд. Условия и призы — в нём."))
    if not rounds:
        views.append(_note("Раундов пока нет. Добавь раунд в карточке игры, чтобы увидеть задание."))
        return views
    # игроки-примеры: столько, сколько нужно, чтобы показать и победителей, и тех, кто без очков
    winners = min(g["places"], len(SAMPLE_NAMES)) if not s.signup else min(3, len(SAMPLE_NAMES) - 1)
    total = winners if not s.signup else min(max(g["need"], winners + 1), len(SAMPLE_NAMES))
    s.players = {-(i + 1): SAMPLE_NAMES[i] for i in range(total)}
    s.open(0)
    views.append(_note("Раунд 1 сразу после публикации."))
    views.append((round_photo(s), *await round_view(bot, s, live=False)))
    s.right = {-(i + 1): SAMPLE_TIMES[i] for i in range(winners)}
    s.out = set(list(s.players)[winners:]) if s.signup else set()
    if cfg["games_show_winners"]:
        views.append(_note("Тот же пост, когда ответили игроки: он обновляется сам после каждого верного ответа."))
        views.append((round_photo(s), *await round_view(bot, s, live=False)))
    close = ("ответили все записавшиеся" if s.signup else f"верно ответили {g['places']} (столько призовых мест)")
    timer = f", вышло «Время на раунд» ({fmt_duration(cfg['games_round_time'])})" if cfg["games_round_time"] else ""
    after = "пост с вопросом удаляется" if cfg["games_cleanup"] else "у поста с вопросом пропадают кнопки"
    views.append(_note(f"Раунд закрывается, когда {close}{timer} или ведущий нажал «Завершить раунд». "
                       f"Тогда {after} и " + ("выходят итоги раунда:" if rounds > 1 else
                                              "сразу выходят итоги игры:")))
    rows, others = s.close(cfg["games_first_bonus"])
    if rounds > 1:
        views.append((None, await results_text(s, 0, rows, others), None))
    for n in range(1, rounds):
        if n == 1 and cfg["games_pause"]:
            views.append(_note("Перед каждым следующим раундом на время паузы появляется это сообщение:"))
            views.append((None, await t(c, "games.next", left=fmt_duration(cfg["games_pause"])), None))
        s.round, s.state = n, "round"
        s.right, s.out = {}, set()
        views.append(_note(f"Раунд {n + 1}."))
        views.append((round_photo(s), *await round_view(bot, s, live=False)))
    if rounds > 1:
        views.append(_note("После последнего раунда — финальная таблица (здесь посчитана по одному раунду-примеру):"))
    views.append((None, await final_text(s), None))
    keys = (["games.right", "games.retry" if g["retries"] else "games.wrong"] if g["type"] == "quiz" else
            ["games.photo_ask", "games.photo_sent", "games.photo_ok", "games.photo_retry" if g["retries"] else
             "games.photo_out"] + ([] if s.signup else ["games.photo_late"]))
    if s.signup:
        keys = ["games.joined"] + keys
    where = "во всплывающих окнах после нажатия кнопки" if g["type"] == "quiz" else "в личке у бота"
    lines = [f"<i>👁 Что игрок видит сам ({where}; меняется в «✏️ Тексты» → «🎲 Мини-игры»):</i>"]
    for key in keys:
        text = await t(c, key)
        lines.append(f"• {escape(text) if texts.TEXTS[key].fmt == 'plain' else text}")
    views.append((None, "\n".join(lines), None))
    return views


async def preview(bot: Bot, uid: int, c: int, game_id: int, g: dict):
    """Показывает админу в личке всю игру: посты в теме с подписями и что видят сами игроки (кнопки не работают)."""
    for photo, text, markup in await preview_views(bot, uid, c, game_id, g):
        for attempt in range(2):  # постов много: если Telegram попросит подождать — ждём и повторяем
            try:
                if photo:
                    await bot.send_photo(uid, photo, caption=text, reply_markup=markup)
                else:
                    await bot.send_message(uid, text, reply_markup=markup)
                break
            except TelegramRetryAfter as e:
                if attempt:
                    raise
                await asyncio.sleep(e.retry_after)
        await asyncio.sleep(0.4)


@router.callback_query(G.filter())
async def on_button(cb: CallbackQuery, callback_data: G, state: FSMContext, bot: Bot):
    a, c, i, r = callback_data.a, callback_data.c, callback_data.i, callback_data.r
    await state.clear()
    if not await can_manage(bot, cb.from_user.id, c):
        return await cb.answer("У тебя больше нет прав настраивать этот чат.", show_alert=True)
    s = _sessions.get(c)
    name, g = await _load(c, i) if i else (None, None)
    if g is None and a in ("card", "tog", "sig", "edit", "radd", "rdel", "prev", "del", "del2", "run"):
        with suppress(TelegramAPIError):
            await cb.answer("Этой игры уже нет.", show_alert=True)
        a = "list"
    cancel = kb([[btn("Отмена", a="card", c=c, i=i)]])
    if a == "list":
        view = await list_view(c)
    elif a == "new":
        kind = "photo" if r else "quiz"
        name = str(time.time_ns())
        await db.content_set(c, M.KIND, name, M.new_game(kind))
        view = await card_view(c, (await db.content_get(c, M.KIND, name))["id"])
    elif a == "card":
        view = await card_view(c, i)
    elif a in ("tog", "sig"):
        if a == "tog":
            g["retries"] = not g["retries"]
        else:
            g["signup"] = not M.signup(g)
        await db.content_set(c, M.KIND, name, g)
        view = await card_view(c, i)
    elif a == "edit":
        await state.set_state(Edit.field)
        await state.update_data(c=c, i=i, f=EDITABLE[r])
        await cb.message.answer(PROMPTS[EDITABLE[r]].format(**M.LIMITS), reply_markup=cancel)
        return await cb.answer()
    elif a == "radd":
        await state.set_state(Edit.q_photo)
        await state.update_data(c=c, i=i)
        await cb.message.answer(f"<b>Раунд {len(g['rounds']) + 1}.</b> Пришли картинку к заданию. «-» — без картинки.",
                                reply_markup=cancel)
        return await cb.answer()
    elif a == "rdel":
        if r < len(g["rounds"]):
            g["rounds"].pop(r)
            await db.content_set(c, M.KIND, name, g)
        view = await card_view(c, i)
    elif a == "prev":
        await cb.answer("Показываю, как будет выглядеть игра")  # сразу: постов много, кнопка не должна «висеть»
        try:
            await preview(bot, cb.from_user.id, c, i, g)
        except TelegramAPIError as e:
            await cb.message.answer(f"⚠️ Telegram не принял пост: {escape(e.message)}")
        view = await card_view(c, i)
        return await cb.message.answer(view[0], reply_markup=view[1])
    elif a == "del":
        view = (f"Удалить игру «{g['title']}» вместе с раундами?",
                kb([[btn("🗑 Да, удалить", a="del2", c=c, i=i), btn("Нет", a="card", c=c, i=i)]]))
    elif a == "del2":
        await db.content_del(i)
        view = await list_view(c)
    elif a == "run":
        cfg = await settings.get(c)
        problem = ("Мини-игры выключены. Включи их в разделе «🎲 Мини-игры»." if not cfg["games"] else
                   "Сначала выбери тему для игр: напиши /gameshere в нужной теме чата." if cfg["games_topic"] is None else
                   "В этом чате уже идёт игра. Дождись конца или останови её." if s else
                   "Добавь хотя бы один раунд." if not g["rounds"] else None)
        if not problem and not await launch(bot, c, i, g, cb.from_user.id, cfg["games_topic"]):
            problem = "Не получилось опубликовать анонс. Проверь, что я могу писать в теме для игр."
        if problem:
            return await cb.answer(problem, show_alert=True)
        view = await live_view(c)
    elif a == "live":
        view = await live_view(c)
    elif a == "go":
        if s and s.state == "lobby":
            if not s.players:
                return await cb.answer("Пока никто не записался.", show_alert=True)
            later(0, begin(bot, s))
            await cb.answer("Начинаю игру")
            await asyncio.sleep(0.5)
        view = await live_view(c)
    elif a == "skip":
        if s and s.state == "round":
            later(0, end_round(bot, s, s.round))
            await cb.answer("Завершаю раунд: очки получат те, кто успел")
            await asyncio.sleep(0.5)
        view = await live_view(c)
    elif a == "stop":
        if s:
            await stop(bot, s)
        view = await list_view(c)
    else:
        return await cb.answer()
    with suppress(TelegramAPIError):
        await cb.answer()
    with suppress(TelegramBadRequest):  # «message is not modified» и старые сообщения
        await cb.message.edit_text(view[0], reply_markup=view[1])


async def _form(message: Message, state: FSMContext, bot: Bot):
    """(данные формы, ключ игры, игра), если у человека есть права на чат и игра ещё существует."""
    data = await state.get_data()
    if not await can_manage(bot, message.from_user.id, data["c"]):
        await state.clear()
        await message.answer("У тебя больше нет прав настраивать этот чат.")
        return None
    name, g = await _load(data["c"], data["i"])
    if g is None:
        await state.clear()
        await message.answer("Этой игры уже нет. Открой список игр заново: /start")
        return None
    return data, name, g


async def _saved(message: Message, state: FSMContext, c: int, game_id: int, name: str, g: dict):
    await db.content_set(c, M.KIND, name, g)
    await state.clear()
    text, markup = await card_view(c, game_id)
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=markup)


def _rich(message: Message, limit: int) -> str:
    """Текст сообщения с оформлением; ValueError, если он пустой или слишком длинный."""
    if not message.text:
        raise ValueError("Нужен текст.")
    if len(message.text) > limit:
        raise ValueError(f"Слишком длинно: {len(message.text)} знаков, а можно до {limit}.")
    return message.html_text


@router.message(Edit.field)
async def on_field(message: Message, state: FSMContext, bot: Bot):
    form = await _form(message, state, bot)
    if not form:
        return
    data, name, g = form
    f, raw = data["f"], (message.text or "").strip()
    try:
        if f == "photo":
            if not message.photo and raw != "-":
                raise ValueError("Нужна картинка (фото), либо «-», чтобы убрать её.")
            g[f] = message.photo[-1].file_id if message.photo else None
        elif f in M.NUMBERS:
            low, high = M.NUMBERS[f]
            if not raw.isdigit() or not low <= int(raw) <= high:
                raise ValueError(f"Нужно число от {low} до {high}.")
            g[f] = int(raw)
        elif f == "title":
            if not raw or "\n" in raw or len(raw) > M.LIMITS[f]:
                raise ValueError(f"Нужна одна строка, не длиннее {M.LIMITS[f]} знаков.")
            g[f] = escape(raw)
        else:
            g[f] = "" if raw == "-" else _rich(message, M.LIMITS[f])
    except ValueError as e:
        return await message.answer(f"⚠️ {e}")
    await _saved(message, state, data["c"], data["i"], name, g)


@router.message(Edit.q_photo)
async def on_q_photo(message: Message, state: FSMContext, bot: Bot):
    form = await _form(message, state, bot)
    if not form:
        return
    if not message.photo and (message.text or "").strip() != "-":
        return await message.answer("⚠️ Нужна картинка (фото), либо «-», если задание без картинки.")
    await state.update_data(photo=message.photo[-1].file_id if message.photo else None)
    await state.set_state(Edit.q_text)
    what = "вопрос" if form[2]["type"] == "quiz" else "задание: что нужно сфотографировать"
    await message.answer(f"Теперь отправь {what}, до {M.LIMITS['question']} знаков. Оформление сохранится.")


@router.message(Edit.q_text)
async def on_q_text(message: Message, state: FSMContext, bot: Bot):
    form = await _form(message, state, bot)
    if not form:
        return
    data, name, g = form
    try:
        text = _rich(message, M.LIMITS["question"])
    except ValueError as e:
        return await message.answer(f"⚠️ {e}")
    if g["type"] == "photo":
        g["rounds"].append({"photo": data["photo"], "text": text})
        return await _saved(message, state, data["c"], data["i"], name, g)
    await state.update_data(text=text)
    await state.set_state(Edit.q_options)
    await message.answer(f"Отправь варианты ответа, от 2 до {M.MAX_OPTIONS}: каждый с новой строки или все через запятую. "
                         f"Один вариант — до {M.LIMITS['option']} знаков.")


@router.message(Edit.q_options)
async def on_q_options(message: Message, state: FSMContext, bot: Bot):
    if not await _form(message, state, bot):
        return
    options = M.parse_options(message.text or "")
    if not 2 <= len(options) <= M.MAX_OPTIONS:
        return await message.answer(f"⚠️ Нужно от 2 до {M.MAX_OPTIONS} вариантов, а получилось {len(options)}.")
    if len(set(options)) < len(options) or any(len(o) > M.LIMITS["option"] for o in options):
        return await message.answer(f"⚠️ Варианты не должны повторяться и быть длиннее {M.LIMITS['option']} знаков.")
    await state.update_data(options=options)
    await state.set_state(Edit.q_correct)
    numbered = "\n".join(f"{n}. {escape(o)}" for n, o in enumerate(options, 1))
    await message.answer(f"{numbered}\n\nНапиши <b>номер</b> правильного ответа.")


@router.message(Edit.q_correct)
async def on_q_correct(message: Message, state: FSMContext, bot: Bot):
    form = await _form(message, state, bot)
    if not form:
        return
    data, name, g = form
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= len(data["options"]):
        return await message.answer(f"⚠️ Нужен номер от 1 до {len(data['options'])}.")
    g["rounds"].append({"photo": data["photo"], "text": data["text"], "options": data["options"],
                        "correct": int(raw) - 1})
    await _saved(message, state, data["c"], data["i"], name, g)
