"""Экономика «Чапи»: чапы, ежедневный бонус, переводы, топ, магазин, инвентарь.

Все денежные операции выполняются в транзакциях BEGIN IMMEDIATE,
чтобы одновременные запросы не могли создать двойное списание/зачисление.
"""
import time
from html import escape

import database as db
from config import DAILY_REWARD

DAILY_COOLDOWN = 24 * 3600  # бонус не чаще раза в 24 часа
# Верхний предел одной операции: защита от переполнения INTEGER в SQLite
MAX_AMOUNT = 10**12

TX_SQL = ("INSERT INTO transactions (chat_id, from_id, to_id, amount, created_at, kind, actor_id, note) "
          "VALUES (?,?,?,?,?,?,?,?)")

# Демонстрационные товары, создаются при первом открытии пустого магазина
DEMO_ITEMS = [
    ("значок", 200, "Красивый значок в инвентарь"),
    ("кофе", 150, "Виртуальная чашка кофе"),
    ("рамка", 500, "Рамка для профиля"),
    ("корона", 5000, "Показать всем, кто тут главный"),
]


async def get_balance(chat_id: int, user_id: int) -> int:
    await db.ensure_member(chat_id, user_id)
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT balance FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
    )
    return (await cur.fetchone())["balance"]


async def claim_daily(chat_id: int, user_id: int) -> tuple[int | None, int | None]:
    """Ежедневный бонус. Возвращает (награда, None) или (None, секунд_осталось)."""
    await db.ensure_member(chat_id, user_id)
    now = time.time()
    async with db.tx_lock:
        conn = await db.get_transaction_db()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT last_daily FROM daily WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
            )
            row = await cur.fetchone()
            if row and now - row["last_daily"] < DAILY_COOLDOWN:
                await conn.rollback()
                return None, int(DAILY_COOLDOWN - (now - row["last_daily"]))
            await conn.execute(
                "INSERT INTO daily (chat_id, user_id, last_daily) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET last_daily = excluded.last_daily",
                (chat_id, user_id, now),
            )
            await conn.execute(
                "UPDATE members SET balance = balance + ? WHERE chat_id = ? AND user_id = ?",
                (DAILY_REWARD, chat_id, user_id),
            )
            await conn.execute(
                TX_SQL, (chat_id, 0, user_id, DAILY_REWARD, now, "daily", user_id, None),
            )
            await conn.commit()
            return DAILY_REWARD, None
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()


async def transfer(chat_id: int, from_id: int, to_id: int, amount: int) -> tuple[bool, str | None]:
    """Перевод чапов. Возвращает (успех, текст_ошибки)."""
    if not isinstance(amount, int) or amount <= 0:
        return False, "Сумма должна быть положительной."
    if amount > MAX_AMOUNT:
        return False, f"Слишком большая сумма (максимум {MAX_AMOUNT})."
    if from_id == to_id:
        return False, "Нельзя переводить чапы самому себе."
    await db.ensure_member(chat_id, from_id)
    await db.ensure_member(chat_id, to_id)
    async with db.tx_lock:
        conn = await db.get_transaction_db()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT balance FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, from_id)
            )
            balance = (await cur.fetchone())["balance"]
            if balance < amount:
                await conn.rollback()
                return False, f"Недостаточно чапов: на балансе {balance}."
            await conn.execute(
                "UPDATE members SET balance = balance - ? WHERE chat_id = ? AND user_id = ?",
                (amount, chat_id, from_id),
            )
            await conn.execute(
                "UPDATE members SET balance = balance + ? WHERE chat_id = ? AND user_id = ?",
                (amount, chat_id, to_id),
            )
            await conn.execute(
                TX_SQL, (chat_id, from_id, to_id, amount, time.time(), "transfer", from_id, None),
            )
            from datetime import datetime, timezone
            task_date = datetime.now(timezone.utc).date().isoformat()
            await conn.execute(
                "INSERT INTO task_progress(chat_id,user_id,task_date,task_key,progress,claimed) VALUES(?,?,?,'transfer',1,0) "
                "ON CONFLICT(chat_id,user_id,task_date,task_key) DO UPDATE SET progress=MIN(progress+1,1000000)",
                (chat_id, from_id, task_date),
            )
            await conn.commit()
            return True, None
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()


