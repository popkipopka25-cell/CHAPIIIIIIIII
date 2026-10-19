
"""Защищённая веб-панель владельца и сотрудников Чапи."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

from aiohttp import web, ClientSession

import config
import database as db
import staff_core as sc


OWNER_COOKIE = "chapi_owner_session"
STAFF_COOKIE = "chapi_staff_session"
SESSION_TTL = 8 * 60 * 60
LOGIN_LIMIT = 8
LOGIN_WINDOW = 600
PASSWORD_MIN_LENGTH = 12
BASE_DIR = Path(__file__).resolve().parent
LOGIN_ATTEMPTS: dict[str, list[float]] = {}

PERMISSIONS = {
    "groups.view": "Просмотр групп",
    "groups.settings": "Изменение настроек групп",
    "users.view": "Поиск пользователей",
    "cases.view": "Просмотр жалоб",
    "cases.manage": "Обработка жалоб",
    "moderation.view": "Просмотр наказаний",
    "moderation.manage": "Управление наказаниями",
    "logs.view": "Просмотр журналов",
    "staff.view": "Просмотр сотрудников",
    "spamblock.view": "Просмотр спам-блокировок",
    "spamblock.add": "Добавление спам-блокировок",
    "spamblock.remove": "Снятие спам-блокировок",
    "economy.balance": "Изменение баланса",
    "economy.gifts": "Выдача подарков",
    "birthdays.manage": "Управление днями рождения",
}


def _secret() -> str:
    return os.getenv("PANEL_SESSION_SECRET", "").strip()


def _password() -> str:
    return os.getenv("PANEL_PASSWORD", "")


def _owner_id() -> int:
    return int(config.OWNER_TELEGRAM_ID)


def _panel_ready() -> bool:
    password = _password()
    secret = _secret()
    token = getattr(config, "BOT_TOKEN", "")

    return bool(
        _owner_id() > 0
        and len(password) >= PASSWORD_MIN_LENGTH
        and len(secret) >= 32
        and password != secret
        and password != token
        and secret != token
        and not password.startswith("change-this-")
        and not secret.startswith("change-this-")
    )


def _sign(value: str) -> str:
    return hmac.new(
        _secret().encode(),
        value.encode(),
        hashlib.sha256,
    ).hexdigest()


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response(
        {"ok": False, "error": message},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True
    host = request.headers.get("Host", "")
    return origin in (f"https://{host}", f"http://{host}")


def _secure_cookie(request: web.Request) -> bool:
    return (
        request.headers.get("X-Forwarded-Proto", request.scheme).lower()
        == "https"
    )


def _owner_session() -> str:
    payload = f"{int(time.time())}.{secrets.token_urlsafe(24)}"
    return f"{payload}.{_sign(payload)}"


def _valid_owner_session(request: web.Request) -> bool:
    if not _panel_ready():
        return False

    raw = request.cookies.get(OWNER_COOKIE, "")
    try:
        stamp, nonce, signature = raw.split(".", 2)
        issued = int(stamp)
    except (ValueError, TypeError):
        return False

    payload = f"{stamp}.{nonce}"
    now = int(time.time())

    return bool(
        issued <= now + 60
        and now - issued <= SESSION_TTL
        and hmac.compare_digest(signature, _sign(payload))
    )


def _password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        310_000,
    ).hex()


def _verify_password(password: str, salt: str, expected: str) -> bool:
    try:
        return hmac.compare_digest(
            _password_hash(password, salt),
            expected,
        )
    except (ValueError, TypeError):
        return False


def _new_staff_session(user_id: int, updated_at: float) -> str:
    stamp = int(time.time())
    nonce = secrets.token_urlsafe(18)
    payload = f"{user_id}.{stamp}.{updated_at:.6f}.{nonce}"
    return f"{payload}.{_sign(payload)}"


async def _staff_identity(request: web.Request) -> dict | None:
    if not _panel_ready():
        return None

    raw = request.cookies.get(STAFF_COOKIE, "")
    try:
        user_text, stamp_text, updated_text, nonce, signature = (
            raw.split(".", 4)
        )
        user_id = int(user_text)
        issued = int(stamp_text)
        session_updated = float(updated_text)
    except (ValueError, TypeError):
        return None

    payload = f"{user_text}.{stamp_text}.{updated_text}.{nonce}"
    now = int(time.time())

    if (
        user_id <= 0
        or issued > now + 60
        or now - issued > SESSION_TTL
        or not hmac.compare_digest(signature, _sign(payload))
    ):
        return None

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT user_id, username, role, active, updated_at
        FROM panel_accounts
        WHERE user_id = ?
        """,
        (user_id,),
    )
    row = await cur.fetchone()

    if not row or not row["active"]:
        return None

    if abs(float(row["updated_at"]) - session_updated) > 0.000001:
        return None

    return {
        "user_id": int(row["user_id"]),
        "username": row["username"],
        "role": row["role"],
    }


