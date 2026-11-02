import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()
BAD_CONFIG = 3  # код выхода «неправильный .env»: перезапускать бесполезно


@dataclass(frozen=True)
class Config:
    token: str
    owners: frozenset
    db_path: str


def _fail(text: str):
    print(text, file=sys.stderr)
    raise SystemExit(BAD_CONFIG)


def _load() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        _fail("BOT_TOKEN не задан — заполни файл .env (пример в .env.example)")
    try:
        owners = frozenset(int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x)
    except ValueError:
        _fail("OWNER_IDS в .env должен содержать только числа через запятую, например 123456789")
    if not owners:
        _fail("OWNER_IDS не задан — укажи свой Telegram ID в .env")
    return Config(token, owners, os.getenv("DB_PATH", "data/bot.db"))


config = _load()