async def top_richest(chat_id: int, limit: int = 10):
    conn = await db.get_db()
    cur = await conn.execute(
        """SELECT m.user_id, m.balance, u.first_name, u.username
           FROM members m LEFT JOIN users u ON u.user_id = m.user_id
           WHERE m.chat_id = ? ORDER BY m.balance DESC, m.user_id LIMIT ?""",
        (chat_id, limit),
    )
    return await cur.fetchall()


async def get_history(chat_id: int, user_id: int, limit: int = 10):
    conn = await db.get_db()
    cur = await conn.execute(
        """SELECT * FROM transactions
           WHERE chat_id = ? AND (from_id = ? OR to_id = ?)
           ORDER BY id DESC LIMIT ?""",
        (chat_id, user_id, user_id, limit),
    )
    return await cur.fetchall()


TASKS = {
    "messages": ("Написать 10 сообщений", 10, 50),
    "transfer": ("Сделать 1 перевод", 1, 75),
    "purchase": ("Купить 1 предмет в магазине", 1, 100),
}


async def get_daily_tasks(chat_id: int, user_id: int):
    from datetime import datetime, timezone
    await db.ensure_member(chat_id, user_id)
    task_date = datetime.now(timezone.utc).date().isoformat()
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT task_key,progress,claimed FROM task_progress WHERE chat_id=? AND user_id=? AND task_date=?",
        (chat_id, user_id, task_date),
    )
    rows = {row["task_key"]: row for row in await cur.fetchall()}
    result = []
    for key, (title, goal, reward) in TASKS.items():
        row = rows.get(key)
        progress = min(goal, int(row["progress"])) if row else 0
        claimed = bool(row["claimed"]) if row else False
        result.append({"key": key, "title": title, "goal": goal, "reward": reward, "progress": progress, "claimed": claimed})
    return result


async def claim_daily_tasks(chat_id: int, user_id: int) -> tuple[int, list[str]]:
    """Забирает награды за все выполненные, ещё не полученные задания."""
    from datetime import datetime, timezone
    await db.ensure_member(chat_id, user_id)
    task_date = datetime.now(timezone.utc).date().isoformat()
    async with db.tx_lock:
        conn = await db.get_transaction_db()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT task_key,progress FROM task_progress WHERE chat_id=? AND user_id=? AND task_date=? AND claimed=0",
                (chat_id, user_id, task_date),
            )
            rows = await cur.fetchall()
            completed = []
            reward_total = 0
            for row in rows:
                info = TASKS.get(row["task_key"])
                if info and int(row["progress"]) >= info[1]:
                    completed.append(row["task_key"])
                    reward_total += info[2]
            if not completed:
                await conn.rollback()
                return 0, []
            for key in completed:
                await conn.execute(
                    "UPDATE task_progress SET claimed=1 WHERE chat_id=? AND user_id=? AND task_date=? AND task_key=? AND claimed=0",
                    (chat_id, user_id, task_date, key),
                )
            await conn.execute(
                "UPDATE members SET balance=balance+? WHERE chat_id=? AND user_id=?",
                (reward_total, chat_id, user_id),
            )
            await conn.execute(
                TX_SQL, (chat_id, 0, user_id, reward_total, time.time(), "task_reward", user_id, ",".join(completed)),
            )
            await conn.commit()
            return reward_total, completed
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()


# ---------- магазин ----------

async def ensure_demo_shop(chat_id: int):
    """Если магазин группы пуст — заполняем демонстрационными товарами."""
    conn = await db.get_db()
    cur = await conn.execute("SELECT COUNT(*) AS c FROM shop_items WHERE chat_id = ?", (chat_id,))
    if (await cur.fetchone())["c"] > 0:
        return
    for name, price, desc in DEMO_ITEMS:
        await conn.execute(
            "INSERT OR IGNORE INTO shop_items (chat_id, name, price, description) VALUES (?,?,?,?)",
            (chat_id, name, price, desc),
        )
    await conn.commit()


