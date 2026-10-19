
"""Штат Чапи: роли, права, журнал, спам-блок и модули."""
from __future__ import annotations

import time

import config
import database as db

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_AGENT = "agent"

ROLE_TITLES = {
    ROLE_OWNER: "👑 Владелец",
    ROLE_ADMIN: "🛡 Глобальный админ",
    ROLE_AGENT: "🕵️ Агент",
}

# Базовые разрешения для сотрудников старой системы global_staff.
PERMS = {
    ROLE_ADMIN: {
        "spamblock.view",
        "spamblock.remove",
        "spamblock.add",
        "staff.view",
        "staff.agents",
        "log.view",
    },
    ROLE_AGENT: {
        "spamblock.view",
        "spamblock.remove",
        "spamblock.add",
    },
}

# Модули бота.
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

# Автоматический спам-блок: число срабатываний за временное окно.
AUTO_BLOCK_HITS = 3
AUTO_BLOCK_WINDOW = 600
_hits: dict[int, list[float]] = {}


def is_owner(user_id: int | None) -> bool:
    """Владелец определяется только настройкой OWNER_TELEGRAM_ID."""
    owner = int(config.OWNER_TELEGRAM_ID)
    return bool(user_id and owner > 0 and int(user_id) == owner)


def resolve_module(word: str) -> str | None:
    value = (word or "").strip().casefold()
    for key, (_title, aliases) in MODULES.items():
        if value == key or value in aliases:
            return key
    return None


async def _panel_account(user_id: int):
    """Возвращает активный аккаунт панели, если он существует."""
    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT user_id, role, active
        FROM panel_accounts
        WHERE user_id = ?
        LIMIT 1
        """,
        (int(user_id),),
    )
    return await cur.fetchone()


# ---------- роли и разрешения ----------

async def get_staff_role(user_id: int) -> str | None:
    if is_owner(user_id):
        return ROLE_OWNER

    conn = await db.get_db()

    # Аккаунт веб-панели имеет приоритет: отключённый аккаунт
    # не должен получать права через старую таблицу global_staff.
    account = await _panel_account(user_id)
    if account is not None:
        if not account["active"]:
            return None
        return account["role"]

    cur = await conn.execute(
        "SELECT role FROM global_staff WHERE user_id = ?",
        (int(user_id),),
    )
    row = await cur.fetchone()
    return row["role"] if row else None


async def has_perm(
    user_id: int,
    perm: str,
    scope: str = "global",
) -> bool:
    """Проверяет право. Для аккаунтов панели действуют личные разрешения."""
    if is_owner(user_id):
        return True

    role = await get_staff_role(user_id)
    if role is None:
        return False

    conn = await db.get_db()
    account = await _panel_account(user_id)

    if account is not None:
        if not account["active"]:
            return False

        # Для аккаунтов панели разрешения задаются индивидуально.
        # Проверяем сначала точную область, затем глобальное право.
        scopes = [scope]
        if scope != "global":
            scopes.append("global")

        for permission_scope in scopes:
            cur = await conn.execute(
                """
                SELECT 1
                FROM panel_permissions
                WHERE user_id = ?
                  AND permission_key = ?
                  AND scope_key = ?
                LIMIT 1
                """,
                (int(user_id), perm, permission_scope),
            )
            if await cur.fetchone():
                return True

        return False

    # Обратная совместимость с существующей системой global_staff.
    return perm in PERMS.get(role, set())


async def staff_log(
    actor_id: int,
    action: str,
    target_id: int | None,
    result: str,
    chat_id: int | None = None,
):
    conn = await db.get_db()
    await conn.execute(
        """
        INSERT INTO staff_log
            (actor_id, action, target_id, result, chat_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(actor_id),
            str(action)[:100],
            target_id,
            str(result)[:500],
            chat_id,
            time.time(),
        ),
    )
    await conn.commit()


