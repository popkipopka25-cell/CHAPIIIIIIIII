"""Этап 5: владелец, штат, спам-блок, глобальные модули."""
import pytest
import config
import staff_core as sc

OWNER = 999


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setattr(config, "OWNER_TELEGRAM_ID", OWNER)
    sc._hits.clear()


@pytest.mark.asyncio
async def test_owner_only_by_id(tmp_db):
    assert sc.is_owner(OWNER) and not sc.is_owner(1)
    assert await sc.get_staff_role(OWNER) == "owner"


@pytest.mark.asyncio
async def test_hire_fire_and_protections(tmp_db):
    ok, _ = await sc.assign_role(OWNER, 10, "admin"); assert ok
    ok, _ = await sc.assign_role(OWNER, 10, "admin"); assert not ok  # дубликат
    ok, _ = await sc.assign_role(10, 20, "admin"); assert not ok      # админ не назначает админов
    ok, _ = await sc.assign_role(10, 20, "agent"); assert ok
    ok, _ = await sc.assign_role(20, 30, "agent"); assert not ok      # агент не нанимает
    ok, _ = await sc.remove_role(10, OWNER, "admin"); assert not ok   # владельца не уволить
    ok, _ = await sc.assign_role(OWNER, OWNER, "agent"); assert not ok
    ok, _ = await sc.remove_role(OWNER, 20, "agent"); assert ok
    assert await sc.get_staff_role(20) is None
    assert len(await sc.get_staff_log()) >= 3


@pytest.mark.asyncio
async def test_spamblock_global_and_removal(tmp_db):
    await sc.assign_role(OWNER, 20, "agent")
    ok, _ = await sc.spamblock_add(55, "реклама", "staff", 20, -100, 7); assert ok
    assert await sc.is_spamblocked(55)
    rec = await sc.spamblock_get(55); assert rec["added_by"] == 20 and rec["chat_id"] == -100
    ok, _ = await sc.spamblock_remove(77, 55); assert not ok          # посторонний
    ok, _ = await sc.spamblock_remove(20, 55); assert ok
    assert not await sc.is_spamblocked(55)
    ok, _ = await sc.spamblock_add(20, "x", "staff", OWNER); assert not ok  # сотрудника нельзя


@pytest.mark.asyncio
async def test_auto_block_after_three_hits(tmp_db):
    assert not await sc.register_violation(66, "флуд", -1, 1, now=100)
    assert not await sc.register_violation(66, "флуд", -1, 2, now=110)
    assert await sc.register_violation(66, "флуд", -1, 3, now=120)
    assert (await sc.spamblock_get(66))["source"] == "auto"


@pytest.mark.asyncio
async def test_modules_owner_only(tmp_db):
    assert sc.resolve_module("экономика") == "economy"
    ok, _ = await sc.set_module(10, "economy", False); assert not ok
    ok, _ = await sc.set_module(OWNER, "economy", False); assert ok
    assert not await sc.module_enabled("economy")
    await sc.set_module(OWNER, "economy", True)
    assert await sc.module_enabled("economy")
