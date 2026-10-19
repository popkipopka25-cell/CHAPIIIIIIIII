"""Этап 5: глобальный штат, глобальный спам-блок и реестр модулей.

Владелец определяется ТОЛЬКО по OWNER_TELEGRAM_ID из окружения.
Все проверки прав выполняются здесь, на сервере, при каждом вызове.
"""
import time

import config
import database as db

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_AGENT = "agent"
ROLE_TITLES = {ROLE_OWNER: "👑 Владелец", ROLE_ADMIN: "🛡 Глобальный админ", ROLE_AGENT: "🕵️ Агент"}

# Разрешения ролей. Владелец имеет все права всегда.
PERMS = {
    ROLE_ADMIN: {"spamblock.view", "spamblock.remove", "spamblock.add", "staff.view", "staff.agents", "log.view"},
    ROLE_AGENT: {"spamblock.view", "spamblock.remove", "spamblock.add"},
}

# Единый реестр модулей: ключ -> (название, синонимы для команд).
MODULES = {
    "economy": ("Экономика", {"экономика", "экономику"}),
    "games": ("Игры", {"игры", "игра"}),
    "marriages": ("Браки", {"браки", "брак"}),
    "clans": ("Кланы", {"кланы", "клан"}),
    "antispam": ("Антиспам", {"антиспам"}),
    "notes": ("Заметки", {"заметки"}),
    "autoreplies": ("Автоответы", {"автоответы"}),
    "cases": ("Дела", {"дела"}),
    "premium": ("Премиум", {"премиум"}),
    "social": ("Соцфункции", {"соц", "социальное", "соцфункции"}),
    "moderation": ("Модерация", {"модерация"}),
}

# Автодобавление: N срабатываний антиспама за окно -> спам-блок (защита от ложных срабатываний).
AUTO_BLOCK_HITS = 3
AUTO_BLOCK_WINDOW = 600
_hits: dict[int, list[float]] = {}


def is_owner(user_id: int | None) -> bool:
    owner = config.OWNER_TELEGRAM_ID
    return bool(user_id and owner > 0 and int(user_id) == owner)


def resolve_module(word: str) -> str | None:
    w = (word or "").strip().casefold()
    for key, (_title, aliases) in MODULES.items():
        if w == key or w in aliases:
            return key
    return None


# ---------- штат ----------

