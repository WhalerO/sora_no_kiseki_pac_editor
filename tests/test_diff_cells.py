import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from retext.business import DiffEntryRow
from retext.diff_cells import DiffTextCells


class DiffCellTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.tree = ttk.Treeview(self.root, columns=("old", "new"), show="headings")
        self.tree._tis_ybar = ttk.Scrollbar(self.root)
        self.tree._tis_xbar = ttk.Scrollbar(self.root)
        self.tree.winfo_ismapped = lambda: True
        self.tree.winfo_height = lambda: 150
        self.visible = "0"
        self.tree.identify_row = lambda y: self.visible if y >= 24 else ""
        self.tree.bbox = lambda item, column: (0 if column == "old" else 250, 24, 250, 30)
        self.rows = [DiffEntryRow(i, i, "约书亚：准备出发。", "约修亚：准备出发。", "modified") for i in range(50)]
        for i in range(len(self.rows)):
            self.tree.insert("", "end", iid=str(i))
        self.cells = DiffTextCells(self.tree, lambda item: self.rows[int(item)])

    def tearDown(self):
        self.root.destroy()

    def test_actual_changes_are_bold_and_stationary_repaint_reuses_excerpt(self):
        self.cells.render()
        for column, expected in (("old", "书"), ("new", "修")):
            cell = self.cells.cells[("0", column)]
            self.assertEqual(cell.get(*cell.tag_ranges("changed")), expected)
            self.assertEqual(self.cells.bold.actual("weight"), "bold")
        with patch("retext.diff_cells.excerpt_parts", side_effect=AssertionError("recomputed")):
            self.cells.render()

    def test_scroll_pool_and_reset_do_not_leave_stale_overlays(self):
        self.cells.render()
        widgets = set(self.cells.cells.values())
        for index in range(1, len(self.rows)):
            self.visible = str(index)
            self.cells.render()
            self.assertEqual(set(self.cells.cells.values()), widgets)
        self.cells.reset()
        self.assertEqual(self.cells.cells, {})
        self.assertTrue(all(not widget.place_info() for widget in widgets))

    def test_long_text_shows_changed_tail_not_only_the_beginning(self):
        prefix = "利贝尔通信：" * 10000
        self.rows[0] = DiffEntryRow(0, 0, prefix + "旧", prefix + "新", "modified")
        self.cells.render()
        for column, expected in (("old", "旧"), ("new", "新")):
            cell = self.cells.cells[("0", column)]
            self.assertEqual(cell.get(*cell.tag_ranges("changed")), expected)
            self.assertLess(len(cell.get("1.0", "end-1c")), 100)


if __name__ == "__main__":
    unittest.main()
