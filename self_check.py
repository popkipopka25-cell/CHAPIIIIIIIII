"""Offline smoke-check for source syntax, command regexes and SQLite schema.

Run with: python tools/self_check.py
This does not replace pytest or a real Telegram API integration test.
"""
from __future__ import annotations

import ast
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    python_files = sorted(
        path for path in ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and ".venv" not in path.parts
    )
    regex_count = 0
    for path in python_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "regexp"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                re.compile(node.args[0].value)
                regex_count += 1

    db_tree = ast.parse((ROOT / "database.py").read_text(encoding="utf-8"))
    schema_node = next(
        node.value for node in db_tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "SCHEMA" for target in node.targets)
    )
    schema = ast.literal_eval(schema_node)
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)

    migration_tree = ast.parse((ROOT / "migrations" / "__init__.py").read_text(encoding="utf-8"))
    migration_functions = [
        node for node in migration_tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("m00")
    ]
    for function in migration_functions:
        for node in ast.walk(function):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                continue
            sql = node.args[0].value.strip()
            if sql.upper().startswith("ALTER TABLE"):
                match = re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", sql, re.IGNORECASE)
                if match:
                    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({match.group(1)})")}
                    if match.group(2) in columns:
                        continue
            conn.execute(sql)

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"users", "members", "punishments", "transactions", "autoreplies", "task_progress", "clans", "marriage_history", "marriage_restore_proposals", "premium_memberships"}
    missing = required - tables
    if missing:
        raise RuntimeError(f"SQLite schema is missing tables: {', '.join(sorted(missing))}")
    premium_columns = {row[1] for row in conn.execute("PRAGMA table_info(premium_memberships)")}
    if "status_text" not in premium_columns:
        raise RuntimeError("SQLite schema is missing premium profile status column")

    from utils import mention, parse_duration
    assert parse_duration("10 минут") == 600
    assert parse_duration("2 часа") == 7200
    assert parse_duration("1 день") == 86400
    assert parse_duration("0м") is None
    assert parse_duration("366д") is None
    assert "&lt;" in mention(1, "<test>")
    assert "&quot;" in mention(1, '"<test>"')

    moderation_source = (ROOT / "handlers" / "moderation.py").read_text(encoding="utf-8")
    notes_source = (ROOT / "handlers" / "notes.py").read_text(encoding="utf-8")
    assert "^проверить мут(\\s|$)" in moderation_source
    assert "escape(str(getattr(e, 'message', '') or e or 'неизвестная ошибка'), quote=True)" in moderation_source
    assert 'Заметки «{escape(name, quote=True)}» нет.' in notes_source

    print(f"OK: {len(python_files)} Python files parsed; {regex_count} command regexes compiled.")
    print(f"OK: base SQLite schema and {len(migration_functions)} migration SQL groups validated.")
    print("OK: duration parsing and HTML escaping checks passed.")
    print("NOTE: run pytest and test with a real Telegram bot before production deployment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
