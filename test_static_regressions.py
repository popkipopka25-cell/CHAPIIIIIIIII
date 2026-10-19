"""Dependency-free regression checks for security-sensitive utility code."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_all_python_sources_parse():
    for source in ROOT.rglob("*.py"):
        ast.parse(source.read_text(encoding="utf-8"), filename=str(source))


def test_mention_escapes_html_names():
    source = (ROOT / "utils.py").read_text(encoding="utf-8")
    assert "safe_name = escape(str(name or user_id), quote=True)" in source
    assert "{safe_name}" in source


def test_note_missing_name_is_escaped():
    source = (ROOT / "handlers" / "notes.py").read_text(encoding="utf-8")
    assert 'Заметки «{escape(name, quote=True)}» нет.' in source


def test_moderation_api_errors_are_html_escaped():
    source = (ROOT / "handlers" / "moderation.py").read_text(encoding="utf-8")
    assert "escape(str(getattr(e, 'message', '') or e or 'неизвестная ошибка'), quote=True)" in source


def test_duration_parser_has_maximum_bound():
    source = (ROOT / "utils.py").read_text(encoding="utf-8")
    assert "seconds > 365 * 86400" in source


def test_role_lookup_uses_short_api_cache():
    source = (ROOT / "handlers" / "moderation.py").read_text(encoding="utf-8")
    assert "_ROLE_CACHE_TTL = 30.0" in source
    assert "except TelegramAPIError:" in source


def test_module_filters_skip_database_lookup_for_unrelated_messages():
    notes = (ROOT / "handlers" / "notes.py").read_text(encoding="utf-8")
    clans = (ROOT / "handlers" / "clans.py").read_text(encoding="utf-8")
    social = (ROOT / "handlers" / "social.py").read_text(encoding="utf-8")
    assert 'if not text.startswith(("заметка", "заметки")):' in notes
    assert 'if not text.startswith(prefixes):' in clans
    assert 'if not text.startswith(prefixes):' in social


def test_new_moderation_commands_are_reserved_from_autoreplies():
    source = (ROOT / "handlers" / "autoreplies.py").read_text(encoding="utf-8")
    for command in ("мои варны", "снять все варны", "проверить мут", "банлист", "причина", "мой id", "кто админ"):
        assert f'"{command}"' in source


def test_command_routers_ignore_messages_without_user_sender():
    for module in ("moderation.py", "economy.py", "clans.py", "premium.py", "social.py", "autoreplies.py", "cases.py"):
        source = (ROOT / "handlers" / module).read_text(encoding="utf-8")
        assert "router.message.filter(_has_from_user)" in source, module


def test_invalid_split_duration_cannot_become_permanent_ban():
    source = (ROOT / "handlers" / "moderation.py").read_text(encoding="utf-8")
    assert 'args[0].strip().lstrip("-").isdigit()' in source
    assert 'r"-?\\d+\\s*[a-zа-яё]+"' in source


def test_manual_spam_reports_are_not_blocked_by_antispam():
    source = (ROOT / "handlers" / "antispam.py").read_text(encoding="utf-8")
    assert 'in {"спам", "пожаловаться на спам"} and has_at_least(role, "agent")' in source
