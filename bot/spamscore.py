"""Баллы подозрительности сообщения: много слабых признаков вместо одной метки.

Ни один признак сам по себе никого не банит — решает сумма (см. handlers/guard.py). Признаки подобраны
по настоящему спаму из чата: пересылки от ботов с кнопками, .apk, «шрифты» из чужих алфавитов,
«мне помог такой-то vpnbot, вбей в поиске», заработок с «пиши в лс», 18+ в канале профиля.
"""
import math
import re
import unicodedata
from collections import Counter

from aiogram.types import Message

from . import antibot
from .db import db
from .spam_seed import HAM, SPAM

# --- словари (ищем и в исходном тексте, и в «расшифрованном»: латиница → кириллица, z → з) ---
MONEY = re.compile(r"\$\s?\d|\d\s?(?:₽|руб|р\b|k\b|к\b|тыс|000\b)|заработ(?:ок|ка|ку|ком|ать|ай|аю|аешь|ной)|зарабатыв|доход|прибыл|вложени|оплата|выплат|"
                   r"шабашк|подработ|ваканси|за смену|/рейс")
GAMBLING = re.compile(r"ставк|казино|букмекер|\bбк\b|рынки прогнозов|ставишь|делаешь ставки")
DRUGS = re.compile(r"кладмен|закладк|перевозчик|перевозч|\bхимик|мефедрон|\bмеф\b|шишки|граффитчик|листовщик")
ADULT_TEXT = re.compile(r"порно|porn|onlyfans|интим|🔞|18\+|nude|секс\b|sex\b")
ADULT_PROFILE = re.compile(r"порно|porn|onlyfans|интим|🔞|18\+|nude|секс|sex|xxx|эфир|приват|сними|раздева|горяч|"
                           r"эроти|досуг|слив|вебкам|webcam|фото без|без цензур")
DOCS = re.compile(r"гибдд|права рф|осаго|каско|без экзаменов|поддельн|подлинные документ|справк[аи] (?:без|любые)")
DM = re.compile(r"\bв лс\b|\bв личк|\bлс\b|пиши(?:те)?\b|напиши(?:те)?\b|\bdm\b")
BOT_NAME = re.compile(r"(?<![@\w])[a-z0-9_]{2,}bot\b")
SEARCH = re.compile(r"в поиске|в поиск|поиске тг|поиск тг|найди в тг|в телеге найди")
BAD_FILES = (".apk", ".apks", ".xapk", ".exe", ".msi", ".bat", ".scr", ".jar", ".cmd", ".vbs")
REF = re.compile(r"[?&](?:ref|start|invite|r|aff|partner)=|/ref/|/r/", re.I)

# буквы, которыми пишут люди в русском чате; всё остальное «буквенное» — маскировка
NORMAL = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґўabcdefghijklmnopqrstuvwxyz")
_DEOB = str.maketrans("aeopcxykmthbz", "аеорсхукмтнвз")


def _text_parts(m: Message):
    parts = [m.text or m.caption or ""]
    if m.quote and m.quote.text:
        parts.append(m.quote.text)
    if m.reply_markup and m.reply_markup.inline_keyboard:
        parts += [b.text for row in m.reply_markup.inline_keyboard for b in row]
    return parts


def _buttons(m: Message) -> int:
    if not (m.reply_markup and m.reply_markup.inline_keyboard):
        return 0
    return sum(1 for row in m.reply_markup.inline_keyboard for b in row if b.url)


def _files(m: Message):
    for doc in (m.document, m.external_reply and m.external_reply.document):
        if doc and doc.file_name:
            yield doc.file_name.lower()


def views(text: str) -> str:
    """Текст для поиска по словарям: как есть + «расшифровка» подменённых букв."""
    low = unicodedata.normalize("NFKC", text).lower()
    return low + "\n" + low.translate(_DEOB)


def disguise(text: str):
    """(слов с латиницей и кириллицей вперемешку, «чужих» букв, всего букв)."""
    letters = [c for c in text.lower() if c.isalpha()]
    odd = sum(1 for c in letters if c not in NORMAL)
    mixed = sum(1 for w in re.findall(r"\w+", text.lower())
                if re.search(r"[a-z]", w) and re.search(r"[а-яё]", w))
    return mixed, odd, len(letters)


