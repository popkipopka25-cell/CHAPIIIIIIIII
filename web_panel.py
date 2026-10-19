"""Защищённая веб-панель владельца Чапи, встроенная в aiohttp-сервер бота."""
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

SESSION_COOKIE = "chapi_owner_session"
SESSION_TTL = 8 * 60 * 60
LOGIN_LIMIT = 8
LOGIN_WINDOW = 10 * 60
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
BASE_DIR = Path(__file__).resolve().parent


def _secret() -> str:
    return os.getenv("PANEL_SESSION_SECRET", "").strip()


def _password() -> str:
    return os.getenv("PANEL_PASSWORD", "")


def _panel_ready() -> bool:
    password, secret = _password(), _secret()
    return bool(
        config.OWNER_TELEGRAM_ID > 0
        and len(password) >= 12
        and len(secret) >= 32
        and password != secret
        and password != config.BOT_TOKEN
        and secret != config.BOT_TOKEN
        and not password.startswith("change-this-")
        and not secret.startswith("change-this-")
    )


def _sign(value: str) -> str:
    return hmac.new(_secret().encode(), value.encode(), hashlib.sha256).hexdigest()


def _new_session() -> str:
    payload = f"{int(time.time())}.{secrets.token_urlsafe(18)}"
    return f"{payload}.{_sign(payload)}"


def _valid_session(request: web.Request) -> bool:
    if not _panel_ready():
        return False
    raw = request.cookies.get(SESSION_COOKIE, "")
    try:
        stamp, nonce, signature = raw.split(".", 2)
        issued = int(stamp)
    except (ValueError, TypeError):
        return False
    payload = f"{stamp}.{nonce}"
    now = int(time.time())
    return issued <= now + 60 and now - issued <= SESSION_TTL and hmac.compare_digest(signature, _sign(payload))


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True
    host = request.headers.get("Host", "")
    return origin in (f"https://{host}", f"http://{host}")


def _secure_cookie(request: web.Request) -> bool:
    return request.headers.get("X-Forwarded-Proto", request.scheme).lower() == "https"


def _owner_id() -> int:
    return int(config.OWNER_TELEGRAM_ID)


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status, headers={"Cache-Control": "no-store"})


@web.middleware
async def _security_headers(request: web.Request, handler):
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


async def _require_auth(request: web.Request) -> web.Response | None:
    if not _valid_session(request):
        return _json_error("Требуется вход владельца.", 401)
    return None


async def panel_page(_request: web.Request) -> web.Response:
    path = BASE_DIR / "static" / "panel.html"
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def login(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)
    if not _panel_ready():
        return _json_error("Панель не настроена. Задайте PANEL_PASSWORD длиной от 12 символов, отдельный PANEL_SESSION_SECRET длиной от 32 символов и корректный OWNER_TELEGRAM_ID. Не используйте примеры из .env.example.", 503)
    ip = request.remote or "unknown"
    now = time.time()
    attempts = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < LOGIN_WINDOW]
    if len(attempts) >= LOGIN_LIMIT:
        _LOGIN_ATTEMPTS[ip] = attempts
        return _json_error("Слишком много попыток. Попробуйте через 10 минут.", 429)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("Ожидался JSON.")
    supplied = payload.get("password", "") if isinstance(payload, dict) else ""
    if not isinstance(supplied, str) or len(supplied) > 1024 or not hmac.compare_digest(supplied, _password()):
        attempts.append(now)
        _LOGIN_ATTEMPTS[ip] = attempts
        return _json_error("Неверный пароль.", 401)
    _LOGIN_ATTEMPTS.pop(ip, None)
    response = web.json_response({"ok": True})
    response.set_cookie(SESSION_COOKIE, _new_session(), max_age=SESSION_TTL, httponly=True,
                        secure=_secure_cookie(request), samesite="Strict", path="/")
    return response


async def logout(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _json_error("Недопустимый источник запроса.", 403)
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/", secure=_secure_cookie(request), httponly=True, samesite="Strict")
    return response


async def session_status(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "authenticated": _valid_session(request),
                              "configured": _panel_ready()})


async def summary(request: web.Request) -> web.Response:
    auth = await _require_auth(request)
    if auth:
        return auth
    conn = await db.get_db()
    queries = {
        "users": "SELECT COUNT(*) FROM users",
        "groups": "SELECT COUNT(*) FROM settings WHERE chat_id < 0",
        "clans": "SELECT COUNT(*) FROM clans",
        "active_punishments": "SELECT COUNT(*) FROM punishments WHERE active=1",
        "notes": "SELECT COUNT(*) FROM notes",
        "autoreplies": "SELECT COUNT(*) FROM autoreplies",
        "spamblocked": "SELECT COUNT(*) FROM spamblock",
    }
    values: dict[str, int] = {}
    for key, sql in queries.items():
        cur = await conn.execute(sql)
        row = await cur.fetchone()
        values[key] = int(row[0] or 0)
    return web.json_response({"ok": True, "stats": values, "modules": await sc.modules_state()})


