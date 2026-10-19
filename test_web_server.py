"""Проверка HTTP-маршрутов для Render и главной страницы панели."""
import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
from aiohttp.test_utils import TestClient, TestServer
import main


async def test_health_and_root():
    async with TestClient(TestServer(main.build_web_app())) as client:
        r = await client.get("/health")
        assert r.status == 200 and (await r.json())["ok"] is True
        r = await client.get("/")
        assert r.status == 200
        assert "Вход владельца" in await r.text()
        r = await client.get("/status")
        assert r.status == 200 and "status" in await r.json()
        r = await client.get("/panel")
        assert r.status == 200 and "Чапи" in await r.text()
