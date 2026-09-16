from __future__ import annotations

import unittest
from pathlib import Path

from retext.paths import REPO_ROOT, _resolve_data_root


class DataRootTests(unittest.TestCase):
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