async def groups(request: web.Request) -> web.Response:
    auth = await _require_auth(request)
    if auth:
        return auth
    conn = await db.get_db()
    cur = await conn.execute("SELECT chat_id, data FROM settings WHERE chat_id < 0 ORDER BY chat_id DESC LIMIT 200")
    result = []
    for row in await cur.fetchall():
        try:
            settings = json.loads(row["data"] or "{}")
            if not isinstance(settings, dict):
                settings = {}
        except (TypeError, json.JSONDecodeError):
            settings = {}
        result.append({"chat_id": row["chat_id"], "settings": {
            key: bool(settings.get(key, default)) for key, default in db.DEFAULT_SETTINGS.items()
            if isinstance(default, bool)
        }})
    return web.json_response({"ok": True, "groups": result})


async def set_group_setting(request: web.Request) -> web.Response:
    auth = await _require_auth(request)
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
    allowed = {k for k, v in db.DEFAULT_SETTINGS.items() if isinstance(v, bool)}
    if chat_id >= 0 or key not in allowed or not isinstance(enabled, bool):
        return _json_error("Недопустимая группа или настройка.")
    conn = await db.get_db()
    cur = await conn.execute("SELECT 1 FROM settings WHERE chat_id=? AND chat_id<0", (chat_id,))
    if not await cur.fetchone():
        return _json_error("Группа не найдена.", 404)
    await db.set_setting(chat_id, key, enabled)
    await db.log_action(chat_id, _owner_id(), "panel_setting_toggle", None, f"{key}={'on' if enabled else 'off'}")
    return web.json_response({"ok": True})


async def staff_list(request: web.Request) -> web.Response:
    auth = await _require_auth(request)
    if auth:
        return auth
    return web.json_response({"ok": True, "staff": await sc.list_staff()})


async def staff_action(request: web.Request) -> web.Response:
    auth = await _require_auth(request)
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
    if user_id <= 0 or user_id > 9_223_372_036_854_775_807 or role not in {sc.ROLE_ADMIN, sc.ROLE_AGENT} or action not in {"add", "remove"}:
        return _json_error("Некорректный ID, роль или действие.")
    ok, message = await (sc.assign_role(_owner_id(), user_id, role) if action == "add" else sc.remove_role(_owner_id(), user_id, role))
    if not ok:
        return _json_error(re.sub(r"<[^>]*>", "", message), 400)
    return web.json_response({"ok": True, "message": re.sub(r"<[^>]*>", "", message)})


async def spamblocks(request: web.Request) -> web.Response:
    auth = await _require_auth(request)
    if auth:
        return auth
    conn = await db.get_db()
    cur = await conn.execute("SELECT user_id, reason, source, added_by, chat_id, created_at FROM spamblock ORDER BY created_at DESC LIMIT 200")
    return web.json_response({"ok": True, "items": [dict(r) for r in await cur.fetchall()]})


async def spamblock_action(request: web.Request) -> web.Response:
    auth = await _require_auth(request)
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
        ok, message = await sc.spamblock_add(user_id, reason, "panel", _owner_id())
    elif action == "remove":
        ok, message = await sc.spamblock_remove(_owner_id(), user_id)
    else:
        return _json_error("Неизвестное действие.")
    if not ok:
        return _json_error(re.sub(r"<[^>]*>", "", message), 400)
    return web.json_response({"ok": True, "message": re.sub(r"<[^>]*>", "", message)})


async def modules(request: web.Request) -> web.Response:
    auth = await _require_auth(request)
    if auth:
        return auth
    return web.json_response({"ok": True, "modules": await sc.modules_state(),
                              "labels": {k: v[0] for k, v in sc.MODULES.items()}})


async def module_action(request: web.Request) -> web.Response:
    auth = await _require_auth(request)
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
    auth = await _require_auth(request)
    if auth:
        return auth
    try:
        limit = max(1, min(100, int(request.query.get("limit", "30"))))
    except ValueError:
        limit = 30
    return web.json_response({"ok": True, "logs": await sc.get_staff_log(limit)})


def add_routes(app: web.Application) -> None:
    app.middlewares.append(_security_headers)
    app.router.add_get("/panel", panel_page)
    app.router.add_get("/panel/", panel_page)
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
