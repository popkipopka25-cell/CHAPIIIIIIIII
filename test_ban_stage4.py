"""Этап 4: бан, разбан, кик — оформление, сроки, причины."""
import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
from handlers.moderation import (
    format_ban_message, format_unban_message, format_kick_message, _merge_reason, _split_time,
)


def test_ban_permanent_without_reason_does_not_crash():
    assert format_ban_message("Rina", "CHAPADS", None, None) == \
        "🚫 Rina заблокирован в чате навсегда\n🦸 Модератор: CHAPADS"


def test_ban_temporary_with_reason():
    assert format_ban_message("Rina", "CHAPADS", 86400, "Спам") == \
        "🚫 Rina заблокирован в чате на 1 день\n💬 Причина: Спам\n🦸 Модератор: CHAPADS"


def test_ban_reason_escaped():
    assert "&lt;b&gt;" in format_ban_message("R", "M", None, "<b>")


def test_ban_term_parsing():
    assert _split_time(["3д", "реклама"]) == (3 * 86400, "реклама")
    assert _split_time(["реклама"]) == (None, "реклама")
    assert _split_time(["999д"])[0] == -1


def test_reason_next_line_merged_and_trimmed():
    assert _merge_reason("флуд", "и оскорбления") == "флуд и оскорбления"
    assert _merge_reason(None, None) is None
    assert len(_merge_reason("x" * 600, None)) == 501


def test_unban_and_kick_format():
    assert format_unban_message("Rina") == "✅ Rina разблокирован и может снова вступить в чат."
    assert format_kick_message("Rina", "M", None) == "👢 Rina исключён из чата\n🦸 Модератор: M"
