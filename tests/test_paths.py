from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from retext.paths import REPO_ROOT, _resolve_data_root, create_runtime_dir


class DataRootTests(unittest.TestCase):
    def test_runtime_directories_are_short_unique_and_exclusively_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("retext.paths.ensure_runtime_root", return_value=root):
                first = create_runtime_dir("pac_session")
                second = create_runtime_dir("pac_session")
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())
            self.assertEqual(first.parent, root)
            self.assertTrue(first.name.startswith("pac_session_"))
            self.assertLessEqual(len(first.name), len("pac_session_") + 8)

    def test_source_run_uses_repository_runtime(self) -> None:
        self.assertEqual(
            _resolve_data_root(
                configured="",
                frozen=False,
                executable=REPO_ROOT / ".venv/Scripts/python.exe",
            ),
            REPO_ROOT / ".runtime",
        )

    def test_frozen_run_uses_executable_directory_not_current_directory(self) -> None:
        self.assertEqual(
            _resolve_data_root(
                configured="",
                frozen=True,
                executable=REPO_ROOT / "portable/TIS_Retext.exe",
            ),
            REPO_ROOT / "portable/TIS_Retext_Data",
        )

    def test_explicit_data_directory_wins(self) -> None:
        configured = str((REPO_ROOT / "custom-data").resolve())
        self.assertEqual(
            _resolve_data_root(
                configured=configured,
                frozen=True,
                executable=REPO_ROOT / "ignored/TIS_Retext.exe",
            ),
            Path(configured),
        )


if __name__ == "__main__":
    unittest.main()