async def list_shop(chat_id: int):
    await ensure_demo_shop(chat_id)
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT name, price, description FROM shop_items WHERE chat_id = ? ORDER BY price LIMIT 25",
        (chat_id,),
    )
    return await cur.fetchall()


async def buy_item(chat_id: int, user_id: int, item_name: str) -> tuple[bool, str]:
    """Покупка товара. Возвращает (успех, сообщение)."""
    item_name = item_name.strip().lower()
    if not item_name:
        return False, "Укажите название товара."
    await db.ensure_member(chat_id, user_id)
    async with db.tx_lock:
        conn = await db.get_transaction_db()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT price FROM shop_items WHERE chat_id = ? AND name = ?",
                (chat_id, item_name.lower()),
            )
            item = await cur.fetchone()
            if not item:
                await conn.rollback()
                return False, f"Товара «{item_name}» нет в магазине."
            cur = await conn.execute(
                "SELECT balance FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
            )
            balance = (await cur.fetchone())["balance"]
            if balance < item["price"]:
                await conn.rollback()
                return False, f"Не хватает чапов: нужно {item['price']}, на балансе {balance}."
            await conn.execute(
                "UPDATE members SET balance = balance - ? WHERE chat_id = ? AND user_id = ?",
                (item["price"], chat_id, user_id),
            )
            await conn.execute(
                "INSERT INTO inventory (chat_id, user_id, item, qty) VALUES (?,?,?,1) "
                "ON CONFLICT(chat_id, user_id, item) DO UPDATE SET qty = qty + 1",
                (chat_id, user_id, item_name.lower()),
            )
            await conn.execute(
                TX_SQL, (chat_id, user_id, 0, item["price"], time.time(), "purchase", user_id, item_name),
            )
            from datetime import datetime, timezone
            task_date = datetime.now(timezone.utc).date().isoformat()
            await conn.execute(
                "INSERT INTO task_progress(chat_id,user_id,task_date,task_key,progress,claimed) VALUES(?,?,?,'purchase',1,0) "
                "ON CONFLICT(chat_id,user_id,task_date,task_key) DO UPDATE SET progress=MIN(progress+1,1000000)",
                (chat_id, user_id, task_date),
            )
            await conn.commit()
            return True, f"Вы купили «{item_name}» за {item['price']} чапов."
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()


async def get_inventory(chat_id: int, user_id: int):
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT item, qty FROM inventory WHERE chat_id = ? AND user_id = ? ORDER BY item",
        (chat_id, user_id),
    )
    return await cur.fetchall()


async def gift_item(chat_id: int, from_id: int, to_id: int, item_name: str) -> tuple[bool, str]:
    """Передаёт одну единицу предмета между инвентарями атомарно."""
    item_name = item_name.strip().lower()
    if not item_name:
        return False, "Укажите название предмета."
    if from_id == to_id:
        return False, "Нельзя подарить предмет самому себе."
    await db.ensure_member(chat_id, from_id)
    await db.ensure_member(chat_id, to_id)
    async with db.tx_lock:
        conn = await db.get_transaction_db()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT qty FROM inventory WHERE chat_id=? AND user_id=? AND item=?",
                (chat_id, from_id, item_name),
            )
            row = await cur.fetchone()
            if not row or int(row["qty"]) <= 0:
                await conn.rollback()
                return False, f"У вас нет предмета «{item_name}»."
            if int(row["qty"]) == 1:
                await conn.execute(
                    "DELETE FROM inventory WHERE chat_id=? AND user_id=? AND item=?",
                    (chat_id, from_id, item_name),
                )
            else:
                await conn.execute(
                    "UPDATE inventory SET qty=qty-1 WHERE chat_id=? AND user_id=? AND item=?",
                    (chat_id, from_id, item_name),
                )
            await conn.execute(
                "INSERT INTO inventory(chat_id,user_id,item,qty) VALUES(?,?,?,1) "
                "ON CONFLICT(chat_id,user_id,item) DO UPDATE SET qty=qty+1",
                (chat_id, to_id, item_name),
            )
            await conn.commit()
            return True, f"Предмет «{item_name}» подарен."
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()


