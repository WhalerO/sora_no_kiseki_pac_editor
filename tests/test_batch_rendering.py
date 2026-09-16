import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from tk_gui import RetextTkApp
from retext.business import ServiceBatchHit
from retext.batch_display import hit_presentation
from retext.compact_toolbar import CompactToolbar
from retext.ui_theme import apply_theme


class BatchRenderingTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.style = ttk.Style(self.root)
        apply_theme(self.root, self.style, 96 / 72, 1)
        self.app = RetextTkApp.__new__(RetextTkApp)
        app = self.app
        app.style = self.style
        app.batch_hit_tree = ttk.Treeview(self.root, columns=("old", "new"))
        app.batch_hit_tree.winfo_ismapped = lambda: True
        self.visible = {"0", "1", "2"}
        app._visible_batch_items = lambda: self.visible
        app.batch_hit_tree.bbox = lambda item, col: (0 if col == "old" else 230, 28 + int(item) % 3 * 32, 230, 32)
        app._batch_cell_overlays = {}
        app._batch_rich_after_id = None
        app.batch_hits = [ServiceBatchHit(True, "test.tbl", "TBL", i, str(i), "前文" * 4000 + "命中" + "后文" * 4000, "前文" * 4000 + "替换" + "后文" * 4000, "命中", "替换", match_start=8000, match_end=8002, write_mode="patch" if i % 2 else "pool-repack") for i in range(30)]
        for i in range(30):
            app.batch_hit_tree.insert("", "end", iid=str(i))

    def tearDown(self):
        self.app._clear_batch_rich_cells()
        self.root.destroy()

    def test_scroll_reuses_widgets_and_stationary_repaint_reuses_excerpts(self):
        app = self.app
        app._render_batch_rich_cells()
        first_widgets = set(app._batch_cell_overlays.values())
        self.assertEqual(len(first_widgets), 6)
        with patch("tk_gui.excerpt_parts", side_effect=AssertionError("should be cached")):
            app._render_batch_rich_cells()
        for index in range(3, 27, 3):
            self.visible = {str(index), str(index + 1), str(index + 2)}
            app._render_batch_rich_cells()
            self.assertEqual(set(app._batch_cell_overlays.values()), first_widgets)
        self.assertLessEqual(len(app._batch_excerpt_cache), 1024)

    def test_overlays_use_exact_row_capability_colors(self):
        self.app._render_batch_rich_cells()
        for (iid, _column), widget in self.app._batch_cell_overlays.items():
            color = hit_presentation(self.app.batch_hits[int(iid)])
            self.assertEqual(widget.cget("background"), color.background)
            self.assertEqual(widget.cget("foreground"), color.foreground)
        self.app.batch_hit_tree.selection_set("1")
        self.app._render_batch_rich_cells()
        self.assertEqual(self.app._batch_cell_overlays[("1", "old")].cget("background"), "#dbeafe")

    def test_toolbar_wraps_without_grid_column_width_collisions(self):
        toolbar = CompactToolbar(self.root)
        toolbar.winfo_width = lambda: 225
        for text in ("短", "比较长的操作", "短", "另一个操作", "短"):
            toolbar.add(text, lambda: None)
        self.root.update_idletasks()
        toolbar._layout()
        rows = set()
        for button in toolbar.buttons:
            info = button.place_info()
            rows.add(info["y"])
            self.assertLessEqual(int(info["x"]) + int(info["width"]), 225)
        self.assertGreater(len(rows), 1)
        toolbar.destroy()

    def test_unchecked_rich_cells_are_white(self):
        self.app.batch_hits[1].checked = False
        self.app._render_batch_rich_cells()
        for column in ("old", "new"):
            self.assertEqual(self.app._batch_cell_overlays[("1", column)].cget("background"), "#ffffff")


if __name__ == "__main__":
    unittest.main()
