"""Общие обработчики (без фильтров/только F.text) должны стоять последними в своём роутере,
иначе команды, объявленные ниже, недостижимы."""
from handlers import antispam, autoreplies, admin_panel


def test_antispam_catch_all_is_last():
    assert antispam.router.message.handlers[-1].callback is antispam.antispam_check


def test_spam_command_reachable_before_catch_all():
    names = [h.callback.__name__ for h in antispam.router.message.handlers]
    assert names.index("antispam_check") == len(names) - 1
    assert len(names) >= 2


def test_other_catch_alls_are_last():
    assert autoreplies.router.message.handlers[-1].callback is autoreplies.match_autoreply
    assert admin_panel.router.message.handlers[-1].callback is admin_panel.owner_agent_pending_input