async def assign_role(
    actor_id: int,
    target_id: int,
    role: str,
) -> tuple[bool, str]:
    if role not in (ROLE_ADMIN, ROLE_AGENT):
        return False, "Неизвестная роль."

    if target_id <= 0:
        return False, "Некорректный Telegram ID."

    if is_owner(target_id):
        return False, "Владельцу уже принадлежат все права."

    actor_role = await get_staff_role(actor_id)

    if role == ROLE_ADMIN and actor_role != ROLE_OWNER:
        return False, "Назначать глобальных администраторов может только владелец."

    if role == ROLE_AGENT and not await has_perm(actor_id, "staff.agents"):
        return False, "Недостаточно прав."

    current = await get_staff_role(target_id)

    if current == role:
        return False, f"Пользователь уже имеет роль: {ROLE_TITLES[role]}."

    if current == ROLE_ADMIN and actor_role != ROLE_OWNER:
        return False, "Изменять роль администратора может только владелец."

    conn = await db.get_db()
    await conn.execute(
        """
        INSERT INTO global_staff (user_id, role, granted_by, granted_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            role = excluded.role,
            granted_by = excluded.granted_by,
            granted_at = excluded.granted_at
        """,
        (target_id, role, actor_id, time.time()),
    )
    await conn.commit()

    note = f" (предыдущая роль: {ROLE_TITLES[current]})" if current else ""
    await staff_log(actor_id, f"+{role}", target_id, "ok" + note)

    return True, (
        f"✅ <code>{target_id}</code> назначен: "
        f"{ROLE_TITLES[role]}{note}."
    )


async def remove_role(
    actor_id: int,
    target_id: int,
    role: str,
) -> tuple[bool, str]:
    if role not in (ROLE_ADMIN, ROLE_AGENT):
        return False, "Неизвестная роль."

    if is_owner(target_id):
        return False, "Владельца нельзя лишить прав."

    actor_role = await get_staff_role(actor_id)

    if role == ROLE_ADMIN and actor_role != ROLE_OWNER:
        return False, "Снимать глобальных администраторов может только владелец."

    if role == ROLE_AGENT and not await has_perm(actor_id, "staff.agents"):
        return False, "Недостаточно прав."

    current = await get_staff_role(target_id)
    if current != role:
        return False, (
            f"Пользователь <code>{target_id}</code> "
            f"не имеет роль: {ROLE_TITLES[role]}."
        )

    conn = await db.get_db()
    await conn.execute(
        "DELETE FROM global_staff WHERE user_id = ?",
        (target_id,),
    )
    await conn.commit()

    await staff_log(actor_id, f"-{role}", target_id, "ok")
    return True, (
        f"✅ <code>{target_id}</code> снят с должности: "
        f"{ROLE_TITLES[role]}."
    )


async def list_staff() -> list[dict]:
    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT user_id, role, granted_by, granted_at
        FROM global_staff
        ORDER BY role, granted_at
        """
    )
    return [dict(row) for row in await cur.fetchall()]


async def get_staff_log(limit: int = 20) -> list[dict]:
    safe_limit = max(1, min(200, int(limit)))
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT * FROM staff_log ORDER BY id DESC LIMIT ?",
        (safe_limit,),
    )
    return [dict(row) for row in await cur.fetchall()]


# ---------- глобальный спам-блок ----------

async def spamblock_get(user_id: int) -> dict | None:
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT * FROM spamblock WHERE user_id = ?",
        (int(user_id),),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def is_spamblocked(user_id: int) -> bool:
    # Действующие сотрудники не должны блокироваться автоматически.
    if await get_staff_role(user_id):
        return False
    return await spamblock_get(user_id) is not None


async def spamblock_add(
    user_id: int,
    reason: str,
    source: str,
    added_by: int | None,
    chat_id: int | None = None,
    message_id: int | None = None,
    details: str = "",
) -> tuple[bool, str]:
    if user_id <= 0:
        return False, "Некорректный Telegram ID."

    if is_owner(user_id) or await get_staff_role(user_id):
        return False, "Сотрудников бота нельзя добавить в спам-блок."

    if await spamblock_get(user_id):
        return False, "Пользователь уже находится в спам-блоке."

    conn = await db.get_db()
    await conn.execute(
        """
        INSERT INTO spamblock
            (user_id, reason, source, added_by, chat_id,
             message_id, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            str(reason)[:300],
            str(source)[:50],
            added_by,
            chat_id,
            message_id,
            str(details)[:500],
            time.time(),
        ),
    )
    await conn.commit()

    await staff_log(
        added_by or 0,
        "spamblock_add",
        user_id,
        f"{source}: {str(reason)[:100]}",
        chat_id,
    )
    return True, f"⛔ <code>{user_id}</code> добавлен в глобальный спам-блок."


