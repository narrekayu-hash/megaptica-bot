import json

from .db import db
from .sky import ZONES

ACTIONS = {"delete": "удалить", "warn": "варн", "mute": "мут", "kick": "кик", "ban": "бан"}


def _pick(*keys):
    return {k: ACTIONS[k] for k in keys}


# ключ: (название в панели, тип, параметры типа)
FIELDS = {
    "warn_limit": ("Лимит варнов", "int", (1, 20)),
    "warn_action": ("За лимит варнов", "enum", _pick("mute", "kick", "ban")),
    "mute_minutes": ("Срок авто-наказаний, мин", "int", (0, 525600)),
    "log_chat": ("Лог-чат", "chat", None),
    "antiflood": ("Антифлуд", "bool", None),
    "flood_limit": ("Макс. сообщений", "int", (2, 50)),
    "flood_seconds": ("За секунд", "int", (1, 120)),
    "flood_action": ("За флуд", "enum", _pick("warn", "mute", "kick", "ban")),
    "autoban": ("Умный антиспам", "bool", None),
    "autoban_days": ("Новичок — первые, дней", "int", (0, 365)),
    "spam_level": ("Строгость", "enum", {"soft": "мягко", "normal": "обычно", "strict": "строго"}),
    "cas": ("Проверять по базе CAS", "bool", None),
    "old_delete": ("Давним: удалять от, баллов", "int", (0, 100)),
    "antilink": ("Запрет ссылок", "bool", None),
    "link_whitelist": ("Разрешённые сайты", "list", None),
    "antiforward": ("Запрет пересылок", "bool", None),
    "antichannel": ("Запрет писать от каналов", "bool", None),
    "spam_action": ("За ссылки/пересылки", "enum", _pick("delete", "warn", "mute", "kick", "ban")),
    "stopwords": ("Стоп-слова", "list", None),
    "stop_action": ("За стоп-слова", "enum", _pick("delete", "warn", "mute", "kick", "ban")),
    "captcha": ("Капча", "bool", None),
    "captcha_seconds": ("Время на капчу, сек", "int", (30, 900)),
    "welcome": ("Приветствие", "bool", None),
    "welcome_text": ("Текст приветствия", "text", None),
    "rules": ("Правила", "text", None),
    "clean_service": ("Удалять «вошёл/вышел»", "bool", None),
    "helpers": ("Помощник недели", "bool", None),
    "helper_min": ("Минимум ответов для итогов", "int", (1, 100)),
    "achievements": ("Ачивки", "bool", None),
    "ach_announce": ("Поздравлять с редкими публично", "bool", None),
    "ach_topic": ("Тема для поздравлений", "topic", None),
    "cmd_events": ("/события", "bool", None),
    "cmd_rules": ("/rules — правила", "bool", None),
    "cmd_notes": ("Заметки (#название, /notes, /get)", "bool", None),
    "cmd_custom": ("Команды-ответы", "bool", None),
    "cmd_filters": ("Автоответы", "bool", None),
    "mod_plain": ("Команды админов без «/»", "bool", None),
    "spambot_cmd": ("Команда /бот", "bool", None),
    "sky_topic": ("Тема для напоминаний", "topic", None),
    "sky_tz": ("Часовой пояс", "enum", ZONES),
    "sky_before": ("Напоминать за, мин", "int", (1, 60)),
    "sky_geyser": ("🌋 Гейзер", "bool", None),
    "sky_grandma": ("👵 Бабушка", "bool", None),
    "sky_turtle": ("🐢 Черепаха", "bool", None),
    "sky_shard": ("💎 Осколки", "bool", None),
    "sky_reset": ("🔄 Сброс дня", "bool", None),
    "sky_cleanup": ("Удалять старые напоминания", "bool", None),
}

