import tkinter as tk
from tkinter import ttk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from retext.adaptive_table import AdaptiveTextTable
from retext.ui_theme import apply_theme


class AdaptiveTableTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.style = ttk.Style(self.root)
        self.style.theme_use("clam")
        apply_theme(self.root, self.style, 96 / 72, 1)
        self.table = AdaptiveTextTable(self.root, columns=("index", "original", "current"), width=640, height=450)
        # Deterministic layout without presenting a test window on the desktop.
        self.table.winfo_width = lambda: 640
        self.table.winfo_height = lambda: 450
        self.table.column("index", width=60, stretch=False)
        self.table.column("original", width=280, stretch=False)
        self.table.column("current", width=280, stretch=False)

    def tearDown(self):
        self.root.destroy()

    def test_mixed_rows_wrap_individually_and_never_exceed_five_times(self):
        short = self.table.insert("", "end", values=(0, "短句", "短句"))
        medium = self.table.insert("", "end", values=(1, "第一行\n第二行\n第三行", "中等"))
        long_text = "利贝尔通信正文" * 10000
        long = self.table.insert("", "end", values=(2, long_text, long_text))
        self.root.update_idletasks()
        heights = [self.table._rows[i].height for i in (short, medium, long)]
        self.assertEqual(heights[0], self.table.base_rowheight)
        self.assertLess(heights[0], heights[1])
        self.assertLess(heights[1], heights[2])
        self.assertLessEqual(heights[2], 5 * heights[0])
        self.assertEqual(self.table.item(long, "values")[2], long_text)
        self.assertTrue(self.table._rows[long].lines["current"][-1].endswith("……"))

    def test_resize_font_scale_and_changed_value_reflow(self):
        iid = self.table.insert("", "end", values=(0, "", "文字" * 35))
        self.root.update_idletasks()
        before = self.table._rows[iid].height
        self.table.column("current", width=560)
        self.root.update_idletasks()
        self.assertLess(self.table._rows[iid].height, before)
        for scale in (1.25, 2, 1):
            apply_theme(self.root, self.style, 96 / 72, scale)
            self.table.refresh_metrics()
            self.root.update_idletasks()
            self.assertLessEqual(self.table._rows[iid].height, self.table.base_rowheight * 5)
        self.table.item(iid, values=(0, "", "短句"))
        self.root.update_idletasks()
        self.assertEqual(self.table._rows[iid].height, self.table.base_rowheight)

    def test_virtualization_and_jump_to_last_row(self):
        for i in range(6000):
            self.table.insert("", "end", iid=str(i), values=(i, "全文" * 1000, "全文" * 1000))
        self.root.update_idletasks()
        self.assertLess(sum(bool(r.height) for r in self.table._rows.values()), 30)
        self.assertLess(len(self.table.find_all()), 200)
        self.table.see("5999")
        self.table.selection_set("5999")
        self.root.update_idletasks()
        bounds = self.table.bbox("5999")
        self.assertTrue(bounds)
        self.assertLess(bounds[1], self.table.winfo_height())
        self.assertLessEqual(bounds[1] + bounds[3], self.table.winfo_height())
        self.assertEqual(self.table.selection(), ("5999",))
        self.assertLess(sum(bool(r.height) for r in self.table._rows.values()), 50)

    def test_search_context_keeps_raw_text_and_can_be_cleared(self):
        text = "前文" * 1000 + "目标" + "后文" * 1000
        iid = self.table.insert("", "end", values=(0, "", text))
        self.table.show_context(iid, "current", (2000, 2002))
        self.root.update_idletasks()
        line = self.table._rows[iid].lines["current"][0]
        self.assertIn("目标", line)
        self.assertTrue(line.startswith("……"))
        self.assertTrue(line.endswith("……"))
        self.assertEqual(self.table.item(iid, "values")[2], text)
        self.assertEqual(self.table._rows[iid].height, self.table.base_rowheight)
        self.table.clear_contexts()
        self.root.update_idletasks()
        self.assertGreater(self.table._rows[iid].height, self.table.base_rowheight)

    def test_stretch_columns_fit_viewport_but_respect_readable_minimums(self):
        self.table.column("original", width=760, minwidth=180, stretch=True)
        self.table.column("current", width=760, minwidth=180, stretch=True)
        self.root.update_idletasks()
        self.assertEqual(sum(self.table._widths.values()), 640)
        self.table.winfo_width = lambda: 300
        self.table._update_widths()
        self.assertEqual(self.table._widths["original"], 180)
        self.assertEqual(self.table._widths["current"], 180)
        self.assertGreater(sum(self.table._widths.values()), 300)

    def test_hidden_columns_do_not_raise_row_height(self):
        iid = self.table.insert("", "end", values=(0, "正文" * 100, "短句"))
        self.table.configure(displaycolumns=("index", "current"))
        self.root.update_idletasks()
        self.assertEqual(self.table._rows[iid].height, self.table.base_rowheight)
        self.table.configure(displaycolumns="#all")
        self.root.update_idletasks()
        self.assertGreater(self.table._rows[iid].height, self.table.base_rowheight)

    def test_inline_editor_changes_reflow_row_without_saving_excerpt(self):
        from tk_gui import RetextTkApp
        table = AdaptiveTextTable(self.root, columns=("index", "location", "original", "current"))
        table.winfo_width = lambda: 640
        table.winfo_height = lambda: 450
        table.configure(displaycolumns=("index", "original", "current"))
        unit = SimpleNamespace(current_text="原文", changed=False)
        app = RetextTkApp.__new__(RetextTkApp)
        app._busy = False
        app.single_tree = table
        app.single_editor = app.single_editor_item = None
        def update(index, value):
            unit.current_text = value
            unit.changed = True
        app.session = SimpleNamespace(document=SimpleNamespace(get_unit=lambda index: unit), update_unit=update)
        iid = table.insert("", "end", values=(0, "字段", "原文", "原文"))
        app.single_row_indices = {iid: 0}
        app._begin_single_edit(iid)
        app.single_editor.delete("1.0", "end")
        full_text = "全长正文" * 1000
        app.single_editor.insert("1.0", full_text)
        app._close_single_editor(save=True)
        self.root.update_idletasks()
        self.assertEqual(unit.current_text, full_text)
        self.assertEqual(table.item(iid, "values")[3], full_text)
        self.assertEqual(table.item(iid, "tags"), ("changed",))
        self.assertLessEqual(table._rows[iid].height, table.max_rowheight)

    def test_inline_editor_selects_actual_late_match_and_preserves_full_text(self):
        from tk_gui import RetextTkApp
        text = "😀前文\n" * 200 + "目标人物" + "后续" * 200
        start = text.index("目标人物")
        unit = SimpleNamespace(current_text=text, changed=False)
        app = RetextTkApp.__new__(RetextTkApp)
        app._busy = False
        app.single_tree = self.table
        # Use the same columns as the real editor, including hidden location.
        app.single_editor = app.single_editor_item = None
        app.session = SimpleNamespace(document=SimpleNamespace(get_unit=lambda index: unit), update_unit=Mock())
        iid = self.table.insert("", "end", values=(0, "", text))
        app.single_row_indices = {iid: 0}
        app._single_match = (iid, start, start + 4, "目标人物")
        app._begin_single_edit(iid)
        self.assertEqual(app.single_editor.get("sel.first", "sel.last"), "目标人物")
        self.assertEqual(app.single_editor.get("1.0", "end-1c"), text)
        app._close_single_editor(save=False)
        app.session.update_unit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
