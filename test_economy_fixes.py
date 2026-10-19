"""Тесты исправлений этапа 2: типы операций, лимиты, миграции, конкурентность."""
import asyncio
import aiosqlite
import pytest

import config
import database as db
import economy
from config import START_BALANCE
from tests.conftest import CHAT, USER_A, USER_B

pytestmark = pytest.mark.asyncio


async def test_admin_take_shown_as_debit_not_incoming(tmp_db):
    await economy.admin_change_balance(CHAT, 999, USER_A, -100)
    hist = await economy.get_history(CHAT, USER_A)
    assert hist[0]["kind"] == "admin_take"
    assert hist[0]["actor_id"] == 999
    line = economy.describe_tx(hist[0], USER_A)
    assert "−100" in line and "списание" in line


async def test_history_kinds(tmp_db):
    await economy.claim_daily(CHAT, USER_A)
    await economy.transfer(CHAT, USER_A, USER_B, 10)
    await economy.admin_change_balance(CHAT, 999, USER_A, 5)
    items = await economy.list_shop(CHAT)
    await economy.buy_item(CHAT, USER_A, items[0]["name"])
    kinds = [r["kind"] for r in await economy.get_history(CHAT, USER_A, 10)]
    assert kinds == ["purchase", "admin_give", "transfer", "daily"]
    b_line = economy.describe_tx((await economy.get_history(CHAT, USER_B))[0], USER_B)
    assert "+10" in b_line and "←" in b_line


async def test_amount_limits(tmp_db):
    ok, _ = await economy.transfer(CHAT, USER_A, USER_B, economy.MAX_AMOUNT + 1)
    assert not ok
    ok, _ = await economy.admin_change_balance(CHAT, 1, USER_A, economy.MAX_AMOUNT + 1)
    assert not ok
    ok, _ = await economy.add_shop_item(CHAT, "x", economy.MAX_AMOUNT + 1)
    assert not ok
    ok, _ = await economy.add_shop_item(CHAT, "   ", 10)
    assert not ok


async def test_buy_strips_and_lowercases(tmp_db):
    await economy.add_shop_item(CHAT, "Шапка", 10)
    ok, _ = await economy.buy_item(CHAT, USER_A, "  ШАПКА ")
    assert ok
    inv = await economy.get_inventory(CHAT, USER_A)
    assert inv[0]["item"] == "шапка"


async def test_buy_multiword_item(tmp_db):
    await economy.add_shop_item(CHAT, "Золотая корона", 25)
    ok, _ = await economy.buy_item(CHAT, USER_A, "  ЗОЛОТАЯ КОРОНА  ")
    assert ok
    inv = await economy.get_inventory(CHAT, USER_A)
    assert any(row["item"] == "золотая корона" and row["qty"] == 1 for row in inv)


async def test_gift_item_moves_one_unit_atomically(tmp_db):
    await economy.add_shop_item(CHAT, "Кофе", 10)
    await economy.buy_item(CHAT, USER_A, "кофе")
    ok, _ = await economy.gift_item(CHAT, USER_A, USER_B, "КОФЕ")
    assert ok
    inv_a = await economy.get_inventory(CHAT, USER_A)
    inv_b = await economy.get_inventory(CHAT, USER_B)
    assert not any(row["item"] == "кофе" for row in inv_a)
    assert any(row["item"] == "кофе" and row["qty"] == 1 for row in inv_b)


async def test_daily_tasks_reward_once(tmp_db):
    await db.ensure_member(CHAT, USER_A)
    await db.bump_task_progress(CHAT, USER_A, "messages", 10)
    await db.bump_task_progress(CHAT, USER_A, "transfer", 1)
    tasks = await economy.get_daily_tasks(CHAT, USER_A)
    assert next(task for task in tasks if task["key"] == "messages")["progress"] == 10
    amount, completed = await economy.claim_daily_tasks(CHAT, USER_A)
    assert amount == 125
    assert set(completed) == {"messages", "transfer"}
    again, _ = await economy.claim_daily_tasks(CHAT, USER_A)
    assert again == 0


async def test_concurrent_transfers_no_overdraft(tmp_db):
    amount = START_BALANCE // 2 + 1  # только один из двух переводов возможен
    res = await asyncio.gather(
        economy.transfer(CHAT, USER_A, USER_B, amount),
        economy.transfer(CHAT, USER_A, 333, amount),
    )
    assert sum(1 for ok, _ in res if ok) == 1
    assert await economy.get_balance(CHAT, USER_A) == START_BALANCE - amount


async def test_concurrent_daily_once(tmp_db):
    res = await asyncio.gather(*[economy.claim_daily(CHAT, USER_A) for _ in range(5)])
    assert sum(1 for r, _ in res if r) == 1


async def test_regular_db_commit_cannot_finish_economy_transaction(tmp_db):
    """Обычная запись должна дождаться транзакции, а не закоммитить её раньше."""
    await db.ensure_member(CHAT, USER_A)
    started = asyncio.Event()
    release = asyncio.Event()

    async def controlled_transaction():
        conn = await db.get_transaction_db()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(
                "UPDATE members SET balance=balance+1 WHERE chat_id=? AND user_id=?",
                (CHAT, USER_A),
            )
            started.set()
            await release.wait()
            await conn.commit()
        finally:
            await conn.close()

    task = asyncio.create_task(controlled_transaction())
    await started.wait()
    log_task = asyncio.create_task(db.log_action(CHAT, 999, "test_commit_isolation", USER_A))
    try:
        await asyncio.sleep(0.05)
        assert not log_task.done()
    finally:
        release.set()
    await asyncio.gather(task, log_task)
    assert await economy.get_balance(CHAT, USER_A) == START_BALANCE + 1


async def test_migration_upgrades_old_db_without_data_loss(tmp_path, monkeypatch):
    """Старая база (без столбца kind) обновляется, данные сохраняются."""
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as c:
        await c.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, from_id INTEGER, to_id INTEGER, amount INTEGER, created_at REAL)")
        await c.execute("INSERT INTO transactions (chat_id, from_id, to_id, amount, created_at) VALUES (?,?,?,?,?)", (CHAT, 0, USER_A, 77, 1.0))
        await c.commit()
    await db.close_db()
    monkeypatch.setattr(config, "DB_PATH", path)
    try:
        hist = await economy.get_history(CHAT, USER_A)
        assert hist[0]["amount"] == 77 and hist[0]["kind"] == "legacy"
        assert "+77" in economy.describe_tx(hist[0], USER_A)
        conn = await db.get_db()
        cur = await conn.execute("SELECT MAX(version) FROM schema_version")
        assert (await cur.fetchone())[0] >= 1
    finally:
        await db.close_db()


async def test_premium_expiry_extends_and_custom_status_is_scoped(tmp_db):
    first_expiry = await db.grant_premium(CHAT, USER_A, 3600, 999)
    assert await db.premium_expiry(CHAT, USER_A) is not None
    assert await db.set_premium_profile_status(CHAT, USER_A, "Люблю космос")
    assert await db.premium_profile_status(CHAT, USER_A) == "Люблю космос"
    second_expiry = await db.grant_premium(CHAT, USER_A, 3600, 999)
    assert second_expiry >= first_expiry + 3599
    assert await db.premium_profile_status(CHAT, USER_A) == "Люблю космос"
    assert await db.premium_expiry(CHAT + 1, USER_A) is None
    assert await db.remove_premium(CHAT, USER_A)
    assert await db.premium_expiry(CHAT, USER_A) is None