def content(m: Message, whitelist=()) -> list[tuple[int, str]]:
    """Признаки самого сообщения: [(баллы, причина)]."""
    out = []
    raw = "\n".join(p for p in _text_parts(m) if p)
    hay = views(raw)

    origin = m.forward_origin
    if origin is not None and origin.type == "user" and origin.sender_user.is_bot:
        out.append((4, "переслано от бота"))
    buttons = _buttons(m)
    if buttons:
        out.append((3, "кнопки-ссылки под сообщением"))
    if any(name.endswith(BAD_FILES) for name in _files(m)):
        out.append((7, "файл-программа (.apk/.exe)"))
    if m.external_reply is not None:
        out.append((2, "ответ на сообщение из другого чата"))

    mixed, odd, letters = disguise(raw)
    if odd >= 4 and odd >= 0.08 * letters:
        out.append((6, "буквы из чужих алфавитов («шрифты»)"))
    if mixed:
        out.append(({1: 1, 2: 2, 3: 3}.get(mixed, 5), "латиница внутри русских слов"))

    link_pts = 0
    for url in antibot._links(m):
        v = antibot._link(url)
        if not v or v in antibot.SAFE_HOSTS or any(v == d or v.endswith("." + d) for d in whitelist):
            continue
        link_pts += 3 if REF.search(url) else 1
    if link_pts:
        out.append((min(link_pts, 4), "реферальная ссылка" if link_pts >= 3 else "ссылка"))
    hidden = [e for e in m.entities or m.caption_entities or []
              if e.type == "text_link" and "." not in e.extract_from(m.text or m.caption or "")]
    if hidden:
        out.append((1, "ссылка спрятана под словом"))

    mentions = [e for e in m.entities or m.caption_entities or [] if e.type == "mention"]
    if mentions and DM.search(hay):
        out.append((2, "«пиши в лс» и @юзернейм"))
    if BOT_NAME.search(hay):
        out.append((4, "советует бота «через поиск»") if SEARCH.search(hay) else (2, "название бота без @"))

    money = len(set(MONEY.findall(hay)))
    if money:
        out.append((3 if money >= 2 else 2, "деньги и заработок"))
    for rx, pts, why in ((GAMBLING, 2, "ставки"), (DRUGS, 4, "«работа» курьером/кладменом"),
                         (ADULT_TEXT, 3, "18+"), (DOCS, 4, "документы и права")):
        if rx.search(hay):
            out.append((pts, why))
    return out


def profile(bio: str, channel_title: str, channel_username: str) -> list[tuple[int, str]]:
    hay = views(" ".join(x for x in (bio, channel_title, channel_username) if x))
    out = []
    if ADULT_PROFILE.search(hay):
        out.append((6, "18+ в профиле или его канале"))
    elif MONEY.search(hay) or DRUGS.search(hay) or GAMBLING.search(hay):
        out.append((4, "заработок в профиле или его канале"))
    return out


# --- обучаемый словарь: слова и пары слов из спама (/бот, кнопка «Бот») и из обычных сообщений ---
def tokens(text: str) -> set:
    norm = antibot.normalize(unicodedata.normalize("NFKC", text))
    words = []
    for w in norm.split():
        if w.isdigit():
            w = "<число>"
        elif w.endswith("bot") and len(w) > 4:
            w = "<бот>"
        if len(w) >= 2:
            words.append(w)
    return set(words) | {f"{a} {b}" for a, b in zip(words, words[1:])}


DOCS_KEY = "#docs"
MIN_HAM = 50       # пока обычных примеров мало, словарь не голосует
_words = None      # токен -> [спам, норма] (база + встроенные примеры)
_dirty = Counter()  # что ещё не записано в базу: (токен, 0|1) -> +n


async def _load():
    global _words
    _words = {}
    for r in await db.all("SELECT token, spam, ham FROM words"):
        _words[r["token"]] = [r["spam"], r["ham"]]
    for kind, samples in ((0, SPAM), (1, HAM)):
        for text in samples:
            _add(text, kind, persist=False)


def _add(text: str, kind: int, persist=True):
    for t in tokens(text) | {DOCS_KEY}:
        _words.setdefault(t, [0, 0])[kind] += 1
        if persist:
            _dirty[(t, kind)] += 1


async def learn(text: str, spam: bool):
    if _words is None:
        await _load()
    if text and len(text.strip()) >= 10:
        _add(text, 0 if spam else 1)


async def flush():
    if not _dirty:
        return
    items = list(_dirty.items())
    _dirty.clear()
    for (t, kind), n in items:
        col = "spam" if kind == 0 else "ham"
        await db.conn.execute(f"INSERT INTO words(token, spam, ham) VALUES(?, ?, ?) "
                              f"ON CONFLICT(token) DO UPDATE SET {col}={col}+?",
                              (t, n if kind == 0 else 0, n if kind == 1 else 0, n))
    await db.conn.commit()


async def probability(text: str):
    """Вероятность спама по словарю (0..1) или None, если данных мало."""
    if _words is None:
        await _load()
    S, H = _words.get(DOCS_KEY, [0, 0])
    if not S or H < MIN_HAM:
        return None
    probs = []
    for t in tokens(text):
        s, h = _words.get(t, (0, 0))
        n = s + h
        if n < 2:
            continue
        ps, ph = s / S, h / H
        p = ps / (ps + ph)
        f = (0.5 * 3 + n * p) / (3 + n)  # редкие слова тянем к «непонятно»
        if abs(f - 0.5) > 0.1:
            probs.append(f)
    if not probs:
        return None
    top = sorted(probs, key=lambda f: -abs(f - 0.5))[:15]
    log_s = sum(math.log(f) for f in top)
    log_h = sum(math.log(1 - f) for f in top)
    return 1 / (1 + math.exp(max(-50, min(50, log_h - log_s))))


async def vocabulary(text: str) -> list[tuple[int, str]]:
    p = await probability(text)
    if p is None or p < 0.7:
        return []
    return [(3 if p >= 0.97 else 2 if p >= 0.85 else 1, f"похоже на спам из словаря ({p:.0%})")]


def text_of(m: Message) -> str:
    return "\n".join(p for p in _text_parts(m) if p)


