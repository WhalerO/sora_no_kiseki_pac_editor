import unittest

from retext.text_presentation import excerpt_parts, find_text_span, resolve_hit_span, wrap_bounded


class TextPresentationTests(unittest.TestCase):
    def test_stale_scan_offsets_are_relocated_only_when_unambiguous(self):
        self.assertEqual(resolve_hit_span("前文目标后文", "前文目标后文", 2, 4), (2, 4))
        self.assertEqual(resolve_hit_span("新前文目标后文", "前文目标后文", 2, 4), (3, 5))
        self.assertIsNone(resolve_hit_span("目标新前文目标后文", "前文目标后文", 2, 4))
        self.assertIsNone(resolve_hit_span("没有匹配", "前文目标后文", 2, 4))
    def test_excerpt_is_centered_on_late_hit_and_fits_column(self):
        text = "报纸前文" * 5000 + "目标人物" + "报纸后文" * 5000
        parts = excerpt_parts(text, 20000, 20004, width=40)
        self.assertEqual(parts[1], "目标人物")
        self.assertTrue(parts[0].startswith("……"))
        self.assertTrue(parts[2].endswith("……"))
        self.assertLessEqual(len("".join(parts)), 40)

    def test_pixel_fit_accounts_for_bold_match_and_chinese_width(self):
        measure = lambda s: sum(2 if ord(c) > 127 else 1 for c in s)
        bold = lambda s: 2 * measure(s)
        parts = excerpt_parts("开头内容" * 100 + "艾斯蒂尔" + "后文" * 100, 400, 404,
                              width=32, measure=measure, measure_match=bold)
        self.assertEqual(parts[1], "艾斯蒂尔")
        self.assertLessEqual(measure(parts[0]) + bold(parts[1]) + measure(parts[2]), 32)

    def test_full_article_match_and_tiny_columns_are_bounded(self):
        text = "全文" * 100000
        self.assertLessEqual(len("".join(excerpt_parts(text, 0, len(text)))), 80)
        for width in (1, 2, 3, 4, 5, 10, 30):
            for span in ((0, len(text)), (20, 22)):
                self.assertLessEqual(len("".join(excerpt_parts(text, *span, width=width))), width)

    def test_start_end_newlines_empty_and_invalid_ranges(self):
        self.assertEqual(excerpt_parts("命中\r\n后续", 0, 2), ("", "命中", "↵后续"))
        self.assertEqual(excerpt_parts("前文命中", 2, 4), ("前文", "命中", ""))
        self.assertEqual(excerpt_parts("", 0, 0), ("", "", ""))
        self.assertEqual(excerpt_parts("短句", -1, 2), ("短句", "", ""))

    def test_normalized_search_returns_original_offsets(self):
        for text, query, expected in (
            ("前文😀利贝尔通信后文", "利贝尔", (3, 6)),
            ("Straße目标", "目标", (6, 8)),
            ("Straße", "SS", (4, 5)),
            ("e\u0301后文", "é", (0, 2)),
            ("e\u0301后文", "后文", (2, 4)),
            ("\u1100\u1161\u11a8目标", "目标", (3, 5)),
            ("가\u11a8目标", "目标", (2, 4)),
        ):
            with self.subTest(text=text, query=query):
                self.assertEqual(find_text_span(text, query), expected)
        self.assertIsNone(find_text_span("ABC", "abc", case_sensitive=True))
        self.assertIsNone(find_text_span("ABC", ""))

    def test_wrapping_preserves_lines_and_caps_without_consuming_entire_book(self):
        self.assertEqual(wrap_bounded("第一行\r\n第二行\n", 10, len, 5), ["第一行", "第二行", ""])
        self.assertEqual(wrap_bounded("短句", 10, len, 5), ["短句"])
        self.assertEqual(wrap_bounded("", 10, len, 5), [""])
        measured = []
        def measure(value):
            measured.append(len(value))
            return len(value)
        lines = wrap_bounded("正文" * 100000, 20, measure, 5)
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[-1].endswith("……"))
        self.assertTrue(all(len(line) <= 20 for line in lines))
        self.assertLess(max(measured), 150)
        self.assertLess(len(measured), 80)


if __name__ == "__main__":
    unittest.main()