async def _owner_required(request: web.Request):
    if not _valid_owner_session(request):
        return _json_error("Требуется вход владельца.", 401)
    return None


async def _staff_required(request: web.Request):
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    identity = await _staff_identity(request)
    if not identity:
        return _json_error("Требуется вход сотрудника.", 401)

    return identity


async def _employee_permission(
    identity: dict,
    permission: str,
    scope: str = "global",
) -> bool:
    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT 1
        FROM panel_permissions
        WHERE user_id = ?
          AND permission_key = ?
          AND scope_key = ?
        LIMIT 1
        """,
        (identity["user_id"], permission, scope),
    )
    if await cur.fetchone():
        return True

    # Для глобального запроса подходит только глобальное разрешение.
    if scope == "global":
        return False

    cur = await conn.execute(
        """
        SELECT 1
        FROM panel_permissions
        WHERE user_id = ?
          AND permission_key = ?
          AND scope_key = 'global'
        LIMIT 1
        """,
        (identity["user_id"], permission),
    )
    return bool(await cur.fetchone())


async def _employee_group_access(
    identity: dict,
    chat_id: int,
    required: str = "view",
) -> bool:
    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT access_level
        FROM panel_group_access
        WHERE user_id = ? AND chat_id = ?
        """,
        (identity["user_id"], chat_id),
    )
    row = await cur.fetchone()

    levels = {"none": 0, "view": 1, "manage": 2}
    return bool(
        row
        and levels.get(row["access_level"], 0) >= levels.get(required, 1)
    )


async def _authorize(
    request: web.Request,
    permission: str,
    chat_id: int | None = None,
    manage: bool = False,
):
    if _valid_owner_session(request):
        return {"role": "owner", "user_id": _owner_id()}, None

    identity = await _staff_required(request)
    if isinstance(identity, web.Response):
        return None, identity

    scope = f"group:{chat_id}" if chat_id is not None else "global"
    if not await _employee_permission(identity, permission, scope):
        return None, _json_error("Недостаточно разрешений.", 403)

    if chat_id is not None:
        level = "manage" if manage else "view"
        if not await _employee_group_access(identity, chat_id, level):
            return None, _json_error(
                "У сотрудника нет доступа к этой группе.",
                403,
            )

    return identity, None


@web.middleware
async def _security_headers(request, handler):
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    return response


# ---------------------------------------------------------------------
# Страницы
# ---------------------------------------------------------------------

