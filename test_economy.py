"""Тесты экономики: баланс, бонус, переводы, магазин, инвентарь."""
import asyncio
import time

import economy
from config import DAILY_REWARD, START_BALANCE
from tests.conftest import CHAT, USER_A, USER_B


async def test_new_member_gets_start_balance(tmp_db):
    balance = await economy.get_balance(CHAT, USER_A)
    assert balance == START_BALANCE


async def test_daily_bonus(tmp_db):
    reward, wait = await economy.claim_daily(CHAT, USER_A)
    assert reward == DAILY_REWARD
    assert wait is None
    assert await economy.get_balance(CHAT, USER_A) == START_BALANCE + DAILY_REWARD


async def test_daily_bonus_cooldown_24h(tmp_db):
    await economy.claim_daily(CHAT, USER_A)
    reward, wait = await economy.claim_daily(CHAT, USER_A)
    assert reward is None
    assert 0 < wait <= 24 * 3600
    # баланс не изменился
    assert await economy.get_balance(CHAT, USER_A) == START_BALANCE + DAILY_REWARD


async def test_daily_bonus_no_double_on_concurrent(tmp_db):
    """Два одновременных запроса бонуса не дают двойную награду."""
    results = await asyncio.gather(
        economy.claim_daily(CHAT, USER_A),
        economy.claim_daily(CHAT, USER_A),
    )
    rewards = [r for r, _ in results if r is not None]
    assert len(rewards) == 1
    assert await economy.get_balance(CHAT, USER_A) == START_BALANCE + DAILY_REWARD


async def test_transfer_ok(tmp_db):
    ok, err = await economy.transfer(CHAT, USER_A, USER_B, 100)
    assert ok and err is None
    assert await economy.get_balance(CHAT, USER_A) == START_BALANCE - 100
    assert await economy.get_balance(CHAT, USER_B) == START_BALANCE + 100


async def test_transfer_insufficient_funds(tmp_db):
    ok, err = await economy.transfer(CHAT, USER_A, USER_B, START_BALANCE + 1)
    assert not ok
    assert "Недостаточно" in err
    assert await economy.get_balance(CHAT, USER_A) == START_BALANCE
    assert await economy.get_balance(CHAT, USER_B) == START_BALANCE


async def test_transfer_negative_and_zero(tmp_db):
    for bad in (0, -50):
        ok, err = await economy.transfer(CHAT, USER_A, USER_B, bad)
        assert not ok
        assert "положительной" in err


async def test_transfer_to_self(tmp_db):
    ok, err = await economy.transfer(CHAT, USER_A, USER_A, 10)
    assert not ok
    assert "самому себе" in err


async def test_transfer_no_negative_balance(tmp_db):
    await economy.transfer(CHAT, USER_A, USER_B, START_BALANCE)
    ok, _ = await economy.transfer(CHAT, USER_A, USER_B, 1)
    assert not ok
    assert await economy.get_balance(CHAT, USER_A) == 0


async def test_top_richest_order(tmp_db):
    await economy.transfer(CHAT, USER_A, USER_B, 500)
    top = await economy.top_richest(CHAT, 10)
    assert top[0]["user_id"] == USER_B
    assert top[0]["balance"] == START_BALANCE + 500


async def test_history_records_operations(tmp_db):
    await economy.claim_daily(CHAT, USER_A)
    await economy.transfer(CHAT, USER_A, USER_B, 50)
    history = await economy.get_history(CHAT, USER_A)
    assert len(history) == 2


async def test_shop_seeded_on_first_open(tmp_db):
    items = await economy.list_shop(CHAT)
    assert len(items) >= 3


async def test_buy_item_and_inventory(tmp_db):
    items = await economy.list_shop(CHAT)
    item = items[0]
    ok, _ = await economy.buy_item(CHAT, USER_A, item["name"])
    assert ok
    assert await economy.get_balance(CHAT, USER_A) == START_BALANCE - item["price"]
    inv = await economy.get_inventory(CHAT, USER_A)
    assert inv[0]["item"] == item["name"] and inv[0]["qty"] == 1
    # повторная покупка увеличивает количество
    await economy.buy_item(CHAT, USER_A, item["name"])
    inv = await economy.get_inventory(CHAT, USER_A)
    assert inv[0]["qty"] == 2


async def test_buy_unknown_item(tmp_db):
    await economy.list_shop(CHAT)
    ok, msg = await economy.buy_item(CHAT, USER_A, "несуществующий")
    assert not ok
    assert "нет в магазине" in msg


async def test_buy_insufficient_funds(tmp_db):
    await economy.add_shop_item(CHAT, "дорогой", START_BALANCE + 100)
    ok, _ = await economy.buy_item(CHAT, USER_A, "дорогой")
    assert not ok
    assert await economy.get_balance(CHAT, USER_A) == START_BALANCE


async def test_admin_give_and_take(tmp_db):
    ok, _ = await economy.admin_change_balance(CHAT, 999, USER_A, 250)
    assert ok
    assert await economy.get_balance(CHAT, USER_A) == START_BALANCE + 250
    ok, _ = await economy.admin_change_balance(CHAT, 999, USER_A, -100)
    assert ok
    assert await economy.get_balance(CHAT, USER_A) == START_BALANCE + 150


async def test_admin_take_cannot_go_negative(tmp_db):
    ok, msg = await economy.admin_change_balance(CHAT, 999, USER_A, -(START_BALANCE + 1))
    assert not ok
    assert "минус" in msg


async def test_balance_persists_after_reconnect(tmp_db, monkeypatch):
    """Данные сохраняются после «перезапуска» (закрытия и открытия базы)."""
    await economy.transfer(CHAT, USER_A, USER_B, 300)
    import database as db
    import config
    path = config.DB_PATH
    await db.close_db()
    monkeypatch.setattr(config, "DB_PATH", path)
    assert await economy.get_balance(CHAT, USER_B) == START_BALANCE + 300


async def test_daily_cooldown_survives_time(tmp_db, monkeypatch):
    """Бонус снова доступен после 24 часов."""
    await economy.claim_daily(CHAT, USER_A)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 24 * 3600 + 1)
    reward, wait = await economy.claim_daily(CHAT, USER_A)
    assert reward == DAILY_REWARD
