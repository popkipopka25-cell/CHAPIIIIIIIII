
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import utils


class ChapiTests(unittest.TestCase):
    """Автоматические проверки основных частей Чапи."""

    def test_all_python_files_have_valid_syntax(self):
        files = [
            p for p in ROOT.rglob("*.py")
            if not any(
                part in {".git", ".venv", "__pycache__"}
                for part in p.parts
            )
        ]
        self.assertTrue(files, "Python-файлы не найдены")

        for file in files:
            with self.subTest(file=str(file.relative_to(ROOT))):
                ast.parse(
                    file.read_text(encoding="utf-8"),
                    filename=str(file),
                )

    def test_required_files_exist(self):
        required = (
            "config.py",
            "database.py",
            "utils.py",
            "migrations.py",
            "requirements.txt",
            "handlers/__init__.py",
            "handlers/command_rights.py",
            "handlers/moderation.py",
        )
        for name in required:
            with self.subTest(file=name):
                self.assertTrue(
                    (ROOT / name).is_file(),
                    f"Не найден обязательный файл: {name}",
                )

    def test_handlers_package_exists(self):
        handlers = ROOT / "handlers"
        self.assertTrue(handlers.is_dir())
        self.assertTrue((handlers / "__init__.py").is_file())
        self.assertTrue(
            list(handlers.glob("*.py")),
            "В handlers нет Python-файлов",
        )

    def test_rank_names_are_complete(self):
        self.assertEqual(
            set(utils.RANK_NAMES),
            {0, 1, 2, 3, 4, 5},
        )
        self.assertEqual(utils.RANK_NAMES[0], "Участник")
        self.assertEqual(
            utils.RANK_NAMES[1], "Младший модератор"
        )
        self.assertEqual(
            utils.RANK_NAMES[2], "Старший модератор"
        )
        self.assertEqual(
            utils.RANK_NAMES[3], "Младший админ"
        )
        self.assertEqual(
            utils.RANK_NAMES[4], "Старший админ"
        )
        self.assertEqual(utils.RANK_NAMES[5], "Создатель")

    def test_role_ranks_are_valid(self):
        for role, rank in utils.ROLE_RANKS.items():
            with self.subTest(role=role):
                self.assertIsInstance(rank, int)
                self.assertGreaterEqual(rank, 0)
                self.assertLessEqual(rank, 5)

    def test_unknown_role_has_no_extra_privileges(self):
        self.assertFalse(utils.can_act_on("unknown", "user"))
        self.assertFalse(utils.has_at_least("unknown", "admin"))

    def test_higher_role_can_act_on_lower_role(self):
        self.assertTrue(utils.can_act_on("admin", "user"))

    def test_equal_or_lower_role_cannot_act_on_higher_role(self):
        self.assertFalse(utils.can_act_on("user", "admin"))
        self.assertFalse(utils.can_act_on("admin", "admin"))

    def test_duration_parser_valid_values(self):
        cases = {
            "10 сек": 10,
            "5 мин": 300,
            "2 часа": 7200,
            "3 дня": 259200,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(
                    utils.parse_duration(value), expected
                )

    def test_duration_parser_rejects_invalid_values(self):
        for value in (
            "",
            "0 мин",
            "-5 мин",
            "вечность",
            "5 мин навсегда",
            "999999 дней",
        ):
            with self.subTest(value=value):
                self.assertIsNone(utils.parse_duration(value))

    def test_duration_formatter(self):
        cases = {
            10: "10 сек.",
            60: "1 мин.",
            3600: "1 ч.",
            86400: "1 дн.",
        }
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(
                    utils.format_duration(seconds), expected
                )

    def test_human_duration(self):
        self.assertEqual(utils.human_duration(0), "0 секунд")
        self.assertEqual(utils.human_duration(60), "1 минуту")
        self.assertEqual(utils.human_duration(3600), "1 час")
        self.assertEqual(utils.human_duration(86400), "1 день")

    def test_mention_escapes_html(self):
        result = utils.mention(12345, "<admin>")
        self.assertIn("12345", result)
        self.assertNotIn("<admin>", result)
        self.assertIn("&lt;admin&gt;", result)

    def test_target_resolution_checks_id(self):
        source = (ROOT / "utils.py").read_text(encoding="utf-8")
        self.assertIn("async def resolve_target", source)
        self.assertIn("find_user_by_username", source)
        self.assertIn("Числовой ID слишком длинный", source)

    def test_database_has_core_tables(self):
        source = (ROOT / "database.py").read_text(encoding="utf-8")
        for table in (
            "users",
            "members",
            "settings",
            "punishments",
            "notes",
            "transactions",
            "marriages",
            "clans",
            "mod_log",
        ):
            with self.subTest(table=table):
                self.assertRegex(
                    source,
                    rf"CREATE TABLE IF NOT EXISTS\s+{table}\b",
                )

    def test_group_settings_have_safe_defaults(self):
        source = (ROOT / "database.py").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_SETTINGS", source)
        self.assertIn('"antispam": True', source)
        self.assertIn("async def get_settings", source)
        self.assertIn("async def set_setting", source)

    def test_command_rank_codes_are_defined(self):
        source = (
            ROOT / "handlers" / "command_rights.py"
        ).read_text(encoding="utf-8")
        for expected in (
            "COMMANDS =",
            "def detect_command",
            "async def get_override",
            "class CommandRankMiddleware",
            "async def command_rank_settings",
            "command_rank_overrides",
        ):
            with self.subTest(item=expected):
                self.assertIn(expected, source)

    def test_mute_command_has_code(self):
        source = (
            ROOT / "handlers" / "command_rights.py"
        ).read_text(encoding="utf-8")
        self.assertRegex(source, r"20:\s*\(\"мут\",?\)")

    def test_rank_override_has_database_limits(self):
        source = (
            ROOT / "handlers" / "command_rights.py"
        ).read_text(encoding="utf-8")
        self.assertIn("CHECK(min_rank BETWEEN 0 AND 5)", source)
        self.assertIn("PRIMARY KEY(chat_id, command_code)", source)

    def test_command_rights_router_is_registered(self):
        source = (
            ROOT / "handlers" / "__init__.py"
        ).read_text(encoding="utf-8")
        self.assertIn("command_rights.CommandRankMiddleware()", source)
        self.assertIn("router.include_router(command_rights.router)", source)

    def test_owner_id_is_used_for_creator_check(self):
        source = (
            ROOT / "handlers" / "command_rights.py"
        ).read_text(encoding="utf-8")
        self.assertIn("config.OWNER_TELEGRAM_ID", source)
        self.assertIn("message.from_user.id", source)

    def test_workflow_runs_tests(self):
        workflow_dir = ROOT / ".github" / "workflows"
        workflows = list(workflow_dir.glob("*.yml"))
        workflows += list(workflow_dir.glob("*.yaml"))
        self.assertTrue(workflows, "GitHub Actions workflow не найден")

        combined = "\n".join(
            p.read_text(encoding="utf-8") for p in workflows
        )
        self.assertIn("unittest discover", combined)

    def test_test_file_has_no_accidental_empty_content(self):
        self.assertGreater(len(Path(__file__).read_text(encoding="utf-8")), 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
