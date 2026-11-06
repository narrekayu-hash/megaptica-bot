import json
import logging
import os
import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    settings TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS warns(
    chat_id INTEGER, user_id INTEGER, count INTEGER NOT NULL,
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT);
CREATE INDEX IF NOT EXISTS users_username ON users(username);
CREATE TABLE IF NOT EXISTS content(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER, kind TEXT, name TEXT, data TEXT,
    UNIQUE(chat_id, kind, name)
);
CREATE TABLE IF NOT EXISTS captcha(
    chat_id INTEGER, user_id INTEGER, message_id INTEGER, deadline INTEGER,
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS members(
    chat_id INTEGER, user_id INTEGER, joined INTEGER,
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS help(
    chat_id INTEGER, user_id INTEGER, name TEXT, question_id INTEGER, week TEXT,
    PRIMARY KEY(chat_id, user_id, question_id)
);
CREATE TABLE IF NOT EXISTS markers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, value TEXT, created INTEGER,
    UNIQUE(kind, value)
);
CREATE TABLE IF NOT EXISTS ach(
    chat_id INTEGER, user_id INTEGER, code TEXT, name TEXT, got INTEGER, announced INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(chat_id, user_id, code)
);
CREATE TABLE IF NOT EXISTS seen(
    chat_id INTEGER, user_id INTEGER, kind TEXT, ref TEXT, ts INTEGER,
    PRIMARY KEY(chat_id, user_id, kind, ref)
);
CREATE INDEX IF NOT EXISTS seen_ref ON seen(chat_id, kind, ref);
CREATE TABLE IF NOT EXISTS words(
    token TEXT PRIMARY KEY, spam INTEGER NOT NULL DEFAULT 0, ham INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reposts(
    channel_id INTEGER, post_id INTEGER, chat_id INTEGER, thread_id INTEGER, message_id INTEGER,
    tag TEXT, album TEXT, media TEXT,
    PRIMARY KEY(channel_id, post_id, chat_id, thread_id)
);
CREATE INDEX IF NOT EXISTS reposts_album ON reposts(channel_id, album);
CREATE TABLE IF NOT EXISTS channel_albums(
    channel_id INTEGER, album TEXT, post_id INTEGER, caption INTEGER, tags TEXT, media TEXT,
    PRIMARY KEY(channel_id, post_id)
);
CREATE INDEX IF NOT EXISTS channel_albums_album ON channel_albums(channel_id, album);
CREATE TABLE IF NOT EXISTS topics(
    chat_id INTEGER, thread_id INTEGER, name TEXT,
    PRIMARY KEY(chat_id, thread_id)
);
"""


class Database:
    def __init__(self):
        self.conn = None
        self._filters = {}  # chat_id -> строки фильтров, чтобы не ходить в базу на каждое сообщение

    async def connect(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = await aiosqlite.connect(path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def backup(self, folder: str, keep: int = 7):
        """Копия базы на сегодня; хранятся последние keep копий."""
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"bot-{time.strftime('%Y-%m-%d')}.db")
        async with aiosqlite.connect(path) as target:
            await self.conn.backup(target)
        copies = sorted(f for f in os.listdir(folder) if f.startswith("bot-") and f.endswith(".db"))
        for old in copies[:-keep]:
            os.remove(os.path.join(folder, old))
        logging.getLogger(__name__).info("Резервная копия базы: %s", path)

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def execute(self, sql, *args):
        await self.conn.execute(sql, args)
        await self.conn.commit()

    async def one(self, sql, *args):
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def all(self, sql, *args):
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchall()

    # --- предупреждения ---
    async def add_warn(self, chat_id, user_id, delta) -> int:
        n = max(0, await self.get_warns(chat_id, user_id) + delta)
        await self.execute("INSERT OR REPLACE INTO warns VALUES(?, ?, ?)", chat_id, user_id, n)
        return n

    async def get_warns(self, chat_id, user_id) -> int:
        row = await self.one("SELECT count FROM warns WHERE chat_id=? AND user_id=?", chat_id, user_id)
        return row["count"] if row else 0

    async def reset_warns(self, chat_id, user_id):
        await self.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", chat_id, user_id)

    # --- юзернеймы (для команд вида «/ban @username») ---
    async def save_user(self, user_id, username):
        await self.execute(
            "INSERT INTO users VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            user_id, username.lower() if username else None,
        )

    async def find_user(self, username):
        row = await self.one("SELECT user_id FROM users WHERE username=?", username.lower())
        return row["user_id"] if row else None

    # --- фильтры и заметки ---
    async def content_list(self, chat_id, kind):
        if kind == "filter" and chat_id in self._filters:
            return self._filters[chat_id]
        rows = await self.all(
            "SELECT id, name, data FROM content WHERE chat_id=? AND kind=? ORDER BY name", chat_id, kind
        )
        if kind == "filter":
            self._filters[chat_id] = rows
        return rows

    async def content_get(self, chat_id, kind, name):
        return await self.one(
            "SELECT id, name, data FROM content WHERE chat_id=? AND kind=? AND name=?", chat_id, kind, name
        )

    async def content_item(self, item_id):
        return await self.one("SELECT * FROM content WHERE id=?", item_id)

    async def content_set(self, chat_id, kind, name, data: dict):
        await self.execute(
            "INSERT INTO content(chat_id, kind, name, data) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(chat_id, kind, name) DO UPDATE SET data=excluded.data",
            chat_id, kind, name, json.dumps(data, ensure_ascii=False),
        )
        self._filters.pop(chat_id, None)

    async def content_del(self, item_id):
        row = await self.content_item(item_id)
        if row:
            await self.execute("DELETE FROM content WHERE id=?", item_id)
            self._filters.pop(row["chat_id"], None)
        return row

    # --- капча ---
    async def captcha_add(self, chat_id, user_id, message_id, deadline):
        await self.execute("INSERT OR REPLACE INTO captcha VALUES(?, ?, ?, ?)", chat_id, user_id, message_id, deadline)

    async def captcha_pop(self, chat_id, user_id):
        row = await self.one("SELECT * FROM captcha WHERE chat_id=? AND user_id=?", chat_id, user_id)
        if row:
            await self.execute("DELETE FROM captcha WHERE chat_id=? AND user_id=?", chat_id, user_id)
        return row

    async def captcha_expired(self, now):
        return await self.all("SELECT * FROM captcha WHERE deadline<=?", now)

    # --- темы форума (Bot API не умеет отдавать их список, поэтому запоминаем встреченные) ---
    async def save_topic(self, chat_id, thread_id, name):
        await self.execute("INSERT OR REPLACE INTO topics VALUES(?, ?, ?)", chat_id, thread_id, name)

    async def topics(self, chat_id):
        return await self.all("SELECT thread_id, name FROM topics WHERE chat_id=? ORDER BY name", chat_id)

    # --- когда участник зашёл (те, кто был до запуска бота, считаются старичками) ---
    async def set_joined(self, chat_id, user_id, ts):
        await self.execute("INSERT OR REPLACE INTO members VALUES(?, ?, ?)", chat_id, user_id, ts)

    async def joined_at(self, chat_id, user_id):
        row = await self.one("SELECT joined FROM members WHERE chat_id=? AND user_id=?", chat_id, user_id)
        return row["joined"] if row else None

    # --- «Помощник недели»: один ответ на один вопрос = одно очко ---
    async def add_help(self, chat_id, user_id, name, question_id, week) -> bool:
        return await self._insert("INSERT OR IGNORE INTO help VALUES(?, ?, ?, ?, ?)",
                                  chat_id, user_id, name, question_id, week)

    async def help_count(self, chat_id, user_id) -> int:
        row = await self.one("SELECT COUNT(*) AS n FROM help WHERE chat_id=? AND user_id=?", chat_id, user_id)
        return row["n"]

    async def help_top(self, chat_id, week, limit):
        return await self.all("SELECT user_id, MAX(name) AS name, COUNT(*) AS n FROM help WHERE chat_id=? AND week=? "
                              "GROUP BY user_id ORDER BY n DESC LIMIT ?", chat_id, week, limit)

    async def _insert(self, sql, *args) -> bool:
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur.rowcount > 0

    # --- отметки для ачивок: «был в теме», «писал в этот день», «выложил фото» и т. п. ---
    async def seen_add(self, chat_id, user_id, kind, ref, ts) -> bool:
        """Возвращает True, если такой отметки ещё не было."""
        return await self._insert("INSERT OR IGNORE INTO seen VALUES(?, ?, ?, ?, ?)", chat_id, user_id, kind, ref, int(ts))

    async def seen_del(self, chat_id, user_id, kind, ref):
        await self.execute("DELETE FROM seen WHERE chat_id=? AND user_id=? AND kind=? AND ref=?", chat_id, user_id, kind, ref)

    async def seen_count(self, chat_id, user_id, kind, min_ref="", min_ts=0) -> int:
        row = await self.one("SELECT COUNT(*) AS n FROM seen WHERE chat_id=? AND user_id=? AND kind=? AND ref>=? AND ts>=?",
                             chat_id, user_id, kind, min_ref, min_ts)
        return row["n"]

    async def seen_refs(self, chat_id, user_id, kind, min_ref=""):
        rows = await self.all("SELECT ref FROM seen WHERE chat_id=? AND user_id=? AND kind=? AND ref>=?",
                              chat_id, user_id, kind, min_ref)
        return {r["ref"] for r in rows}

    async def seen_ts(self, chat_id, user_id, kind, ref):
        row = await self.one("SELECT ts FROM seen WHERE chat_id=? AND user_id=? AND kind=? AND ref=?", chat_id, user_id, kind, ref)
        return row["ts"] if row else None

    async def seen_owner(self, chat_id, kind, ref):
        """Чья это отметка (например, кто выложил фото с этим message_id)."""
        row = await self.one("SELECT user_id FROM seen WHERE chat_id=? AND kind=? AND ref=?", chat_id, kind, ref)
        return row["user_id"] if row else None

    async def seen_ref_count(self, chat_id, kind, ref) -> int:
        row = await self.one("SELECT COUNT(*) AS n FROM seen WHERE chat_id=? AND kind=? AND ref=?", chat_id, kind, ref)
        return row["n"]

    # --- полученные ачивки ---
    async def ach_add(self, chat_id, user_id, code, name, announced) -> bool:
        return await self._insert("INSERT OR IGNORE INTO ach VALUES(?, ?, ?, ?, strftime('%s','now'), ?)",
                                  chat_id, user_id, code, name, int(announced))

    async def ach_of(self, chat_id, user_id):
        return await self.all("SELECT code, got FROM ach WHERE chat_id=? AND user_id=? ORDER BY got", chat_id, user_id)

    async def ach_del(self, chat_id, user_id, code) -> bool:
        return await self._insert("DELETE FROM ach WHERE chat_id=? AND user_id=? AND code=?", chat_id, user_id, code)

    async def ach_del_code(self, chat_id, code):
        await self.execute("DELETE FROM ach WHERE chat_id=? AND code=?", chat_id, code)

    async def ach_holders(self, chat_id, code) -> int:
        row = await self.one("SELECT COUNT(*) AS n FROM ach WHERE chat_id=? AND code=?", chat_id, code)
        return row["n"]

    async def ach_pending(self):
        return await self.all("SELECT chat_id, user_id, code, name FROM ach WHERE announced=0 ORDER BY got")

    async def ach_mark_announced(self, chat_id):
        await self.execute("UPDATE ach SET announced=1 WHERE chat_id=? AND announced=0", chat_id)

    # --- метки спам-ботов (общие для всех чатов) ---
    async def add_marker(self, kind, value) -> int:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO markers(kind, value, created) VALUES(?, ?, strftime('%s','now'))", (kind, value))
        await self.conn.commit()
        return cur.rowcount

    async def markers(self):
        return await self.all("SELECT id, kind, value FROM markers ORDER BY id DESC")

    async def del_marker(self, marker_id):
        await self.execute("DELETE FROM markers WHERE id=?", marker_id)

    # группа превратилась в супергруппу — у неё новый ID
    async def migrate(self, old, new):
        for table in ("chats", "warns", "content", "captcha", "topics", "members", "help", "ach", "seen", "reposts"):
            await self.conn.execute(f"UPDATE OR REPLACE {table} SET chat_id=? WHERE chat_id=?", (new, old))
        await self.conn.commit()
        self._filters.pop(old, None)


db = Database()
