"""Точка входа для хостингов, которые запускают main.py (то же самое, что python -m bot)."""
import asyncio

from bot.__main__ import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
