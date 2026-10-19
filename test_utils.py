from utils import can_act_on, format_duration, has_at_least, mention, parse_duration


def test_parse_duration_russian_units_and_bounds():
    assert parse_duration('10м') == 600
    assert parse_duration('2д') == 172800
    assert parse_duration('10 минут') == 600
    assert parse_duration('2 часа') == 7200
    assert parse_duration('1 день') == 86400
    assert parse_duration('0м') is None
    assert parse_duration('366д') is None
    assert parse_duration('10 months') is None


def test_role_hierarchy_is_strict_for_actions():
    assert has_at_least('admin', 'moderator')
    assert not has_at_least('agent', 'moderator')
    assert can_act_on('admin', 'moderator')
    assert not can_act_on('moderator', 'moderator')
    assert not can_act_on('user', 'owner')


def test_mention_escapes_untrusted_display_name():
    result = mention(12345, '<b>bad & name</b>')
    assert result == '<a href="tg://user?id=12345">&lt;b&gt;bad &amp; name&lt;/b&gt;</a>'


def test_duration_formatting():
    assert format_duration(3600) == '1 ч.'
    assert format_duration(120) == '2 мин.'
    assert format_duration(17) == '17 сек.'
