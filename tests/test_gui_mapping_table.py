from __future__ import annotations

import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from retext.business import BatchMapping
from tk_gui import MappingTable


class MappingTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display is unavailable: {exc}") from exc
        cls.checkbox_images = {
            False: tk.PhotoImage(master=cls.root, width=12, height=12),
            True: tk.PhotoImage(master=cls.root, width=12, height=12),
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.root.destroy()

    def setUp(self) -> None:
        self.table = MappingTable(self.root, self.checkbox_images)

    def tearDown(self) -> None:
        self.table.destroy()

    def test_each_mapping_row_has_an_independent_full_match_flag(self) -> None:
        exact = self.table._insert(True, "雷光", "审判雷光", True)
        self.table._insert(True, "风花", "春风", False)
        self.table._insert(False, "停用", "忽略", True)

        self.assertEqual(
            self.table.get_pairs(),
            [
                BatchMapping("雷光", "审判雷光", True),
                BatchMapping("风花", "春风", False),
            ],
        )
        self.table.tree.selection_set(exact)
        self.table.toggle_full_match()
        self.assertFalse(self.table.get_rows()[0]["full_match"])

    def test_mapping_json_v2_roundtrips_and_v1_defaults_to_contains(self) -> None:
        self.table._insert(True, "雷光", "审判雷光", True)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            v2_path = root / "mapping-v2.json"
            with patch(
                "tk_gui.filedialog.asksaveasfilename",
                return_value=str(v2_path),
            ):
                self.table.save_rows()
            payload = json.loads(v2_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 2)
            self.assertTrue(payload["mappings"][0]["full_match"])

            loaded = MappingTable(self.root, self.checkbox_images)
            try:
                with patch(
                    "tk_gui.filedialog.askopenfilename",
                    return_value=str(v2_path),
                ):
                    loaded.load_rows()
                self.assertEqual(
                    loaded.get_pairs(),
                    [BatchMapping("雷光", "审判雷光", True)],
                )

                v1_path = root / "mapping-v1.json"
                v1_path.write_text(
                    json.dumps(
                        {
                            "format": "tis-retext-mapping",
                            "version": 1,
                            "mappings": [
                                {"enabled": True, "old": "旧", "new": "新"}
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with patch(
                    "tk_gui.filedialog.askopenfilename",
                    return_value=str(v1_path),
                ):
                    loaded.load_rows()
                self.assertEqual(
                    loaded.get_pairs(),
                    [BatchMapping("旧", "新", False)],
                )
            finally:
                loaded.destroy()

    def test_move_selected_rows_preserves_relative_order_and_flags(self):
        rows = [self.table._insert(True, str(i), str(i + 1), i == 2) for i in range(5)]
        self.table.tree.selection_set(rows[1:3])
        self.table.move_rows(-1)
        self.assertEqual([p.old for p in self.table.get_pairs()], ["1", "2", "0", "3", "4"])
        self.assertTrue(self.table.get_pairs()[1].full_match)
        self.table.move_rows(1)
        self.assertEqual([p.old for p in self.table.get_pairs()], ["0", "1", "2", "3", "4"])
        self.table.tree.selection_set((rows[0], rows[-1]))
        self.table.move_rows(-1)
        self.assertEqual([p.old for p in self.table.get_pairs()], ["0", "1", "2", "4", "3"])


if __name__ == "__main__":
    unittest.main()