DEFAULTS = {
    "warn_limit": 3,
    "warn_action": "mute",
    "mute_minutes": 1440,
    "log_chat": None,
    "antiflood": False,
    "flood_limit": 6,
    "flood_seconds": 5,
    "flood_action": "mute",
    "antilink": False,
    "link_whitelist": ["youtube.com", "youtu.be"],
    "antiforward": False,
    "antichannel": False,
    "spam_action": "delete",
    "stopwords": [],
    "stop_action": "delete",
    "captcha": True,
    "captcha_seconds": 120,
    "welcome": True,
    "welcome_text": "Привет, {mention}! Добро пожаловать в <b>{chat}</b> 👋",
    "rules": "",
    "clean_service": True,
    "topic_slow": {},  # id темы (строкой, «0» — General) -> секунды между сообщениями одного человека
    "topic_noreply": [],  # id тем, где нельзя отвечать на сообщения других участников
    "autoban": True,
    "helpers": True,
    "helper_min": 3,
    "helper_topics": [],
    "helper_post_topic": None,
    "helper_last_week": "",
    "sky_topic": None,
    "sky_tz": "Europe/Moscow",
    "sky_before": 10,
    "sky_geyser": True,
    "sky_grandma": True,
    "sky_turtle": True,
    "sky_shard": True,
    "sky_reset": False,
    "sky_cleanup": True,
    "autoban_days": 3,
    "spam_level": "normal",
    "cas": True,
    "old_delete": 15,
    "achievements": True,
    "texts": {},          # ключ текста -> свой вариант (см. bot/texts.py)
    "panel_users": {},    # кому владелец дал доступ к панели: id (строкой) -> имя
    "panel_by_rights": True,  # пускать в панель админов с правом «Изменение профиля группы»
    "cmd_events": True,
    "cmd_rules": True,
    "cmd_notes": True,
    "cmd_custom": True,
    "cmd_filters": True,
    "mod_plain": True,
    "spambot_cmd": True,
    "ach_announce": True,
    "ach_topic": None,
    "ach_topics": {},   # id темы -> роль для ачивок: photo | friends | chat
    "ach_off": [],      # выключенные ачивки
    "ach_names": {},    # код ачивки -> своё название
    "ach_custom": {},   # свои ачивки, которые выдают админы: код -> {"t": название, "d": за что}
}

# Короткие пояснения, которые панель показывает над кнопками
HINTS = {
    "warn_limit": "сколько предупреждений можно получить до наказания",
    "warn_action": "что бот делает, когда предупреждения кончились",
    "mute_minutes": "на сколько бот сам мутит/банит (0 — навсегда)",
    "log_chat": "куда присылать отчёт о каждом наказании",
    "antiflood": "наказывать за много сообщений подряд",
    "flood_limit": "сколько сообщений подряд ещё можно",
    "flood_seconds": "за какое время считаются сообщения",
    "flood_action": "наказание за флуд",
    "antilink": "удалять ссылки на сайты",
    "link_whitelist": "сайты, на которые ссылаться можно",
    "antiforward": "удалять пересланные сообщения",
    "antichannel": "удалять сообщения, отправленные от имени канала",
    "autoban": "бот складывает баллы подозрительности: пересылка от бота с кнопками, .apk, «шрифты», "
               "«вбей бота в поиске», заработок с «пиши в лс», 18+ в канале профиля, метки /бот и т. п. "
               "Много баллов у новичка — бан, средне — удалить, мут на сутки и карточка админам «Бот / Человек». "
               "Давних участников бот никогда не банит сам, а при очень высоких баллах только удаляет сообщение",
    "autoban_days": "кто в чате меньше стольких дней, считается новичком (0 — все). Кто был в чате до запуска бота, "
                    "считается давним участником",
    "old_delete": "если у сообщения давнего участника столько баллов или больше, бот удаляет сообщение и присылает "
                  "карточку, но самого участника не наказывает (0 — никогда не удалять)",
    "spam_level": "мягко — банит только совсем очевидных ботов, строго — банит и присылает карточки раньше",
    "cas": "спрашивать общую базу спамеров CAS, не забанен ли новичок в других чатах (бот отправляет туда только ID)",
    "spam_action": "наказание за ссылку или пересылку",
    "stopwords": "слова, которые нельзя писать",
    "stop_action": "наказание за стоп-слово",
    "captcha": "новичок должен нажать кнопку, иначе вылетит",
    "captcha_seconds": "сколько времени дать на кнопку",
    "welcome": "здороваться с новенькими",
    "welcome_text": "что писать новеньким",
    "rules": "ответ на /rules",
    "clean_service": "удалять «X вошёл в чат» и «X вышел»",
    "helpers": "в воскресенье в 20:00 благодарить троих, кто больше всех отвечал на чужие вопросы. Темы, где считать ответы, отмечаются значком 🏅 в «⏱ Темы»; куда писать итоги — командой /helperhere в нужной теме",
    "helper_min": "сколько ответов за неделю нужно, чтобы попасть в итоги",
    "achievements": "участники получают ачивки за жизнь в чате, а смотрят их командой /ачивки (видно только им)",
    "ach_announce": "раз в несколько минут одним сообщением поздравлять тех, кто получил редкую или секретную ачивку, "
                    "а также тех, кому ачивку выдал админ",
    "ach_topic": "куда писать поздравления. Выбирается командой /achhere в нужной теме. Пока тема не выбрана, "
                 "поздравления с редкими ачивками не публикуются",
    "cmd_events": "ближайшие события Sky по команде /события (видно только спросившему)",
    "cmd_rules": "отвечать правилами на /rules",
    "cmd_notes": "показывать заметки по #названию, /get и список по /notes",
    "cmd_custom": "отвечать на команды-ответы из «❓ Команды-ответы»",
    "cmd_filters": "автоответы на слова-триггеры",
    "mod_plain": "понимать «бан», «мут», «варн» и другие команды админов без «/» в начале",
    "spambot_cmd": "команда /бот: бан спам-бота, удаление его сообщений и запоминание меток",
    "sky_topic": "куда бот пишет «через 10 минут гейзер». Выбирается командой /skyhere в нужной теме",
    "sky_tz": "в каком времени показывать события в /события и в напоминаниях",
    "sky_before": "за сколько минут до начала события напоминать",
    "sky_geyser": "напоминать о гейзере (раз в 2 часа)",
    "sky_grandma": "напоминать о бабушке (раз в 2 часа)",
    "sky_turtle": "напоминать о черепахе (раз в 2 часа)",
    "sky_shard": "напоминать об осколках (до 3 раз в день, не каждый день)",
    "sky_reset": "напоминать о сбросе дня",
    "sky_cleanup": "удалять напоминание через 15 минут после начала события, чтобы тема не копила их",
}

