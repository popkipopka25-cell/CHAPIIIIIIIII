import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ChapiTests(unittest.TestCase):
    def test_python_syntax(self):
        files = [
            p for p in ROOT.rglob("*.py")
            if ".git" not in p.parts
            and ".venv" not in p.parts
        ]
        self.assertTrue(files, "Python-файлы не найдены")

        for file in files:
            with self.subTest(file=str(file.relative_to(ROOT))):
                ast.parse(
                    file.read_text(encoding="utf-8"),
                    filename=str(file),
                )

    def test_handlers_folder_exists(self):
        self.assertTrue((ROOT / "handlers").is_dir())

    def test_utils_file_exists(self):
        self.assertTrue((ROOT / "utils.py").is_file())


if __name__ == "__main__":
    unittest.main()
