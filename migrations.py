
"""Идемпотентные миграции SQLite для Чапи."""
from __future__ import annotations

import aiosqlite


async def _add_column_if_missing(
    conn: aiosqlite.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cur.fetchall()}

    if column not in existing:
        await conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


async def run_migrations(conn: aiosqlite.Connection) -> None:
    """Создаёт таблицы новых функций и безопасно добавляет недостающие поля."""

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

    -- Учётные записи сотрудников веб-панели.
    -- Владелец управляет созданием и отключением аккаунтов.
    CREATE TABLE IF NOT EXISTS panel_accounts (
        user_id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        role TEXT NOT NULL
            CHECK (role IN ('admin', 'agent')),
        active INTEGER NOT NULL DEFAULT 1
            CHECK (active IN (0, 1)),
        created_by INTEGER NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );

    -- Индивидуальные разрешения сотрудников.
    -- scope_key: global или group:<chat_id>.
    CREATE TABLE IF NOT EXISTS panel_permissions (
        user_id INTEGER NOT NULL,
        permission_key TEXT NOT NULL,
        scope_key TEXT NOT NULL DEFAULT 'global',
        granted_by INTEGER NOT NULL,
        granted_at REAL NOT NULL,
        PRIMARY KEY (user_id, permission_key, scope_key),
        FOREIGN KEY (user_id)
            REFERENCES panel_accounts(user_id)
            ON DELETE CASCADE
    );

    -- Уровень доступа сотрудника к отдельной группе.
    CREATE TABLE IF NOT EXISTS panel_group_access (
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        access_level TEXT NOT NULL DEFAULT 'view'
            CHECK (access_level IN ('none', 'view', 'manage')),
        granted_by INTEGER NOT NULL,
        granted_at REAL NOT NULL,
        PRIMARY KEY (user_id, chat_id),
        FOREIGN KEY (user_id)
            REFERENCES panel_accounts(user_id)
            ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_panel_accounts_role
        ON panel_accounts(role, active);

    CREATE INDEX IF NOT EXISTS idx_panel_permissions_user
        ON panel_permissions(user_id, scope_key);

    CREATE INDEX IF NOT EXISTS idx_panel_group_access_chat
        ON panel_group_access(chat_id, user_id);
    """)

    # Поля, которые добавлялись в более поздних версиях проекта.
    for column, definition in (
        ("kind", "TEXT"),
        ("actor_id", "INTEGER"),
        ("note", "TEXT"),
    ):
        await _add_column_if_missing(
            conn, "transactions", column, definition
        )

    for column, definition in (
        ("privacy", "TEXT NOT NULL DEFAULT 'open'"),
        ("coins", "INTEGER NOT NULL DEFAULT 0"),
    ):
        await _add_column_if_missing(
            conn, "clans", column, definition
        )

    # Старые записи транзакций остаются действительными:
    # kind и note могут быть NULL.
    await conn.commit()
