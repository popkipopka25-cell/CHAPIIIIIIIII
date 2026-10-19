"""База данных SQLite: автоматическое создание таблиц и помощники."""
import asyncio
import aiosqlite
import json
import time
from contextlib import asynccontextmanager
import config

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    first_seen REAL,
    xp INTEGER DEFAULT 0,
    level INTEGER DEFAULT 1,
    messages_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS members (
    chat_id INTEGER,
    user_id INTEGER,
    role TEXT DEFAULT 'user',
    balance INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS settings (
    chat_id INTEGER PRIMARY KEY,
    data TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS punishments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_id INTEGER,
    type TEXT,
    reason TEXT,
    by_id INTEGER,
    created_at REAL,
    until REAL,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS notes (
    chat_id INTEGER,
    name TEXT,
    text TEXT,
    PRIMARY KEY (chat_id, name)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    from_id INTEGER,
    to_id INTEGER,
    amount INTEGER,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS daily (
    chat_id INTEGER,
    user_id INTEGER,
    last_daily REAL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS marriages (
    chat_id INTEGER,
    user1 INTEGER,
    user2 INTEGER,
    since REAL,
    PRIMARY KEY (chat_id, user1)
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    from_id INTEGER,
    to_id INTEGER,
    created_at REAL,
    status TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS clans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    name TEXT,
    description TEXT DEFAULT '',
    owner_id INTEGER,
    created_at REAL,
    UNIQUE (chat_id, name)
);

CREATE TABLE IF NOT EXISTS clan_members (
    clan_id INTEGER,
    user_id INTEGER,
    role TEXT DEFAULT 'member',
    PRIMARY KEY (clan_id, user_id)
);

CREATE TABLE IF NOT EXISTS clan_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clan_id INTEGER,
    user_id INTEGER,
    status TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS clan_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clan_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    inviter_id INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at REAL
);

CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_id INTEGER,
    reason TEXT,
    message_text TEXT,
    status TEXT DEFAULT 'pending',
    decided_by INTEGER,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS mod_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    actor_id INTEGER,
    action TEXT,
    target_id INTEGER,
    details TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS shop_items (
    chat_id INTEGER,
    name TEXT,
    price INTEGER,
    description TEXT DEFAULT '',
    PRIMARY KEY (chat_id, name)
);

CREATE TABLE IF NOT EXISTS inventory (
    chat_id INTEGER,
    user_id INTEGER,
    item TEXT,
    qty INTEGER DEFAULT 1,
    PRIMARY KEY (chat_id, user_id, item)
);
"""

DEFAULT_SETTINGS = {
    "antispam": True,
    "greetings": True,
    "notes": True,
    "autoreplies": True,
    "count_messages": True,
    "games": True,
    "economy": True,
    "marriages": True,
    "clans": True,
    "welcome_text": "Добро пожаловать, {name}!",
    "antispam_action": "warn",  # warn | delete | mute | case
    "antispam_max_messages": 5,      # сообщений за окно
    "antispam_window_sec": 10,       # окно в секундах
    "antispam_max_duplicates": 3,    # одинаковых сообщений подряд
    "antispam_max_links": 3,         # ссылок за окно
    "mute_duration_sec": 3600,       # мут от антиспама
    "exempt_roles": ["owner", "admin", "moderator"],
}

_db: aiosqlite.Connection | None = None

# Блокировка сериализует транзакции приложения; каждая из них использует
# отдельное SQLite-соединение, чтобы обычные commit() не завершали её досрочно.
tx_lock = asyncio.Lock()


_init_lock = asyncio.Lock()


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is not None:
        return _db
    async with _init_lock:  # одно соединение даже при одновременных первых запросах
        if _db is None:
            conn = await aiosqlite.connect(config.DB_PATH, isolation_level=None)
            try:
                conn.row_factory = aiosqlite.Row
                await conn.executescript(SCHEMA)
                from migrations import run_migrations
                await run_migrations(conn)
            except BaseException:
                await conn.close()
                raise
            _db = conn
    return _db


async def get_transaction_db() -> aiosqlite.Connection:
    """Открывает отдельное SQLite-соединение для явной транзакции.

    Денежные транзакции не должны делить соединение с обычными обработчиками:
    commit() из другого обработчика мог бы преждевременно завершить BEGIN IMMEDIATE.
    """
    await get_db()  # гарантируем создание схемы и выполнение миграций
    conn = await aiosqlite.connect(config.DB_PATH, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    return conn


@asynccontextmanager
async def transaction():
    """Атомарная запись через отдельное соединение с SQLite."""
    async with tx_lock:
        conn = await get_transaction_db()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            yield conn
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            await conn.close()


async def close_db():
    global _db, tx_lock, _init_lock
    # новые блокировки: следующий запуск может идти в другом цикле событий
    tx_lock = asyncio.Lock()
    _init_lock = asyncio.Lock()
    if _db is not None:
        await _db.close()
        _db = None


async def init_db():
    await get_db()


# ---------- пользователи ----------

async def upsert_user(user_id: int, username: str | None, first_name: str | None):
    db = await get_db()
    await db.execute(
        """INSERT INTO users (user_id, username, first_name, first_seen)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
        (user_id, username, first_name, time.time()),
    )
    await db.execute(
        "INSERT OR IGNORE INTO members (chat_id, user_id) VALUES (?, ?)",
        (0, user_id),  # глобальная запись; групповые создаются при необходимости
    )
    await db.commit()


async def find_user_by_username(username: str) -> int | None:
    db = await get_db()
    cur = await db.execute(
        "SELECT user_id FROM users WHERE lower(username) = lower(?)", (username.lstrip("@"),)
    )
    row = await cur.fetchone()
    return row["user_id"] if row else None


async def get_user(user_id: int):
    db = await get_db()
    cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return await cur.fetchone()


async def ensure_member(chat_id: int, user_id: int):
    """Создаёт запись участника со стартовым балансом, если её ещё нет."""
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO members (chat_id, user_id, balance) VALUES (?, ?, ?)",
        (chat_id, user_id, config.START_BALANCE),
    )
    await db.commit()


# ---------- роли ----------

async def get_role(chat_id: int, user_id: int) -> str:
    from config import OWNER_TELEGRAM_ID
    if user_id == OWNER_TELEGRAM_ID:
        return "owner"
    db = await get_db()
    cur = await db.execute(
        "SELECT role FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
    )
    row = await cur.fetchone()
    return row["role"] if row else "user"


async def set_role(chat_id: int, user_id: int, role: str):
    await ensure_member(chat_id, user_id)
    db = await get_db()
    await db.execute(
        "UPDATE members SET role = ? WHERE chat_id = ? AND user_id = ?", (role, chat_id, user_id)
    )
    await db.commit()


# ---------- настройки ----------

async def premium_expiry(chat_id: int, user_id: int, now: float | None = None) -> float | None:
    current = time.time() if now is None else float(now)
    conn = await get_db()
    cur = await conn.execute(
        "SELECT expires_at FROM premium_memberships WHERE chat_id=? AND user_id=? AND expires_at>?",
        (chat_id, user_id, current),
    )
    row = await cur.fetchone()
    return float(row["expires_at"]) if row else None


async def grant_premium(chat_id: int, user_id: int, seconds: int, actor_id: int) -> float:
    """Выдаёт или продлевает премиум; срок добавляется к текущему, если он активен."""
    now = time.time()
    async with transaction() as conn:
        cur = await conn.execute(
            "SELECT expires_at FROM premium_memberships WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
        row = await cur.fetchone()
        start = max(now, float(row["expires_at"])) if row and float(row["expires_at"]) > now else now
        expires = start + seconds
        await conn.execute(
            "INSERT INTO premium_memberships(chat_id,user_id,expires_at,granted_by,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id,user_id) DO UPDATE SET expires_at=excluded.expires_at, granted_by=excluded.granted_by, created_at=excluded.created_at",
            (chat_id, user_id, expires, actor_id, now),
        )
    return expires


async def premium_profile_status(chat_id: int, user_id: int) -> str | None:
    current = time.time()
    conn = await get_db()
    cur = await conn.execute(
        "SELECT status_text FROM premium_memberships WHERE chat_id=? AND user_id=? AND expires_at>?",
        (chat_id, user_id, current),
    )
    row = await cur.fetchone()
    return str(row["status_text"] or "") if row else None


async def set_premium_profile_status(chat_id: int, user_id: int, status_text: str) -> bool:
    conn = await get_db()
    cur = await conn.execute(
        "UPDATE premium_memberships SET status_text=? WHERE chat_id=? AND user_id=? AND expires_at>?",
        (status_text, chat_id, user_id, time.time()),
    )
    await conn.commit()
    return cur.rowcount > 0


async def remove_premium(chat_id: int, user_id: int) -> bool:
    conn = await get_db()
    cur = await conn.execute("DELETE FROM premium_memberships WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    await conn.commit()
    return cur.rowcount > 0


async def get_settings(chat_id: int) -> dict:
    """Возвращает настройки чата и регистрирует его при первом обращении.

    Автосохранение нужно, чтобы группы появлялись в панели владельца даже
    до ручного изменения настроек. Повреждённый JSON не останавливает бота.
    """
    db = await get_db()
    cur = await db.execute("SELECT data FROM settings WHERE chat_id = ?", (chat_id,))
    row = await cur.fetchone()
    if row is None:
        data = {}
        await db.execute(
            "INSERT OR IGNORE INTO settings (chat_id, data) VALUES (?, ?)",
            (chat_id, json.dumps(DEFAULT_SETTINGS, ensure_ascii=False)),
        )
        await db.commit()
    else:
        try:
            data = json.loads(row["data"] or "{}")
            if not isinstance(data, dict):
                data = {}
        except (TypeError, json.JSONDecodeError):
            data = {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    return merged


async def set_setting(chat_id: int, key: str, value):
    settings = await get_settings(chat_id)
    settings[key] = value
    db = await get_db()
    await db.execute(
        "INSERT INTO settings (chat_id, data) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET data=excluded.data",
        (chat_id, json.dumps(settings, ensure_ascii=False)),
    )
    await db.commit()


# ---------- журнал ----------

async def log_action(chat_id: int, actor_id: int, action: str, target_id: int | None, details: str = ""):
    db = await get_db()
    await db.execute(
        "INSERT INTO mod_log (chat_id, actor_id, action, target_id, details, created_at) VALUES (?,?,?,?,?,?)",
        (chat_id, actor_id, action, target_id, details, time.time()),
    )
    await db.commit()


async def get_log(chat_id: int, limit: int = 20):
    db = await get_db()
    safe_limit = max(1, min(100, int(limit)))
    cur = await db.execute(
        "SELECT * FROM mod_log WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, safe_limit)
    )
    return await cur.fetchall()


async def expire_punishments(now: float | None = None) -> int:
    """Помечает истёкшие временные наказания неактивными; возвращает число записей."""
    db = await get_db()
    current = time.time() if now is None else float(now)
    cur = await db.execute(
        "UPDATE punishments SET active=0 WHERE active=1 AND until IS NOT NULL AND until<=?",
        (current,),
    )
    return max(0, int(cur.rowcount or 0))


# ---------- счётчик сообщений и опыт ----------

async def bump_task_progress(chat_id: int, user_id: int, task_key: str, amount: int = 1):
    """Увеличивает прогресс ежедневного задания в UTC-дате."""
    from datetime import datetime, timezone
    if task_key not in {"messages", "transfer", "purchase"} or amount <= 0:
        return
    task_date = datetime.now(timezone.utc).date().isoformat()
    conn = await get_db()
    await conn.execute(
        "INSERT INTO task_progress(chat_id,user_id,task_date,task_key,progress,claimed) VALUES(?,?,?,?,?,0) "
        "ON CONFLICT(chat_id,user_id,task_date,task_key) DO UPDATE SET progress=MIN(progress+excluded.progress,1000000)",
        (chat_id, user_id, task_date, task_key, amount),
    )
    await conn.commit()


async def bump_message_stats(chat_id: int, user_id: int, xp_per_message: int) -> int | None:
    """Обновляет статистику и возвращает новый уровень, если он вырос."""
    conn = await get_db()
    cur = await conn.execute("SELECT level FROM users WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    old_level = int(row["level"]) if row else 1
    await conn.execute(
        "UPDATE users SET messages_count = messages_count + 1, xp = xp + ? WHERE user_id = ?",
        (xp_per_message, user_id),
    )
    # простая формула уровня: level = 1 + xp // 100
    await conn.execute(
        "UPDATE users SET level = 1 + xp / 100 WHERE user_id = ?", (user_id,)
    )
    cur = await conn.execute("SELECT level FROM users WHERE user_id=?", (user_id,))
    new_row = await cur.fetchone()
    new_level = int(new_row["level"]) if new_row else old_level
    await conn.commit()
    await bump_task_progress(chat_id, user_id, "messages")
    return new_level if new_level > old_level else None