async def get_staff_role(user_id: int) -> str | None:
    if is_owner(user_id):
        return ROLE_OWNER
    conn = await db.get_db()
    cur = await conn.execute("SELECT role FROM global_staff WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    return row["role"] if row else None


async def has_perm(user_id: int, perm: str) -> bool:
    role = await get_staff_role(user_id)
    if role == ROLE_OWNER:
        return True
    return bool(role and perm in PERMS.get(role, set()))


async def staff_log(actor_id: int, action: str, target_id: int | None, result: str, chat_id: int | None = None):
    conn = await db.get_db()
    await conn.execute(
        "INSERT INTO staff_log (actor_id, action, target_id, result, chat_id, created_at) VALUES (?,?,?,?,?,?)",
        (actor_id, action, target_id, result, chat_id, time.time()),
    )
    await conn.commit()


async def assign_role(actor_id: int, target_id: int, role: str) -> tuple[bool, str]:
    if role not in (ROLE_ADMIN, ROLE_AGENT):
        return False, "Неизвестная роль."
    if target_id <= 0:
        return False, "Некорректный Telegram ID."
    if is_owner(target_id):
        return False, "Владельцу нельзя назначить другую роль — он уже имеет полный доступ."
    actor_role = await get_staff_role(actor_id)
    # Админов назначает только владелец; агентов — владелец или админ с правом staff.agents.
    if role == ROLE_ADMIN and actor_role != ROLE_OWNER:
        return False, "Назначать глобальных админов может только владелец."
    if role == ROLE_AGENT and not await has_perm(actor_id, "staff.agents"):
        return False, "Недостаточно прав."
    current = await get_staff_role(target_id)
    if current == role:
        return False, f"Пользователь уже {ROLE_TITLES[role]}."
    if current == ROLE_ADMIN and actor_role != ROLE_OWNER:
        return False, "Изменить роль админа может только владелец."
    conn = await db.get_db()
    await conn.execute(
        "INSERT INTO global_staff (user_id, role, granted_by, granted_at) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, granted_by=excluded.granted_by, granted_at=excluded.granted_at",
        (target_id, role, actor_id, time.time()),
    )
    await conn.commit()
    note = f" (было: {ROLE_TITLES[current]})" if current else ""
    await staff_log(actor_id, f"+{role}", target_id, "ok" + note)
    return True, f"✅ <code>{target_id}</code> назначен: {ROLE_TITLES[role]}{note}."


async def remove_role(actor_id: int, target_id: int, role: str) -> tuple[bool, str]:
    if is_owner(target_id):
        return False, "Владельца нельзя уволить или лишить прав."
    actor_role = await get_staff_role(actor_id)
    if role == ROLE_ADMIN and actor_role != ROLE_OWNER:
        return False, "Снимать глобальных админов может только владелец."
    if role == ROLE_AGENT and not await has_perm(actor_id, "staff.agents"):
        return False, "Недостаточно прав."
    current = await get_staff_role(target_id)
    if current != role:
        return False, f"Пользователь <code>{target_id}</code> не является: {ROLE_TITLES[role]}."
    conn = await db.get_db()
    await conn.execute("DELETE FROM global_staff WHERE user_id=?", (target_id,))
    await conn.commit()
    await staff_log(actor_id, f"-{role}", target_id, "ok")
    return True, f"✅ <code>{target_id}</code> снят с должности: {ROLE_TITLES[role]}."


async def list_staff() -> list[dict]:
    conn = await db.get_db()
    cur = await conn.execute("SELECT user_id, role, granted_by, granted_at FROM global_staff ORDER BY role, granted_at")
    return [dict(r) for r in await cur.fetchall()]


async def get_staff_log(limit: int = 20) -> list[dict]:
    conn = await db.get_db()
    cur = await conn.execute("SELECT * FROM staff_log ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in await cur.fetchall()]


# ---------- спам-блок ----------

async def spamblock_get(user_id: int) -> dict | None:
    conn = await db.get_db()
    cur = await conn.execute("SELECT * FROM spamblock WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def is_spamblocked(user_id: int) -> bool:
    if await get_staff_role(user_id):  # штат и владелец не блокируются
        return False
    return await spamblock_get(user_id) is not None


async def spamblock_add(user_id: int, reason: str, source: str, added_by: int | None,
                        chat_id: int | None = None, message_id: int | None = None,
                        details: str = "") -> tuple[bool, str]:
    if is_owner(user_id) or await get_staff_role(user_id):
        return False, "Сотрудников бота нельзя добавить в спам-блок."
    if await spamblock_get(user_id):
        return False, "Пользователь уже в спам-блоке."
    conn = await db.get_db()
    await conn.execute(
        "INSERT INTO spamblock (user_id, reason, source, added_by, chat_id, message_id, details, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (user_id, reason[:300], source, added_by, chat_id, message_id, details[:500], time.time()),
    )
    await conn.commit()
    await staff_log(added_by or 0, "spamblock_add", user_id, f"{source}: {reason[:100]}", chat_id)
    return True, f"⛔ <code>{user_id}</code> добавлен в глобальный спам-блок."


async def spamblock_remove(actor_id: int, user_id: int) -> tuple[bool, str]:
    if not await has_perm(actor_id, "spamblock.remove"):
        return False, "Недостаточно прав для снятия спам-блока."
    if not await spamblock_get(user_id):
        return False, f"Пользователь <code>{user_id}</code> не находится в спам-блоке."
    conn = await db.get_db()
    await conn.execute("DELETE FROM spamblock WHERE user_id=?", (user_id,))
    await conn.commit()
    _hits.pop(user_id, None)
    await staff_log(actor_id, "spamblock_remove", user_id, "ok")
    return True, f"✅ <code>{user_id}</code> удалён из спам-блока. Доступ к боту восстановлен."


async def register_violation(user_id: int, reason: str, chat_id: int | None, message_id: int | None,
                             now: float | None = None) -> bool:
    """Вызывается антиспамом. Возвращает True, если пользователь автоматически заблокирован."""
    if not await module_enabled("antispam"):
        return False
    now = now or time.time()
    hits = [t for t in _hits.get(user_id, []) if now - t < AUTO_BLOCK_WINDOW]
    hits.append(now)
    _hits[user_id] = hits
    if len(hits) < AUTO_BLOCK_HITS:
        return False
    ok, _ = await spamblock_add(user_id, f"авто: {reason}", "auto", None, chat_id, message_id,
                                f"{len(hits)} нарушений за {AUTO_BLOCK_WINDOW // 60} мин")
    return ok


# ---------- глобальные модули ----------

async def module_enabled(key: str) -> bool:
    conn = await db.get_db()
    cur = await conn.execute("SELECT enabled FROM global_modules WHERE key=?", (key,))
    row = await cur.fetchone()
    return True if row is None else bool(row["enabled"])


async def set_module(actor_id: int, key: str, enabled: bool) -> tuple[bool, str]:
    if not is_owner(actor_id):
        return False, "Управлять модулями может только владелец."
    if key not in MODULES:
        return False, "Неизвестный модуль."
    conn = await db.get_db()
    await conn.execute(
        "INSERT INTO global_modules (key, enabled, changed_by, changed_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at=excluded.changed_at",
        (key, int(enabled), actor_id, time.time()),
    )
    await conn.commit()
    await staff_log(actor_id, "module_on" if enabled else "module_off", None, key)
    return True, f"{'🟢 Включён' if enabled else '🔴 Выключен'} модуль «{MODULES[key][0]}» во всех группах."


async def modules_state() -> dict[str, bool]:
    return {k: await module_enabled(k) for k in MODULES}
