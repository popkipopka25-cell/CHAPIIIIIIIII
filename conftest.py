"""Общие фикстуры тестов: каждая проверка работает с временной базой SQLite."""
import os
import sys

import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import database as db  # noqa: E402


@pytest_asyncio.fixture
async def tmp_db(tmp_path, monkeypatch):
    """Временная база данных; настоящие данные пользователей не затрагиваются."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    await db.close_db()
    db._db = None
    yield db
    await db.close_db()


CHAT = -1001234567890
USER_A = 111
USER_B = 222
