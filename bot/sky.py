"""Расписание событий Sky: Children of the Light.

Регулярные события (гейзер, бабушка, черепаха, сброс дня) — факты об игре: время в игре идёт
по тихоокеанскому времени (America/Los_Angeles), события повторяются по нему.

Расчёт осколков (shard eruptions) перенесён на Python из проекта sky-shards:
https://github.com/PlutoyDev/sky-shards (src/data/shard.ts)

    MIT License

    Copyright (c) 2024 Plutoy

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

SKY_TZ = ZoneInfo("America/Los_Angeles")

# ключ: (название, эмодзи, период в минутах, первое начало после полуночи по времени игры, в минутах)
REGULAR = {
    "geyser": ("Гейзер", "🌋", 120, 5),
    "grandma": ("Бабушка", "👵", 120, 35),
    "turtle": ("Черепаха", "🐢", 120, 50),
    "reset": ("Сброс дня", "🔄", 1440, 0),
}

# Часовые пояса, в которых бот показывает время (значение настройки -> подпись)
ZONES = {
    "Europe/Kaliningrad": "Калининград", "Europe/Moscow": "Москва", "Europe/Samara": "Самара",
    "Asia/Yekaterinburg": "Екатеринбург", "Asia/Omsk": "Омск", "Asia/Novosibirsk": "Новосибирск",
    "Asia/Irkutsk": "Иркутск", "Asia/Vladivostok": "Владивосток", "Europe/Minsk": "Минск",
    "Europe/Kyiv": "Киев", "Asia/Almaty": "Алматы",
}


def next_regular(key: str, now: datetime) -> datetime:
    """Ближайшее начало регулярного события (по часам игры, с учётом перехода на летнее время)."""
    _, _, period, first = REGULAR[key]
    local = now.astimezone(SKY_TZ)
    midnight = datetime.combine(local.date(), time(0), SKY_TZ)
    for day in range(2):
        base = midnight + timedelta(days=day)
        for k in range(1440 // period):
            start = (base + timedelta(minutes=first + k * period)).astimezone(timezone.utc)
            if start > now:
                return start
    raise AssertionError("событие не найдено")


# --- осколки (порт sky-shards) ---
LAND = timedelta(minutes=8, seconds=40)   # приземление после начала
END = timedelta(hours=4)                  # конец после начала
REALMS = ["prairie", "forest", "valley", "wasteland", "vault"]
REALM_NAMES = {"prairie": "Daylight Prairie", "forest": "Hidden Forest", "valley": "Valley of Triumph",
               "wasteland": "Golden Wasteland", "vault": "Vault of Knowledge"}
MAP_NAMES = {
    "prairie.butterfly": "Butterfly Field", "prairie.village": "Village Islands", "prairie.cave": "Cave",
    "prairie.bird": "Bird Nest", "prairie.island": "Sanctuary Island", "forest.brook": "Forest Brook",
    "forest.boneyard": "Boneyard", "forest.end": "Forest Garden", "forest.tree": "Treehouse",
    "forest.sunny": "Elevated Clearing", "valley.rink": "Ice Rink", "valley.dreams": "Village of Dreams",
    "valley.hermit": "Hermit Valley", "wasteland.temple": "Broken Temple", "wasteland.battlefield": "Battlefield",
    "wasteland.graveyard": "Graveyard", "wasteland.crab": "Crabfield", "wasteland.ark": "Forgotten Ark",
    "vault.starlight": "Starlight Desert", "vault.jelly": "Jellyfish Cove",
}
# (дни недели без осколков: 1 — пн … 7 — вс, интервал, смещение от полуночи, карты по королевствам, награда)
SHARD_GROUPS = [
    ({6, 7}, timedelta(hours=8), timedelta(hours=1, minutes=50),
     ["prairie.butterfly", "forest.brook", "valley.rink", "wasteland.temple", "vault.starlight"], None),
    ({7, 1}, timedelta(hours=8), timedelta(hours=2, minutes=10),
     ["prairie.village", "forest.boneyard", "valley.rink", "wasteland.battlefield", "vault.starlight"], None),
    ({1, 2}, timedelta(hours=6), timedelta(hours=7, minutes=40),
     ["prairie.cave", "forest.end", "valley.dreams", "wasteland.graveyard", "vault.jelly"], 2),
    ({2, 3}, timedelta(hours=6), timedelta(hours=2, minutes=20),
     ["prairie.bird", "forest.tree", "valley.dreams", "wasteland.crab", "vault.jelly"], 2.5),
    ({3, 4}, timedelta(hours=6), timedelta(hours=3, minutes=30),
     ["prairie.island", "forest.sunny", "valley.hermit", "wasteland.ark", "vault.jelly"], 3.5),
]
REWARD_OVERRIDE = {"forest.end": 2.5, "valley.dreams": 2.5, "forest.tree": 3.5, "vault.jelly": 3.5}


@dataclass
class Shard:
    day: date
    has_shard: bool
    red: bool
    realm: str
    map: str
    reward: float | None
    occurrences: list  # [(начало, приземление, конец)] в UTC

    @property
    def place(self) -> str:
        return f"{REALM_NAMES[self.realm]} · {MAP_NAMES[self.map]}"


def shard_for_day(day: date) -> Shard:
    dom, wd = day.day, day.isoweekday()
    red = dom % 2 == 1
    realm_idx = (dom - 1) % 5
    group = (((dom - 1) // 2) % 3) + 2 if red else (dom // 2) % 2
    no_days, interval, offset, maps, reward = SHARD_GROUPS[group]
    shard_map = maps[realm_idx]
    midnight = datetime.combine(day, time(0), SKY_TZ)
    first = midnight.astimezone(timezone.utc) + offset  # как в оригинале: абсолютное время от полуночи
    # в воскресенье перехода на летнее/зимнее время оригинал сдвигает начало на час
    if wd == 7 and bool(midnight.dst()) != bool(first.astimezone(SKY_TZ).dst()):
        first += timedelta(hours=-1 if first.astimezone(SKY_TZ).dst() else 1)
    occ = [(s, s + LAND, s + END) for s in (first + interval * i for i in range(3))]
    return Shard(day, wd not in no_days, red, REALMS[realm_idx], shard_map,
                 REWARD_OVERRIDE.get(shard_map, reward) if red else None, occ)


def next_shard(now: datetime) -> Shard:
    """Ближайший день с осколками, у которого ещё не закончилось последнее извержение."""
    day = now.astimezone(SKY_TZ).date()
    for i in range(14):
        info = shard_for_day(day + timedelta(days=i))
        if info.has_shard and now < info.occurrences[-1][2]:
            return info
    raise AssertionError("осколок не найден")


# --- текст для участников ---
def left(delta: timedelta) -> str:
    seconds = delta.total_seconds()
    if seconds < 60:
        return "меньше минуты"
    minutes = int(-(-seconds // 60))  # округляем вверх: при 9:50 до начала пишем «10 мин"
    h, m = divmod(minutes, 60)
    return f"{h} ч {m} мин" if h and m else (f"{h} ч" if h else f"{m} мин")


def clock(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%H:%M")


WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def shard_line(info: Shard, now: datetime, tz: str) -> str:
    color = "🔴 красный" if info.red else "⚫ чёрный"
    head = f"💎 <b>Осколок</b>: {color}, {escape(info.place)}"
    if info.reward:
        head += f", вознесённых свечей: {info.reward:g}"
    for start, land, end in info.occurrences:
        if end <= now:
            continue
        if start <= now:
            return f"{head}\n   идёт сейчас, закончится через {left(end - now)} ({clock(end, tz)})"
        day = start.astimezone(ZoneInfo(tz))
        when = clock(start, tz) if day.date() == now.astimezone(ZoneInfo(tz)).date() else \
            f"{WEEKDAYS[day.weekday()]} {day:%d.%m} {clock(start, tz)}"
        return f"{head}\n   через {left(start - now)} ({when}), приземление в {clock(land, tz)}"
    return head


def overview(now: datetime, tz: str) -> str:
    lines = [f"🌌 <b>События Sky</b> (время: {ZONES.get(tz, tz)})", ""]
    for key, (name, emoji, _, _) in REGULAR.items():
        start = next_regular(key, now)
        lines.append(f"{emoji} <b>{name}</b> — через {left(start - now)} ({clock(start, tz)})")
    lines += ["", shard_line(next_shard(now), now, tz), "",
              "<i>Осколки считаются по правилам sky-shards (PlutoyDev, MIT). Если игра изменит "
              "расписание, время может не совпасть.</i>"]
    return "\n".join(lines)


def upcoming(now: datetime, horizon: timedelta):
    """Все начала событий в ближайшее время: [(ключ, начало, осколок или None)]."""
    out = []
    for key in REGULAR:
        start = next_regular(key, now)
        if start - now <= horizon:
            out.append((key, start, None))
    info = next_shard(now)
    for start, _, _ in info.occurrences:
        if now < start and start - now <= horizon:
            out.append(("shard", start, info))
    return out
