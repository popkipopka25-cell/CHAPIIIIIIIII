"""Идемпотентные миграции SQLite для Чапи."""
from __future__ import annotations

import aiosqlite


async def _add_column_if_missing(
    conn: aiosqlite.Connection, table: str, column: str, definition: str
) -> None:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cur.fetchall()}
    if column not in existing:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def run_migrations(conn: aiosqlite.Connection) -> None:
    """Создаёт таблицы новых функций и добавляет недостающие поля безопасно."""
    await conn.executescript("""
    CREATE TABLE IF NOT EXISTS premium_memberships (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        expires_at REAL NOT NULL,
        granted_by INTEGER,
        created_at REAL,
        status_text TEXT DEFAULT '',
        PRIMARY KEY (chat_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS task_progress (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        task_date TEXT NOT NULL,
        task_key TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0,
        claimed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id, task_date, task_key)
    );

    CREATE TABLE IF NOT EXISTS autoreplies (
        chat_id INTEGER NOT NULL,
        trigger_key TEXT NOT NULL,
        trigger_text TEXT NOT NULL,
        response TEXT NOT NULL,
        created_by INTEGER,
        created_at REAL,
        PRIMARY KEY (chat_id, trigger_key)
    );

    CREATE TABLE IF NOT EXISTS marriage_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user1 INTEGER NOT NULL,
        user2 INTEGER NOT NULL,
        since REAL NOT NULL,
        divorced_at REAL NOT NULL,
        restored_at REAL
    );

    CREATE TABLE IF NOT EXISTS marriage_restore_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        history_id INTEGER NOT NULL,
        from_id INTEGER NOT NULL,
        to_id INTEGER NOT NULL,
        created_at REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
    );

    CREATE TABLE IF NOT EXISTS global_staff (
        user_id INTEGER PRIMARY KEY,
        role TEXT NOT NULL,
        granted_by INTEGER,
        granted_at REAL
    );

    CREATE TABLE IF NOT EXISTS staff_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        target_id INTEGER,
        result TEXT NOT NULL DEFAULT '',
        chat_id INTEGER,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS spamblock (
        user_id INTEGER PRIMARY KEY,
        reason TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual',
        added_by INTEGER,
        chat_id INTEGER,
        message_id INTEGER,
        details TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS global_modules (
        key TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 1,
        changed_by INTEGER,
        changed_at REAL
    );

    CREATE TABLE IF NOT EXISTS clan_bans (
        clan_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        by_id INTEGER,
        created_at REAL,
        PRIMARY KEY (clan_id, user_id)
    );

    CREATE INDEX IF NOT EXISTS idx_punishments_active
        ON punishments(active, until);
    CREATE INDEX IF NOT EXISTS idx_transactions_chat_created
        ON transactions(chat_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_autoreplies_chat
        ON autoreplies(chat_id, trigger_key);
    """)

    # Поля, которые добавлялись в более поздних версиях проекта.
    for column, definition in (
        ("kind", "TEXT"),
        ("actor_id", "INTEGER"),
        ("note", "TEXT"),
    ):
        await _add_column_if_missing(conn, "transactions", column, definition)

    for column, definition in (
        ("privacy", "TEXT NOT NULL DEFAULT 'open'"),
        ("coins", "INTEGER NOT NULL DEFAULT 0"),
    ):
        await _add_column_if_missing(conn, "clans", column, definition)

    # Старые записи транзакций остаются действительными: kind/note могут быть NULL.
    await conn.commit()
