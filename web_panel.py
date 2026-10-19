
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

from aiohttp import web

import config
import database as db
import staff_core as sc


OWNER_COOKIE = "chapi_owner_session"
STAFF_COOKIE = "chapi_staff_session"
SESSION_TTL = 8 * 60 * 60
LOGIN_LIMIT = 8
LOGIN_WINDOW = 10 * 60
PASSWORD_MIN_LENGTH = 12
BASE_DIR = Path(__file__).resolve().parent
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}

# Разрешения, которые владелец может назначать сотрудникам.
PERMISSIONS = {
    "spamblock.view": "Просмотр спам-блокировок",
    "spamblock.add": "Добавление спам-блокировок",
    "spamblock.remove": "Снятие спам-блокировок",
    "economy.balance": "Изменение баланса Чапи",
    "economy.gifts": "Выдача предметов и подарков",
    "birthdays.manage": "Управление днями рождения",
    "cases.view": "Просмотр жалоб",
    "cases.manage": "Обработка жалоб",
    "moderation.view": "Просмотр модерации",
    "moderation.manage": "Управление модерацией",
    "groups.view": "Просмотр групп",
    "groups.settings": "Изменение настроек групп",
    "staff.view": "Просмотр сотрудников",
    "logs.view": "Просмотр журналов",
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

    return (
        issued <= now + 60
        and now - issued <= SESSION_TTL
        and hmac.compare_digest(signature, _sign(payload))
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


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response(
        {"ok": False, "error": message},
        status=status,
        headers={"Cache-Control": "no-store"},
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
        actual = _password_hash(password, salt)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _new_staff_session(user_id: int, updated_at: float) -> str:
    stamp = int(time.time())
    nonce = secrets.token_urlsafe(18)
    payload = f"{user_id}.{stamp}.{updated_at:.6f}.{nonce}"
    return f"{payload}.{_sign(payload)}"


async def _staff_identity(request: web.Request) -> dict | None:
    """Проверяет подпись сессии и актуальность учётной записи."""
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


async def _owner_required(request: web.Request) -> web.Response | None:
    if not _valid_owner_session(request):
        return _json_error("Требуется вход владельца.", 401)
    return None


async def _staff_required(request: web.Request) -> dict | web.Response:
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
    """Права назначаются явно; роль admin сама по себе их не выдаёт."""
    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT 1
        FROM panel_permissions
        WHERE user_id = ?
          AND permission_key = ?
          AND scope_key IN (?, 'global')
        LIMIT 1
        """,
        (identity["user_id"], permission, scope),
    )
    return bool(await cur.fetchone())


async def _employee_group_access(
    identity: dict,
    chat_id: int,
    required: str = "view",
) -> bool:
    """Проверяет явный доступ к группе."""
    if identity["role"] not in {"admin", "agent"}:
        return False

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


@web.middleware
async def _security_headers(request: web.Request, handler):
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

async def panel_page(_request: web.Request) -> web.Response:
    path = BASE_DIR / "static" / "panel.html"
    if not path.is_file():
        return web.Response(
            text="Файл static/panel.html не найден.",
            status=404,
        )
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def owner_page(request: web.Request) -> web.Response:
    return await panel_page(request)


async def admin_page(request: web.Request) -> web.Response:
    return await panel_page(request)


async def agent_page(request: web.Request) -> web.Response:
    return await panel_page(request)


# ---------------------------------------------------------------------
# Вход владельца — сохраняем прежний API
# ---------------------------------------------------------------------

async def login(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    if not _panel_ready():
        return _json_error(
            "Панель не настроена. Проверь PANEL_PASSWORD, "
            "PANEL_SESSION_SECRET и OWNER_TELEGRAM_ID в Render.",
            503,
        )

    ip = request.remote or "unknown"
    now = time.time()
    attempts = [
        t for t in _LOGIN_ATTEMPTS.get(ip, [])
        if now - t < LOGIN_WINDOW
    ]

    if len(attempts) >= LOGIN_LIMIT:
        _LOGIN_ATTEMPTS[ip] = attempts
        return _json_error(
            "Слишком много попыток. Попробуйте позже.",
            429,
        )

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("Ожидался JSON.")

    supplied = payload.get("password", "") if isinstance(payload, dict) else ""

    if (
        not isinstance(supplied, str)
        or len(supplied) > 1024
        or not hmac.compare_digest(supplied, _password())
    ):
        attempts.append(now)
        _LOGIN_ATTEMPTS[ip] = attempts
        return _json_error("Неверный пароль.", 401)

    _LOGIN_ATTEMPTS.pop(ip, None)
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
    response.del_cookie(
        OWNER_COOKIE,
        path="/",
        secure=_secure_cookie(request),
        httponly=True,
        samesite="Strict",
    )
    return response


async def session_status(request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "authenticated": _valid_owner_session(request),
        "configured": _panel_ready(),
        "role": "owner" if _valid_owner_session(request) else None,
    })


# ---------------------------------------------------------------------
# Вход сотрудников
# ---------------------------------------------------------------------

async def staff_login(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    if not _panel_ready():
        return _json_error("Панель не настроена.", 503)

    ip = request.remote or "unknown"
    now = time.time()
    attempts = [
        t for t in _LOGIN_ATTEMPTS.get(ip, [])
        if now - t < LOGIN_WINDOW
    ]

    if len(attempts) >= LOGIN_LIMIT:
        _LOGIN_ATTEMPTS[ip] = attempts
        return _json_error("Слишком много попыток входа.", 429)

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("Ожидался JSON.")

    username = payload.get("username", "") if isinstance(payload, dict) else ""
    password = payload.get("password", "") if isinstance(payload, dict) else ""

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
        _LOGIN_ATTEMPTS[ip] = attempts
        return _json_error("Неверный логин или пароль.", 401)

    _LOGIN_ATTEMPTS.pop(ip, None)
    response = web.json_response({
        "ok": True,
        "role": row["role"],
        "username": row["username"],
    })
    response.set_cookie(
        STAFF_COOKIE,
        _new_staff_session(
            int(row["user_id"]),
            float(row["updated_at"]),
        ),
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
    response.del_cookie(
        STAFF_COOKIE,
        path="/",
        secure=_secure_cookie(request),
        httponly=True,
        samesite="Strict",
    )
    return response


async def staff_session_status(request: web.Request) -> web.Response:
    identity = await _staff_identity(request)
    if not identity:
        return web.json_response({
            "ok": True,
            "authenticated": False,
        })

    return web.json_response({
        "ok": True,
        "authenticated": True,
        **identity,
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
    group_access = [
        {"chat_id": row["chat_id"], "level": row["access_level"]}
        for row in await cur.fetchall()
    ]

    return web.json_response({
        "ok": True,
        "role": identity["role"],
        "permissions": permissions,
        "group_access": group_access,
        "permission_labels": PERMISSIONS,
    })


# ---------------------------------------------------------------------
# Управление учётными записями — только владелец
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
    return web.json_response({
        "ok": True,
        "accounts": [dict(row) for row in await cur.fetchall()],
    })


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

    if (
        not username
        or len(username) > 64
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", username)
    ):
        return _json_error(
            "Логин должен содержать только латинские буквы, "
            "цифры, точку, дефис или подчёркивание."
        )

    if (
        not isinstance(password, str)
        or len(password) < PASSWORD_MIN_LENGTH
        or len(password) > 1024
    ):
        return _json_error(
            f"Пароль должен содержать не менее {PASSWORD_MIN_LENGTH} символов."
        )

    if role not in {"admin", "agent"}:
        return _json_error("Роль должна быть admin или agent.")

    salt = secrets.token_hex(16)
    password_hash = _password_hash(password, salt)
    now = time.time()
    conn = await db.get_db()

    try:
        await conn.execute(
            """
            INSERT INTO panel_accounts (
                user_id, username, password_hash, password_salt,
                role, active, created_by, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                user_id,
                username,
                password_hash,
                salt,
                role,
                _owner_id(),
                now,
                now,
            ),
        )
        await conn.commit()
    except Exception as exc:
        await conn.rollback()
        if "UNIQUE constraint failed" in str(exc):
            return _json_error(
                "Этот Telegram ID или логин уже используется."
            )
        raise

    return web.json_response({"ok": True})


async def account_update(request: web.Request) -> web.Response:
    """Смена пароля, роли или отключение учётной записи."""
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

    role = payload.get("role")
    active = payload.get("active")
    password = payload.get("password")
    conn = await db.get_db()

    cur = await conn.execute(
        "SELECT user_id FROM panel_accounts WHERE user_id = ?",
        (user_id,),
    )
    if not await cur.fetchone():
        return _json_error("Сотрудник не найден.", 404)

    updates = ["updated_at = ?"]
    values: list = [time.time()]

    if role is not None:
        if role not in {"admin", "agent"}:
            return _json_error("Недопустимая роль.")
        updates.append("role = ?")
        values.append(role)

    if active is not None:
        if not isinstance(active, bool):
            return _json_error("Поле active должно быть true или false.")
        updates.append("active = ?")
        values.append(int(active))

    if password is not None:
        if (
            not isinstance(password, str)
            or len(password) < PASSWORD_MIN_LENGTH
            or len(password) > 1024
        ):
            return _json_error(
                f"Новый пароль должен содержать не менее "
                f"{PASSWORD_MIN_LENGTH} символов."
            )
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
        return _json_error("Некорректная область действия разрешения.")

    if action not in {"grant", "revoke"}:
        return _json_error("Действие должно быть grant или revoke.")

    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT 1 FROM panel_accounts WHERE user_id = ?",
        (user_id,),
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
        return _json_error("Недопустимая группа или уровень доступа.")

    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT 1 FROM panel_accounts WHERE user_id = ?",
        (user_id,),
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
# Существующие функции владельца
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
        "active_punishments": (
            "SELECT COUNT(*) FROM punishments WHERE active = 1"
        ),
        "notes": "SELECT COUNT(*) FROM notes",
        "autoreplies": "SELECT COUNT(*) FROM autoreplies",
        "spamblocked": "SELECT COUNT(*) FROM spamblock",
    }

    values = {}
    for key, sql in queries.items():
        cur = await conn.execute(sql)
        row = await cur.fetchone()
        values[key] = int(row[0] or 0)

    return web.json_response({
        "ok": True,
        "stats": values,
        "modules": await sc.modules_state(),
    })


async def groups(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT chat_id, data
        FROM settings
        WHERE chat_id < 0
        ORDER BY chat_id DESC
        LIMIT 200
        """
    )

    result = []
    for row in await cur.fetchall():
        try:
            settings = json.loads(row["data"] or "{}")
            if not isinstance(settings, dict):
                settings = {}
        except (TypeError, json.JSONDecodeError):
            settings = {}

        result.append({
            "chat_id": row["chat_id"],
            "settings": {
                key: bool(settings.get(key, default))
                for key, default in db.DEFAULT_SETTINGS.items()
                if isinstance(default, bool)
            },
        })

    return web.json_response({"ok": True, "groups": result})


async def set_group_setting(request: web.Request) -> web.Response:
    auth = await _owner_required(request)
    if auth:
        return auth
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)

    try:
        payload = await request.json()
        chat_id = int(payload.get("chat_id"))
        key = str(payload.get("key", ""))
        enabled = payload.get("enabled")
    except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
        return _json_error("Некорректные данные.")

    allowed = {
        key for key, value in db.DEFAULT_SETTINGS.items()
        if isinstance(value, bool)
    }
    if chat_id >= 0 or key not in allowed or not isinstance(enabled, bool):
        return _json_error("Недопустимая группа или настройка.")

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
        _owner_id(),
        "panel_setting_toggle",
        None,
        f"{key}={'on' if enabled else 'off'}",
    )
    return web.json_response({"ok": True})


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
    except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
        return _json_error("Введите числовой Telegram ID.")

    if (
        user_id <= 0
        or user_id > 9_223_372_036_854_775_807
        or role not in {sc.ROLE_ADMIN, sc.ROLE_AGENT}
        or action not in {"add", "remove"}
    ):
        return _json_error("Некорректный ID, роль или действие.")

    if action == "add":
        ok, message = await sc.assign_role(_owner_id(), user_id, role)
    else:
        ok, message = await sc.remove_role(_owner_id(), user_id, role)

    if not ok:
        return _json_error(re.sub(r"<[^>]*>", "", message), 400)

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
        FROM spamblock
        ORDER BY created_at DESC
        LIMIT 200
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
    except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
        return _json_error("Введите числовой Telegram ID.")

    if user_id <= 0 or user_id > 9_223_372_036_854_775_807:
        return _json_error("Некорректный Telegram ID.")

    if action == "add":
        if not reason:
            return _json_error("Укажите причину спам-блока.")
        ok, message = await sc.spamblock_add(
            user_id, reason, "panel", _owner_id()
        )
    elif action == "remove":
        ok, message = await sc.spamblock_remove(_owner_id(), user_id)
    else:
        return _json_error("Неизвестное действие.")

    if not ok:
        return _json_error(re.sub(r"<[^>]*>", "", message), 400)

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
    except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
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


# ---------------------------------------------------------------------
# Пример защищённого API сотрудника: просмотр спам-блокировок.
# Другие действия добавляем только с отдельной проверкой прав.
# ---------------------------------------------------------------------

async def staff_spamblocks(request: web.Request) -> web.Response:
    identity = await _staff_required(request)
    if isinstance(identity, web.Response):
        return identity

    if not await _employee_permission(identity, "spamblock.view"):
        return _json_error("Нет разрешения на просмотр спам-блокировок.", 403)

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT user_id, reason, source, added_by, chat_id, created_at
        FROM spamblock
        ORDER BY created_at DESC
        LIMIT 200
        """
    )
    return web.json_response({
        "ok": True,
        "items": [dict(row) for row in await cur.fetchall()],
    })


async def staff_spamblock_action(request: web.Request) -> web.Response:
    identity = await _staff_required(request)
    if isinstance(identity, web.Response):
        return identity

    try:
        payload = await request.json()
        user_id = int(payload.get("user_id"))
        action = str(payload.get("action", ""))
        reason = str(payload.get("reason", ""))[:300].strip()
    except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
        return _json_error("Некорректные данные.")

    if user_id <= 0 or user_id > 9_223_372_036_854_775_807:
        return _json_error("Некорректный Telegram ID.")

    if action not in {"add", "remove"}:
        return _json_error("Неизвестное действие.")

    permission = (
        "spamblock.add" if action == "add" else "spamblock.remove"
    )
    if not await _employee_permission(identity, permission):
        return _json_error("У сотрудника нет нужного разрешения.", 403)

    actor_id = identity["user_id"]

    if action == "add":
        if not reason:
            return _json_error("Укажите причину спам-блока.")
        ok, message = await sc.spamblock_add(
            user_id, reason, "staff_panel", actor_id
        )
    else:
        ok, message = await sc.spamblock_remove(actor_id, user_id)

    if not ok:
        return _json_error(re.sub(r"<[^>]*>", "", message), 400)

    return web.json_response({
        "ok": True,
        "message": re.sub(r"<[^>]*>", "", message),
    })


def add_routes(app: web.Application) -> None:
    """Регистрирует маршруты панели, сохраняя старые URL."""
    if _security_headers not in app.middlewares:
        app.middlewares.append(_security_headers)

    # Страницы. Интерфейс пока обслуживается общим HTML-файлом.
    app.router.add_get("/", panel_page)
    app.router.add_get("/panel", panel_page)
    app.router.add_get("/panel/", panel_page)
    app.router.add_get("/owner", owner_page)
    app.router.add_get("/admin", admin_page)
    app.router.add_get("/agent", agent_page)

    # Владелец.
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

    # Управление отдельными учётными записями сотрудников.
    app.router.add_get("/api/owner/accounts", accounts_list)
    app.router.add_post("/api/owner/accounts", account_create)
    app.router.add_post("/api/owner/accounts/update", account_update)
    app.router.add_post(
        "/api/owner/accounts/permissions", account_permissions
    )
    app.router.add_post(
        "/api/owner/accounts/group-access", account_group_access
    )

    # Вход и профиль сотрудника.
    app.router.add_post("/api/staff/login", staff_login)
    app.router.add_post("/api/staff/logout", staff_logout)
    app.router.add_get("/api/staff/session", staff_session_status)
    app.router.add_get("/api/staff/me/permissions", staff_my_permissions)

    # Действия сотрудников с серверной проверкой разрешений.
    app.router.add_get("/api/staff/spamblocks", staff_spamblocks)
    app.router.add_post(
        "/api/staff/spamblock-action", staff_spamblock_action
    )