async def spamblock_remove(
    actor_id: int,
    user_id: int,
) -> tuple[bool, str]:
    if not await has_perm(actor_id, "spamblock.remove"):
        return False, "Недостаточно прав для снятия спам-блока."

    if not await spamblock_get(user_id):
        return False, (
            f"Пользователь <code>{user_id}</code> "
            "не находится в спам-блоке."
        )

    conn = await db.get_db()
    await conn.execute(
        "DELETE FROM spamblock WHERE user_id = ?",
        (user_id,),
    )
    await conn.commit()

    _hits.pop(user_id, None)
    await staff_log(actor_id, "spamblock_remove", user_id, "ok")

    return True, (
        f"✅ <code>{user_id}</code> удалён из спам-блока. "
        "Доступ к боту восстановлен."
    )


async def register_violation(
    user_id: int,
    reason: str,
    chat_id: int | None,
    message_id: int | None,
    now: float | None = None,
) -> bool:
    """Регистрирует нарушение антиспама и при необходимости блокирует."""
    if not await module_enabled("antispam"):
        return False

    current_time = time.time() if now is None else float(now)
    hits = [
        stamp for stamp in _hits.get(user_id, [])
        if current_time - stamp < AUTO_BLOCK_WINDOW
    ]
    hits.append(current_time)
    _hits[user_id] = hits

    if len(hits) < AUTO_BLOCK_HITS:
        return False

    ok, _message = await spamblock_add(
        user_id,
        f"авто: {reason}",
        "auto",
        None,
        chat_id,
        message_id,
        f"{len(hits)} нарушений за {AUTO_BLOCK_WINDOW // 60} мин",
    )
    return ok


# ---------- глобальные модули ----------

async def module_enabled(key: str) -> bool:
    if key not in MODULES:
        return False

    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT enabled FROM global_modules WHERE key = ?",
        (key,),
    )
    row = await cur.fetchone()
    return True if row is None else bool(row["enabled"])


async def set_module(
    actor_id: int,
    key: str,
    enabled: bool,
) -> tuple[bool, str]:
    if not is_owner(actor_id):
        return False, "Управлять глобальными модулями может только владелец."

    if key not in MODULES:
        return False, "Неизвестный модуль."

    conn = await db.get_db()
    await conn.execute(
        """
        INSERT INTO global_modules (key, enabled, changed_by, changed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            enabled = excluded.enabled,
            changed_by = excluded.changed_by,
            changed_at = excluded.changed_at
        """,
        (key, int(enabled), actor_id, time.time()),
    )
    await conn.commit()

    await staff_log(
        actor_id,
        "module_on" if enabled else "module_off",
        None,
        key,
    )

    status = "🟢 Включён" if enabled else "🔴 Выключен"
    return True, f"{status} модуль «{MODULES[key][0]}» во всех группах."


async def modules_state() -> dict[str, bool]:
    conn = await db.get_db()
    cur = await conn.execute("SELECT key, enabled FROM global_modules")
    rows = await cur.fetchall()
    saved = {row["key"]: bool(row["enabled"]) for row in rows}
    return {key: saved.get(key, True) for key in MODULES}