async def panel_page(request: web.Request) -> web.Response:
    path = BASE_DIR / "static" / "panel.html"
    if not path.is_file():
        return web.Response(text="Файл static/panel.html не найден.", status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def owner_page(request):
    return await panel_page(request)


async def admin_page(request):
    return await panel_page(request)


async def agent_page(request):
    return await panel_page(request)


# ---------------------------------------------------------------------
# Авторизация владельца
# ---------------------------------------------------------------------

async def login(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    if not _panel_ready():
        return _json_error("Панель не настроена в Render.", 503)

    ip = request.remote or "unknown"
    now = time.time()
    attempts = [
        stamp for stamp in LOGIN_ATTEMPTS.get(ip, [])
        if now - stamp < LOGIN_WINDOW
    ]

    if len(attempts) >= LOGIN_LIMIT:
        LOGIN_ATTEMPTS[ip] = attempts
        return _json_error("Слишком много попыток. Попробуйте позже.", 429)

    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return _json_error("Ожидался JSON.")

    supplied = payload.get("password", "") if isinstance(payload, dict) else ""
    if (
        not isinstance(supplied, str)
        or len(supplied) > 1024
        or not hmac.compare_digest(supplied, _password())
    ):
        attempts.append(now)
        LOGIN_ATTEMPTS[ip] = attempts
        return _json_error("Неверный пароль.", 401)

    LOGIN_ATTEMPTS.pop(ip, None)
    response = web.json_response({"ok": True, "role": "owner"})
    response.set_cookie(
        OWNER_COOKIE,
        _owner_session(),
        max_age=SESSION_TTL,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="Strict",
        path="/",
    )
    return response


async def logout(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    response = web.json_response({"ok": True})
    response.del_cookie(OWNER_COOKIE, path="/", httponly=True, samesite="Strict")
    return response


async def session_status(request: web.Request) -> web.Response:
    authenticated = _valid_owner_session(request)
    return web.json_response({
        "ok": True,
        "authenticated": authenticated,
        "configured": _panel_ready(),
        "role": "owner" if authenticated else None,
    })


# ---------------------------------------------------------------------
# Авторизация сотрудников
# ---------------------------------------------------------------------

async def staff_login(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    if not _panel_ready():
        return _json_error("Панель не настроена.", 503)

    ip = request.remote or "unknown"
    now = time.time()
    attempts = [
        stamp for stamp in LOGIN_ATTEMPTS.get(ip, [])
        if now - stamp < LOGIN_WINDOW
    ]

    if len(attempts) >= LOGIN_LIMIT:
        LOGIN_ATTEMPTS[ip] = attempts
        return _json_error("Слишком много попыток входа.", 429)

    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return _json_error("Ожидался JSON.")

    if not isinstance(payload, dict):
        return _json_error("Некорректные данные.")

    username = payload.get("username", "")
    password = payload.get("password", "")

    if (
        not isinstance(username, str)
        or not isinstance(password, str)
        or len(username) > 64
        or len(password) > 1024
    ):
        return _json_error("Некорректные данные.")

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT user_id, username, password_hash, password_salt,
               role, active, updated_at
        FROM panel_accounts
        WHERE username = ? COLLATE NOCASE
        """,
        (username.strip(),),
    )
    row = await cur.fetchone()

    valid = bool(
        row
        and row["active"]
        and _verify_password(
            password,
            row["password_salt"],
            row["password_hash"],
        )
    )

    if not valid:
        attempts.append(now)
        LOGIN_ATTEMPTS[ip] = attempts
        return _json_error("Неверный логин или пароль.", 401)

    LOGIN_ATTEMPTS.pop(ip, None)
    response = web.json_response({
        "ok": True,
        "role": row["role"],
        "username": row["username"],
    })
    response.set_cookie(
        STAFF_COOKIE,
        _new_staff_session(int(row["user_id"]), float(row["updated_at"])),
        max_age=SESSION_TTL,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="Strict",
        path="/",
    )
    return response


async def staff_logout(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)
    response = web.json_response({"ok": True})
    response.del_cookie(STAFF_COOKIE, path="/", httponly=True, samesite="Strict")
    return response


async def staff_session_status(request: web.Request) -> web.Response:
    identity = await _staff_identity(request)
    return web.json_response({
        "ok": True,
        "authenticated": bool(identity),
        **(identity or {}),
    })


async def staff_my_permissions(request: web.Request) -> web.Response:
    identity = await _staff_required(request)
    if isinstance(identity, web.Response):
        return identity

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT permission_key, scope_key
        FROM panel_permissions
        WHERE user_id = ?
        ORDER BY scope_key, permission_key
        """,
        (identity["user_id"],),
    )
    permissions = [
        {"key": row["permission_key"], "scope": row["scope_key"]}
        for row in await cur.fetchall()
    ]

    cur = await conn.execute(
        """
        SELECT chat_id, access_level
        FROM panel_group_access
        WHERE user_id = ?
        ORDER BY chat_id
        """,
        (identity["user_id"],),
    )
    access = [
        {"chat_id": row["chat_id"], "level": row["access_level"]}
        for row in await cur.fetchall()
    ]

    return web.json_response({
        "ok": True,
        "role": identity["role"],
        "permissions": permissions,
        "group_access": access,
        "permission_labels": PERMISSIONS,
    })


# ---------------------------------------------------------------------
# Учётные записи сотрудников
# ---------------------------------------------------------------------

async def accounts_list(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT user_id, username, role, active, created_by,
               created_at, updated_at
        FROM panel_accounts
        ORDER BY created_at DESC
        LIMIT 500
        """
    )
    accounts = [dict(row) for row in await cur.fetchall()]

    for account in accounts:
        cur = await conn.execute(
            """
            SELECT permission_key, scope_key
            FROM panel_permissions WHERE user_id = ?
            """,
            (account["user_id"],),
        )
        account["permissions"] = [
            {"key": row["permission_key"], "scope": row["scope_key"]}
            for row in await cur.fetchall()
        ]

    return web.json_response({"ok": True, "accounts": accounts})


async def account_create(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    try:
        payload = await request.json()
        user_id = int(payload.get("user_id"))
        username = str(payload.get("username", "")).strip()
        password = payload.get("password", "")
        role = str(payload.get("role", ""))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return _json_error("Проверьте введённые данные.")

    if user_id <= 0 or user_id == _owner_id():
        return _json_error("Недопустимый Telegram ID.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", username):
        return _json_error("Некорректный логин.")
    if not isinstance(password, str) or not PASSWORD_MIN_LENGTH <= len(password) <= 1024:
        return _json_error(f"Пароль должен содержать минимум {PASSWORD_MIN_LENGTH} символов.")
    if role not in {"admin", "agent"}:
        return _json_error("Роль должна быть admin или agent.")

    salt = secrets.token_hex(16)
    now = time.time()
    conn = await db.get_db()

    try:
        await conn.execute(
            """
            INSERT INTO panel_accounts (
                user_id, username, password_hash, password_salt,
                role, active, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                user_id, username, _password_hash(password, salt), salt,
                role, _owner_id(), now, now,
            ),
        )
        await conn.commit()
    except Exception as exc:
        await conn.rollback()
        if "UNIQUE constraint failed" in str(exc):
            return _json_error("Этот ID или логин уже используется.")
        raise

    return web.json_response({"ok": True})


async def account_update(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    try:
        payload = await request.json()
        user_id = int(payload.get("user_id"))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return _json_error("Некорректный Telegram ID.")

    if user_id <= 0 or user_id == _owner_id():
        return _json_error("Недопустимый Telegram ID.")

    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT user_id FROM panel_accounts WHERE user_id = ?",
        (user_id,),
    )
    if not await cur.fetchone():
        return _json_error("Сотрудник не найден.", 404)

    updates = ["updated_at = ?"]
    values = [time.time()]

    role = payload.get("role")
    active = payload.get("active")
    password = payload.get("password")

    if role is not None:
        if role not in {"admin", "agent"}:
            return _json_error("Недопустимая роль.")
        updates.append("role = ?")
        values.append(role)

    if active is not None:
        if not isinstance(active, bool):
            return _json_error("active должен быть true или false.")
        updates.append("active = ?")
        values.append(int(active))

    if password is not None:
        if not isinstance(password, str) or not PASSWORD_MIN_LENGTH <= len(password) <= 1024:
            return _json_error("Новый пароль слишком короткий или длинный.")
        salt = secrets.token_hex(16)
        updates.extend(["password_hash = ?", "password_salt = ?"])
        values.extend([_password_hash(password, salt), salt])

    values.append(user_id)
    await conn.execute(
        f"UPDATE panel_accounts SET {', '.join(updates)} WHERE user_id = ?",
        values,
    )
    await conn.commit()
    return web.json_response({"ok": True})


async def account_permissions(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    try:
        payload = await request.json()
        user_id = int(payload.get("user_id"))
        permission = str(payload.get("permission", ""))
        scope = str(payload.get("scope", "global"))
        action = str(payload.get("action", "grant"))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return _json_error("Некорректные данные.")

    if permission not in PERMISSIONS:
        return _json_error("Неизвестное разрешение.")
    if scope != "global" and not re.fullmatch(r"group:-\d+", scope):
        return _json_error("Некорректная область действия.")
    if action not in {"grant", "revoke"}:
        return _json_error("Неизвестное действие.")

    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT 1 FROM panel_accounts WHERE user_id = ?", (user_id,)
    )
    if not await cur.fetchone():
        return _json_error("Сотрудник не найден.", 404)

    if action == "grant":
        await conn.execute(
            """
            INSERT OR REPLACE INTO panel_permissions
                (user_id, permission_key, scope_key, granted_by, granted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, permission, scope, _owner_id(), time.time()),
        )
    else:
        await conn.execute(
            """
            DELETE FROM panel_permissions
            WHERE user_id = ? AND permission_key = ? AND scope_key = ?
            """,
            (user_id, permission, scope),
        )

    await conn.commit()
    return web.json_response({"ok": True})


async def account_group_access(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    try:
        payload = await request.json()
        user_id = int(payload.get("user_id"))
        chat_id = int(payload.get("chat_id"))
        level = str(payload.get("level", "view"))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return _json_error("Некорректные данные.")

    if chat_id >= 0 or level not in {"none", "view", "manage"}:
        return _json_error("Некорректная группа или уровень доступа.")

    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT 1 FROM panel_accounts WHERE user_id = ?", (user_id,)
    )
    if not await cur.fetchone():
        return _json_error("Сотрудник не найден.", 404)

    cur = await conn.execute(
        "SELECT 1 FROM settings WHERE chat_id = ? AND chat_id < 0",
        (chat_id,),
    )
    if not await cur.fetchone():
        return _json_error("Группа не найдена.", 404)

    await conn.execute(
        """
        INSERT OR REPLACE INTO panel_group_access
            (user_id, chat_id, access_level, granted_by, granted_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, chat_id, level, _owner_id(), time.time()),
    )
    await conn.commit()
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------
# Статистика и группы
# ---------------------------------------------------------------------

async def summary(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth

    conn = await db.get_db()
    queries = {
        "users": "SELECT COUNT(*) FROM users",
        "groups": "SELECT COUNT(*) FROM settings WHERE chat_id < 0",
        "clans": "SELECT COUNT(*) FROM clans",
        "active_punishments": "SELECT COUNT(*) FROM punishments WHERE active = 1",
        "cases": "SELECT COUNT(*) FROM cases WHERE status = 'pending'",
        "spamblocked": "SELECT COUNT(*) FROM spamblock",
    }

    stats = {}
    for key, sql in queries.items():
        cur = await conn.execute(sql)
        row = await cur.fetchone()
        stats[key] = int(row[0] or 0)

    return web.json_response({
        "ok": True,
        "stats": stats,
        "modules": await sc.modules_state(),
    })


async def groups(request: web.Request) -> web.Response:
    identity, error = await _authorize(request, "groups.view")
    if error:
        return error

    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT chat_id, data FROM settings WHERE chat_id < 0 ORDER BY chat_id DESC LIMIT 500"
    )
    result = []

    for row in await cur.fetchall():
        try:
            settings = json.loads(row["data"] or "{}")
            if not isinstance(settings, dict):
                settings = {}
        except (TypeError, json.JSONDecodeError):
            settings = {}

        if identity["role"] != "owner":
            if not await _employee_group_access(identity, int(row["chat_id"])):
                continue

        result.append({
            "chat_id": int(row["chat_id"]),
            "settings": {
                key: bool(settings.get(key, default))
                for key, default in db.DEFAULT_SETTINGS.items()
                if isinstance(default, bool)
            },
        })

    return web.json_response({"ok": True, "groups": result})


async def set_group_setting(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    try:
        payload = await request.json()
        chat_id = int(payload.get("chat_id"))
        key = str(payload.get("key", ""))
        enabled = payload.get("enabled")
    except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
        return _json_error("Некорректные данные.")

    identity, error = await _authorize(
        request, "groups.settings", chat_id, manage=True
    )
    if error:
        return error

    allowed = {
        name for name, value in db.DEFAULT_SETTINGS.items()
        if isinstance(value, bool)
    }
    if chat_id >= 0 or key not in allowed or not isinstance(enabled, bool):
        return _json_error("Недопустимая настройка.")

    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT 1 FROM settings WHERE chat_id = ? AND chat_id < 0",
        (chat_id,),
    )
    if not await cur.fetchone():
        return _json_error("Группа не найдена.", 404)

    await db.set_setting(chat_id, key, enabled)
    await db.log_action(
        chat_id,
        identity["user_id"],
        "panel_setting_toggle",
        None,
        f"{key}={'on' if enabled else 'off'}",
    )
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------
# Поиск пользователей и аватары
# ---------------------------------------------------------------------

async def users_search(request: web.Request) -> web.Response:
    identity, error = await _authorize(request, "users.view")
    if error:
        return error

    query = request.query.get("q", "").strip().lstrip("@")
    if len(query) < 2:
        return web.json_response({"ok": True, "users": []})
    if len(query) > 100:
        return _json_error("Слишком длинный запрос.")

    conn = await db.get_db()
    pattern = f"%{query}%"

    if query.isdigit():
        cur = await conn.execute(
            """
            SELECT user_id, username, first_name, first_seen,
                   xp, level, messages_count
            FROM users
            WHERE CAST(user_id AS TEXT) LIKE ?
               OR username LIKE ? COLLATE NOCASE
               OR first_name LIKE ? COLLATE NOCASE
            ORDER BY user_id DESC LIMIT 50
            """,
            (pattern, pattern, pattern),
        )
    else:
        cur = await conn.execute(
            """
            SELECT user_id, username, first_name, first_seen,
                   xp, level, messages_count
            FROM users
            WHERE username LIKE ? COLLATE NOCASE
               OR first_name LIKE ? COLLATE NOCASE
            ORDER BY user_id DESC LIMIT 50
            """,
            (pattern, pattern),
        )

    users = []
    for row in await cur.fetchall():
        item = dict(row)
        item["avatar_url"] = f"/api/panel/users/{item['user_id']}/avatar"
        users.append(item)

    return web.json_response({"ok": True, "users": users})


async def user_avatar(request: web.Request) -> web.Response:
    identity, error = await _authorize(request, "users.view")
    if error:
        return error

    try:
        user_id = int(request.match_info["user_id"])
    except (ValueError, KeyError):
        return _json_error("Некорректный ID.")

    token = getattr(config, "BOT_TOKEN", "")
    if not token:
        return _json_error("Токен бота не настроен.", 503)

    try:
        async with ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{token}/getUserProfilePhotos",
                params={"user_id": user_id, "limit": 1},
                timeout=10,
            ) as response:
                data = await response.json()

            if not data.get("ok") or not data.get("result", {}).get("photos"):
                return web.Response(status=404)

            photo = data["result"]["photos"][0][-1]["file_id"]

            async with session.get(
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": photo},
                timeout=10,
            ) as response:
                file_data = await response.json()

            if not file_data.get("ok"):
                return web.Response(status=404)

            file_path = file_data["result"]["file_path"]
            async with session.get(
                f"https://api.telegram.org/file/bot{token}/{file_path}",
                timeout=15,
            ) as response:
                if response.status != 200:
                    return web.Response(status=404)
                content = await response.read()
                content_type = response.headers.get(
                    "Content-Type", "image/jpeg"
                )

        return web.Response(
            body=content,
            content_type=content_type,
            headers={"Cache-Control": "private, max-age=300"},
        )
    except Exception:
        return web.Response(status=404)


# ---------------------------------------------------------------------
# Жалобы
# ---------------------------------------------------------------------

async def cases_list(request: web.Request) -> web.Response:
    identity, error = await _authorize(request, "cases.view")
    if error:
        return error

    try:
        limit = max(1, min(100, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50

    status = request.query.get("status", "").strip()
    conn = await db.get_db()

    if status in {"pending", "approved", "rejected", "closed"}:
        cur = await conn.execute(
            """
            SELECT c.*, u.username, u.first_name
            FROM cases c
            LEFT JOIN users u ON u.user_id = c.user_id
            WHERE c.status = ?
            ORDER BY c.id DESC LIMIT ?
            """,
            (status, limit),
        )
    else:
        cur = await conn.execute(
            """
            SELECT c.*, u.username, u.first_name
            FROM cases c
            LEFT JOIN users u ON u.user_id = c.user_id
            ORDER BY c.id DESC LIMIT ?
            """,
            (limit,),
        )

    return web.json_response({
        "ok": True,
        "items": [dict(row) for row in await cur.fetchall()],
    })


async def case_action(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    try:
        payload = await request.json()
        case_id = int(payload.get("case_id"))
        status = str(payload.get("status", ""))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return _json_error("Некорректные данные.")

    if status not in {"approved", "rejected", "closed", "pending"}:
        return _json_error("Недопустимый статус жалобы.")

    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT id, chat_id, user_id, status FROM cases WHERE id = ?",
        (case_id,),
    )
    case = await cur.fetchone()
    if not case:
        return _json_error("Жалоба не найдена.", 404)

    chat_id = int(case["chat_id"] or 0)
    identity, error = await _authorize(
        request,
        "cases.manage",
        chat_id if chat_id < 0 else None,
        manage=True,
    )
    if error:
        return error

    await conn.execute(
        "UPDATE cases SET status = ?, decided_by = ? WHERE id = ?",
        (status, identity["user_id"], case_id),
    )
    await conn.commit()

    if chat_id < 0:
        await db.log_action(
            chat_id,
            identity["user_id"],
            "case_status_changed",
            int(case["user_id"] or 0),
            f"case={case_id}; status={status}",
        )

    return web.json_response({"ok": True, "case_id": case_id, "status": status})


# ---------------------------------------------------------------------
# Журнал модерации и наказания
# ---------------------------------------------------------------------

async def moderation_logs(request: web.Request) -> web.Response:
    identity, error = await _authorize(request, "moderation.view")
    if error:
        return error

    try:
        limit = max(1, min(100, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT m.*, u.username, u.first_name
        FROM mod_log m
        LEFT JOIN users u ON u.user_id = m.target_id
        ORDER BY m.id DESC LIMIT ?
        """,
        (limit,),
    )
    return web.json_response({
        "ok": True,
        "items": [dict(row) for row in await cur.fetchall()],
    })


async def punishments_list(request: web.Request) -> web.Response:
    identity, error = await _authorize(request, "moderation.view")
    if error:
        return error

    try:
        limit = max(1, min(100, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT p.*, u.username, u.first_name
        FROM punishments p
        LEFT JOIN users u ON u.user_id = p.user_id
        ORDER BY p.id DESC LIMIT ?
        """,
        (limit,),
    )
    return web.json_response({
        "ok": True,
        "items": [dict(row) for row in await cur.fetchall()],
    })


# ---------------------------------------------------------------------
# Старые функции владельца: сотрудники, спам-блокировки, модули и логи
# ---------------------------------------------------------------------

async def staff_list(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth
    return web.json_response({"ok": True, "staff": await sc.list_staff()})


async def staff_action(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    try:
        payload = await request.json()
        user_id = int(payload.get("user_id"))
        role = str(payload.get("role", ""))
        action = str(payload.get("action", ""))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return _json_error("Введите числовой Telegram ID.")

    if user_id <= 0 or role not in {sc.ROLE_ADMIN, sc.ROLE_AGENT}:
        return _json_error("Некорректный ID или роль.")

    if action == "add":
        ok, message = await sc.assign_role(_owner_id(), user_id, role)
    elif action == "remove":
        ok, message = await sc.remove_role(_owner_id(), user_id, role)
    else:
        return _json_error("Неизвестное действие.")

    if not ok:
        return _json_error(re.sub(r"<[^>]*>", "", message))

    return web.json_response({
        "ok": True,
        "message": re.sub(r"<[^>]*>", "", message),
    })


async def spamblocks(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT user_id, reason, source, added_by, chat_id, created_at
        FROM spamblock ORDER BY created_at DESC LIMIT 200
        """
    )
    return web.json_response({
        "ok": True,
        "items": [dict(row) for row in await cur.fetchall()],
    })


async def spamblock_action(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    try:
        payload = await request.json()
        user_id = int(payload.get("user_id"))
        action = str(payload.get("action", ""))
        reason = str(payload.get("reason", ""))[:300].strip()
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return _json_error("Введите числовой Telegram ID.")

    if user_id <= 0:
        return _json_error("Некорректный ID.")

    if action == "add":
        if not reason:
            return _json_error("Укажите причину.")
        ok, message = await sc.spamblock_add(
            user_id, reason, "panel", _owner_id()
        )
    elif action == "remove":
        ok, message = await sc.spamblock_remove(_owner_id(), user_id)
    else:
        return _json_error("Неизвестное действие.")

    if not ok:
        return _json_error(re.sub(r"<[^>]*>", "", message))

    return web.json_response({
        "ok": True,
        "message": re.sub(r"<[^>]*>", "", message),
    })


async def modules(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth
    return web.json_response({
        "ok": True,
        "modules": await sc.modules_state(),
        "labels": {key: value[0] for key, value in sc.MODULES.items()},
    })


async def module_action(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    try:
        payload = await request.json()
        key = str(payload.get("key", ""))
        enabled = payload.get("enabled")
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return _json_error("Некорректные данные.")

    if key not in sc.MODULES or not isinstance(enabled, bool):
        return _json_error("Неизвестный модуль или состояние.")

    ok, message = await sc.set_module(_owner_id(), key, enabled)
    if not ok:
        return _json_error(message, 403)

    return web.json_response({"ok": True, "message": message})


async def staff_logs(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth
    try:
        limit = max(1, min(100, int(request.query.get("limit", "30"))))
    except ValueError:
        limit = 30
    return web.json_response({
        "ok": True,
        "logs": await sc.get_staff_log(limit),
    })


async def staff_spamblocks(request: web.Request) -> web.Response:
    identity, error = await _authorize(request, "spamblock.view")
    if error:
        return error

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT user_id, reason, source, added_by, chat_id, created_at
        FROM spamblock ORDER BY created_at DESC LIMIT 200
        """
    )
    return web.json_response({
        "ok": True,
        "items": [dict(row) for row in await cur.fetchall()],
    })


async def staff_spamblock_action(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    try:
        payload = await request.json()
        user_id = int(payload.get("user_id"))
        action = str(payload.get("action", ""))
        reason = str(payload.get("reason", ""))[:300].strip()
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return _json_error("Некорректные данные.")

    permission = {
        "add": "spamblock.add",
        "remove": "spamblock.remove",
    }.get(action)
    if not permission or user_id <= 0:
        return _json_error("Некорректное действие или ID.")

    identity, error = await _authorize(request, permission)
    if error:
        return error

    if action == "add":
        if not reason:
            return _json_error("Укажите причину.")
        ok, message = await sc.spamblock_add(
            user_id, reason, "staff_panel", identity["user_id"]
        )
    else:
        ok, message = await sc.spamblock_remove(
            identity["user_id"], user_id
        )

    if not ok:
        return _json_error(re.sub(r"<[^>]*>", "", message))

    return web.json_response({
        "ok": True,
        "message": re.sub(r"<[^>]*>", "", message),
    })


async def staff_logs_api(request: web.Request) -> web.Response:
    identity, error = await _authorize(request, "logs.view")
    if error:
        return error
    try:
        limit = max(1, min(100, int(request.query.get("limit", "30"))))
    except ValueError:
        limit = 30
    return web.json_response({
        "ok": True,
        "logs": await sc.get_staff_log(limit),
    })


# ---------------------------------------------------------------------
# Регистрация маршрутов
# ---------------------------------------------------------------------

def add_routes(app: web.Application) -> None:
    """Регистрирует маршруты панели. Вызывать один раз при создании app."""
    if _security_headers not in app.middlewares:
        app.middlewares.append(_security_headers)

    app.router.add_get("/", panel_page)
    app.router.add_get("/panel", panel_page)
    app.router.add_get("/panel/", panel_page)
    app.router.add_get("/owner", owner_page)
    app.router.add_get("/admin", admin_page)
    app.router.add_get("/agent", agent_page)

    # Вход владельца.
    app.router.add_post("/api/panel/login", login)
    app.router.add_post("/api/panel/logout", logout)
    app.router.add_get("/api/panel/session", session_status)
    app.router.add_get("/api/panel/summary", summary)
    app.router.add_get("/api/panel/groups", groups)
    app.router.add_post("/api/panel/group-setting", set_group_setting)
    app.router.add_get("/api/panel/staff", staff_list)
    app.router.add_post("/api/panel/staff-action", staff_action)
    app.router.add_get("/api/panel/spamblocks", spamblocks)
    app.router.add_post("/api/panel/spamblock-action", spamblock_action)
    app.router.add_get("/api/panel/modules", modules)
    app.router.add_post("/api/panel/module-action", module_action)
    app.router.add_get("/api/panel/logs", staff_logs)

    # Аккаунты сотрудников и права.
    app.router.add_get("/api/owner/accounts", accounts_list)
    app.router.add_post("/api/owner/accounts", account_create)
    app.router.add_post("/api/owner/accounts/update", account_update)
    app.router.add_post("/api/owner/accounts/permissions", account_permissions)
    app.router.add_post("/api/owner/accounts/group-access", account_group_access)

    # Вход сотрудников.
    app.router.add_post("/api/staff/login", staff_login)
    app.router.add_post("/api/staff/logout", staff_logout)
    app.router.add_get("/api/staff/session", staff_session_status)
    app.router.add_get("/api/staff/me/permissions", staff_my_permissions)

    # Пользователи и аватары.
    app.router.add_get("/api/panel/users/search", users_search)
    app.router.add_get("/api/staff/users/search", users_search)
    app.router.add_get("/api/panel/users/{user_id}/avatar", user_avatar)
    app.router.add_get("/api/staff/users/{user_id}/avatar", user_avatar)

    # Жалобы.
    app.router.add_get("/api/panel/cases", cases_list)
    app.router.add_get("/api/staff/cases", cases_list)
    app.router.add_post("/api/panel/case-action", case_action)
    app.router.add_post("/api/staff/case-action", case_action)

    # Модерация и журналы.
    app.router.add_get("/api/panel/moderation-logs", moderation_logs)
    app.router.add_get("/api/staff/moderation-logs", moderation_logs)
    app.router.add_get("/api/panel/punishments", punishments_list)
    app.router.add_get("/api/staff/punishments", punishments_list)
    app.router.add_get("/api/staff/logs", staff_logs_api)

    # Действия сотрудников со спам-блокировками.
    app.router.add_get("/api/staff/spamblocks", staff_spamblocks)
    app.router.add_post("/api/staff/spamblock-action", staff_spamblock_action)
