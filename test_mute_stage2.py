"""Этап 2: мут/размут — сроки, причины, оформление."""
import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
from utils import human_duration, parse_duration
from handlers.moderation import _split_time, split_command, format_mute_message, format_unmute_message


def test_human_duration():
    assert human_duration(3600) == "1 час"
    assert human_duration(600) == "10 минут"
    assert human_duration(7200) == "2 часа"
    assert human_duration(86400) == "1 день"
    assert human_duration(5 * 86400) == "5 дней"
    assert human_duration(60) == "1 минуту"


def test_split_time_variants():
    assert _split_time(["10", "минут", "Плохо", "себя", "вёл"]) == (600, "Плохо себя вёл")
    assert _split_time(["2ч"]) == (7200, None)
    assert _split_time(["1", "день"]) == (86400, None)
    assert _split_time([]) == (None, None)
    assert _split_time(["0м"])[0] == -1
    assert _split_time(["-5м"])[0] == -1
    assert _split_time(["999д"])[0] == -1
    assert _split_time(["1", "месяц"])[0] == -1


def test_reason_on_next_line():
    words, extra = split_command("мут 10м\nФлуд в чате")
    assert words == ["мут", "10м"] and extra == "Флуд в чате"


def test_mute_format_with_reason():
    text = format_mute_message("Rina", "CHAPADS", 3600, "Плохая")
    assert text == "🔇 Rina лишается права слова на 1 час\n💬 Причина: Плохая\n🦸 Модератор: CHAPADS"


def test_mute_format_without_reason():
    text = format_mute_message("Rina", "CHAPADS", 3600, None)
    assert text == "🔇 Rina лишается права слова на 1 час\n👺 Модератор: CHAPADS"
    assert "Причина" not in text


def test_reason_is_escaped():
    assert "&lt;b&gt;" in format_mute_message("R", "M", 60, "<b>")


def test_unmute_format():
    assert format_unmute_message("Rina").startswith("✅ Пользователю Rina вернули право слова.")


def test_parse_duration_full_words():
    assert parse_duration("2 часа") == 7200
