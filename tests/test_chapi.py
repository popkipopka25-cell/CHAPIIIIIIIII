import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ChapiTests(unittest.TestCase):
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
        for name in ("utils.py", "database.py", "config.py"):
            with self.subTest(file=name):
                self.assertTrue(
                    (ROOT / name).is_file(),
                    f"Не найден файл {name}",
                )

    def test_handlers_package_exists(self):
        self.assertTrue((ROOT / "handlers").is_dir())
        self.assertTrue((ROOT / "handlers" / "__init__.py").is_file())

    def test_handler_modules_have_valid_syntax(self):
        handlers = ROOT / "handlers"
        files = list(handlers.glob("*.py"))
        self.assertTrue(files, "В handlers нет Python-файлов")

        for file in files:
            with self.subTest(file=file.name):
                ast.parse(
                    file.read_text(encoding="utf-8"),
                    filename=str(file),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