async def add_shop_item(chat_id: int, name: str, price: int, description: str = "") -> tuple[bool, str]:
    name = name.strip().lower()
    if not name or len(name) > 32:
        return False, "Название товара: от 1 до 32 символов."
    if len(description) > 300:
        return False, "Описание товара не должно превышать 300 символов."
    if price <= 0 or price > MAX_AMOUNT:
        return False, "Цена должна быть положительной и не больше лимита."
    conn = await db.get_db()
    await conn.execute(
        "INSERT INTO shop_items (chat_id, name, price, description) VALUES (?,?,?,?) "
        "ON CONFLICT(chat_id, name) DO UPDATE SET price = excluded.price, description = excluded.description",
        (chat_id, name.lower(), price, description),
    )
    await conn.commit()
    return True, f"Товар «{name.lower()}» ({price} чапов) сохранён."


async def remove_shop_item(chat_id: int, name: str) -> bool:
    conn = await db.get_db()
    cur = await conn.execute(
        "DELETE FROM shop_items WHERE chat_id = ? AND name = ?", (chat_id, name.lower())
    )
    await conn.commit()
    return cur.rowcount > 0


# ---------- административные операции ----------

async def admin_change_balance(chat_id: int, actor_id: int, target_id: int, amount: int) -> tuple[bool, str]:
    """Выдача (amount > 0) или списание (amount < 0) чапов администратором."""
    if amount == 0:
        return False, "Сумма не может быть нулевой."
    if abs(amount) > MAX_AMOUNT:
        return False, f"Слишком большая сумма (максимум {MAX_AMOUNT})."
    await db.ensure_member(chat_id, target_id)
    async with db.tx_lock:
        conn = await db.get_transaction_db()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT balance FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, target_id)
            )
            balance = (await cur.fetchone())["balance"]
            if balance + amount < 0:
                await conn.rollback()
                return False, f"Нельзя увести баланс в минус: у пользователя {balance} чапов."
            await conn.execute(
                "UPDATE members SET balance = balance + ? WHERE chat_id = ? AND user_id = ?",
                (amount, chat_id, target_id),
            )
            await conn.execute(
                TX_SQL,
                (chat_id, 0 if amount > 0 else target_id, target_id if amount > 0 else 0, abs(amount),
                 time.time(), "admin_give" if amount > 0 else "admin_take", actor_id, None),
            )
            await conn.commit()
            return True, f"Баланс изменён на {amount:+d}. Теперь: {balance + amount} чапов."
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()


KIND_LABELS = {
    "daily": "ежедневный бонус",
    "transfer": "перевод",
    "purchase": "покупка",
    "admin_give": "выдача администратором",
    "admin_take": "списание администратором",
    "task_reward": "награда за задания",
}


def describe_tx(r, uid: int) -> str:
    """Человекочитаемая строка операции для истории пользователя uid."""
    from datetime import datetime
    date = datetime.fromtimestamp(r["created_at"]).strftime("%d.%m %H:%M")
    keys = r.keys()
    kind = r["kind"] if "kind" in keys and r["kind"] else "legacy"
    incoming = r["to_id"] == uid
    sign = "+" if incoming else "−"
    if kind == "transfer":
        other = r["from_id"] if incoming else r["to_id"]
        arrow = "←" if incoming else "→"
        return f"{date} — {sign}{r['amount']} {arrow} {other} (перевод)"
    if kind == "purchase":
        note = r["note"] if "note" in keys and r["note"] else ""
        return f"{date} — −{r['amount']} (покупка {escape(str(note), quote=True)})".replace(" )", ")")
    if kind in KIND_LABELS:
        return f"{date} — {sign}{r['amount']} ({KIND_LABELS[kind]})"
    # старые записи без типа
    if r["from_id"] == 0:
        return f"{date} — +{r['amount']} (бонус/выдача)"
    if r["to_id"] == 0:
        return f"{date} — −{r['amount']} (покупка)"
    return f"{date} — {sign}{r['amount']}"
