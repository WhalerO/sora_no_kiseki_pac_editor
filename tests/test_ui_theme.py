import tkinter as tk
from tkinter import font as tkfont, ttk
import unittest

from retext.ui_theme import apply_theme


class UiThemeTests(unittest.TestCase):
    def test_sidebar_guard_scales_its_fixed_width_with_dpi(self):
        from tk_gui import RetextTkApp
        target = RetextTkApp._main_sash_target(2100, 330, pixel_scale=1.75)
        self.assertEqual(target, round(330 * 1.75))
        self.assertIsNone(RetextTkApp._main_sash_target(2100, target, pixel_scale=1.75))

    def test_row_height_follows_actual_font_metrics_without_compounding_dpi(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        try:
            style = ttk.Style(root)
            style.theme_use("clam")
            for native in (96 / 72, 144 / 72, 192 / 72):
                heights = []
                for scale in (0.67, 0.75, 0.85, 1.0, 1.25, 1.5, 2.0, 0.67):
                    apply_theme(root, style, native, scale)
                    line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
                    row = int(style.lookup("Treeview", "rowheight"))
                    heights.append(row)
                    self.assertGreaterEqual(row, line + (6 if scale < 1 else 8))
                    self.assertAlmostEqual(float(root.tk.call("tk", "scaling")), native, delta=0.03)
                self.assertEqual(heights[0], heights[-1])
                self.assertLess(heights[0], heights[-2])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
