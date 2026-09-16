from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from retext.business import WorkspaceBusiness, BusinessFileTarget, BatchMapping
from retext.domain import DocumentKind, TextUnit
from retext.session import DocumentSession, SessionOptions
from retext.text_presentation import find_text_spans
from retext.pac_files_view import directory_index
from retext.batch_display import hit_presentation
from retext.text_search import search_text_targets
from test_regressions import minimal_exact_dat


class OrderedBatchTests(unittest.TestCase):
    def test_chaining_and_reverse_order_produce_different_results(self):
        service = WorkspaceBusiness()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.dat"
            for rules, expected, count in (([("阵", "阵营"), ("阵营", "队伍")], "队伍", 2),
                                            ([("阵营", "队伍"), ("阵", "阵营")], "阵营", 1)):
                path.write_bytes(minimal_exact_dat())
                scan = service.scan_mixed_batch_targets([BusinessFileTarget(str(path), path.name)], "*.dat", rules, ordered=True, use_equal_fast_path=False)
                self.assertTrue(scan.complete)
                self.assertEqual(len(scan.hits), count)
                self.assertTrue(all(h.writable for h in scan.hits))
                ok, fail, logs = service.execute_mixed_batch(scan.hits, rules, do_backup=False)
                self.assertEqual((ok, fail), (count, 0), logs)
                document = DocumentSession().open_document(path)
                self.assertIn(expected, [u.current_text for u in document.units])

    def test_unchecked_dependency_and_changed_order_cannot_write(self):
        service = WorkspaceBusiness()
        rules = [("阵", "阵营"), ("阵营", "队伍")]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.dat"
            original = minimal_exact_dat()
            path.write_bytes(original)
            scan = service.scan_mixed_batch_targets([BusinessFileTarget(str(path), path.name)], "*.dat", rules, ordered=True, use_equal_fast_path=False)
            scan.hits[0].checked = False
            ok, fail, logs = service.execute_mixed_batch(scan.hits, rules, do_backup=False)
            self.assertEqual((ok, fail), (0, 1), logs)
            self.assertIn("前序依赖", "".join(logs))
            self.assertEqual(path.read_bytes(), original)
            ok, fail, logs = service.execute_mixed_batch(scan.hits, list(reversed(rules)), do_backup=False)
            self.assertEqual(ok, 0)
            self.assertIn("顺序已改变", "".join(logs))
            self.assertEqual(path.read_bytes(), original)

    def test_same_rule_does_not_loop_and_full_match_uses_stage_input(self):
        service = WorkspaceBusiness()
        document = SimpleNamespace(kind=DocumentKind.TBL, units=[TextUnit(3, "甲甲", "甲甲", "row")])
        document.get_unit = lambda index: document.units[0]
        rules = [BatchMapping("甲", "甲甲"), BatchMapping("甲甲甲甲", "乙", True)]
        hits = service._scan_ordered_document_hits(Path("test.tbl"), document, rules)
        self.assertEqual(len(hits), 3)
        self.assertEqual(document.units[0].current_text, "甲甲")
        service._apply_service_hits(document, hits)
        self.assertEqual(document.units[0].current_text, "乙")

    def test_batch_save_refuses_change_since_load(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.dat"
            path.write_bytes(minimal_exact_dat())
            document = DocumentSession().open_document(path)
            path.write_bytes(b"external change")
            with self.assertRaisesRegex(RuntimeError, "changed on disk"):
                WorkspaceBusiness()._save_and_verify_document(document, path, do_backup=False)
            self.assertEqual(path.read_bytes(), b"external change")


class PresentationTests(unittest.TestCase):
    def test_matches_use_original_unicode_offsets_and_every_occurrence(self):
        self.assertEqual(find_text_spans("甲目标乙目标", "目标"), [(1, 3), (4, 6)])
        self.assertEqual(find_text_spans("e\u0301 É", "é"), [(0, 2), (3, 4)])
        self.assertEqual(find_text_spans("ß SS", "ss"), [(0, 1), (2, 4)])
        self.assertEqual(find_text_spans("ß", "s"), [(0, 1)])
        self.assertEqual(find_text_spans("", ""), [])

    def test_directory_index_aggregates_nested_sizes_without_extraction(self):
        index = directory_index([("a/file.tbl", 12, "未修改"), ("a/b/image.dds", 100, "新增"), ("root.dat", 5, "已修改")])
        self.assertEqual([(v.path, v.size) for v in index[""]], [("a", 112), ("root.dat", 5)])
        self.assertEqual([v.path for v in index["a"]], ["a/b", "a/file.tbl"])
        self.assertEqual(index["a/b"][0].state, "新增")

    def test_write_capabilities_share_labels_and_colors(self):
        for mode, writable, expected in (("search-only", False, "unchanged"), ("SLOT", True, "patch"), ("pool-repack", True, "rebuild"), ("repack-risk", True, "risk"), ("", False, "blocked")):
            hit = SimpleNamespace(pair_old="a", pair_new="b", write_mode=mode, writable=writable)
            self.assertEqual(hit_presentation(hit).key, expected)
        self.assertEqual(hit_presentation(SimpleNamespace(pair_old="a", pair_new="a")).label, "未变更")

    def test_readonly_search_preserves_bytes_and_reports_parse_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.dat"
            original = minimal_exact_dat()
            path.write_bytes(original)
            result = search_text_targets([BusinessFileTarget(str(path), "scena/test.dat", "pac-id"), BusinessFileTarget(str(path.parent / "missing.dat"), "missing.dat")], "阵", options=SessionOptions())
            self.assertEqual(len(result.hits), 1)
            self.assertEqual(result.parsed, 1)
            self.assertEqual(len(result.errors), 1)
            self.assertEqual(result.hits[0].source_id, "pac-id")
            self.assertEqual(path.read_bytes(), original)

    def test_readonly_search_uses_logical_tbl_name_for_schema(self):
        with patch("retext.text_search.DocumentSession") as factory:
            session = factory.return_value
            session.find_matches.return_value = []
            options = SessionOptions()
            search_text_targets([BusinessFileTarget("123.tbl", "table/t_books.tbl")], "甲", options=options)
            selected = session.open_document.call_args.kwargs["options"]
            self.assertEqual(selected.schema_hint, "t_books")
            self.assertEqual(options.schema_hint, "")


if __name__ == "__main__":
    unittest.main()