SECTIONS = {
    "mod": ("🛡 Модерация", ["warn_limit", "warn_action", "mute_minutes", "log_chat"]),
    "spam": ("🚫 Антиспам", ["autoban", "autoban_days", "spam_level", "cas", "old_delete", "antiflood", "flood_limit", "flood_seconds", "flood_action", "antilink",
                            "link_whitelist", "antiforward", "antichannel", "spam_action", "stopwords",
                            "stop_action"]),
    "new": ("👋 Новички", ["captcha", "captcha_seconds", "welcome", "welcome_text", "rules", "clean_service"]),
    "help": ("🏅 Помощник недели", ["helpers", "helper_min"]),
    "cmds": ("🧩 Команды", ["cmd_events", "cmd_rules", "cmd_notes", "cmd_custom", "cmd_filters", "mod_plain",
                           "spambot_cmd"]),
    "ach": ("🏆 Ачивки", ["achievements", "ach_announce", "ach_topic"]),
    "sky": ("🌌 Sky", ["sky_topic", "sky_tz", "sky_before", "sky_geyser", "sky_grandma", "sky_turtle",
                      "sky_shard", "sky_reset", "sky_cleanup"]),
}
SECTION_OF = {k: sec for sec, (_, keys) in SECTIONS.items() for k in keys}

_cache = {}
_titled = set()


async def get(chat_id: int) -> dict:
    s = _cache.get(chat_id)
    if s is None:
        row = await db.one("SELECT settings FROM chats WHERE chat_id=?", chat_id)
        if row is None:
            await db.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES(?)", chat_id)
        stored = json.loads(row["settings"]) if row else {}
        s = {**DEFAULTS, **stored}
        for key, (_, typ, opt) in FIELDS.items():  # значение из старой версии бота, которого больше нет
            if typ == "enum" and s[key] not in opt or typ == "bool" and not isinstance(s[key], bool):
                s[key] = DEFAULTS[key]
        _cache[chat_id] = s
    return s


async def put(chat_id: int, key: str, value):
    s = await get(chat_id)
    s[key] = value
    # храним только отличия от умолчаний, чтобы новые умолчания подхватывались сами
    diff = {k: v for k, v in s.items() if DEFAULTS.get(k) != v}
    await db.execute("UPDATE chats SET settings=? WHERE chat_id=?", json.dumps(diff, ensure_ascii=False), chat_id)


async def register(chat_id: int, title: str, active: bool = True):
    await db.execute(
        "INSERT INTO chats(chat_id, title, active) VALUES(?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, active=excluded.active",
        chat_id, title or "", int(active),
    )


async def touch(chat):
    """Запоминает название чата один раз за запуск."""
    if chat.id not in _titled:
        _titled.add(chat.id)
        await register(chat.id, chat.title)


async def migrate(old: int, new: int):
    await db.migrate(old, new)
    _cache.pop(old, None)
    _titled.discard(old)


async def set_topic_timer(chat_id: int, key: str, seconds: int):
    slow = dict((await get(chat_id))["topic_slow"])  # копия: умолчания общие для всех чатов
    if seconds:
        slow[key] = seconds
    else:
        slow.pop(key, None)
    await put(chat_id, "topic_slow", slow)


async def toggle_noreply(chat_id: int, key: str) -> bool:
    """Включает/выключает запрет ответов в теме. Возвращает новое состояние."""
    topics = list((await get(chat_id))["topic_noreply"])
    on = key not in topics
    topics = topics + [key] if on else [t for t in topics if t != key]
    await put(chat_id, "topic_noreply", topics)
    return on


async def toggle_list(chat_id: int, key: str, item: str) -> bool:
    """Добавляет элемент в список-настройку или убирает его. Возвращает, есть ли он там теперь."""
    items = list((await get(chat_id))[key])
    on = item not in items
    await put(chat_id, key, items + [item] if on else [x for x in items if x != item])
    return on
