import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from retext.archive import FpacArchiveService, PacWorkspaceManager
from retext.archive.repair import _analyze_dat_payload, DatReferenceRepairService, PacDatReferenceRepairService
from retext.business import BusinessFileTarget
from retext.domain import DocumentKind, TextUnit
from retext.session import SessionOptions
from retext.text_search import TextSearchResult, search_text_targets, search_pac_targets
from retext.batch_display import hit_presentation, hit_row_presentation
from retext.diff_presentation import difference_spans
from tk_gui import RetextTkApp
from tests.test_pac_archive import _make_pac, _minimal_exact_dat


class StreamingSearchTests(unittest.TestCase):
    def document(self, text):
        return SimpleNamespace(kind=DocumentKind.DAT, units=[TextUnit(0, text, text, "entry")])

    def test_first_hit_arrives_before_next_target_and_batches_are_bounded(self):
        batches = []
        def targets():
            yield BusinessFileTarget("first.dat", "first.dat")
            self.assertTrue(batches)
            self.assertEqual(len(batches[0]), 1)
            yield BusinessFileTarget("second.dat", "second.dat")
        with patch("retext.text_search.DocumentSession") as factory:
            factory.return_value.open_document.return_value = self.document("目标 " * 150)
            result = search_text_targets(targets(), "目标", options=SessionOptions(), on_hits=batches.append)
        self.assertEqual(len(result.hits), 300)
        self.assertEqual(sum(map(len, batches)), 300)
        self.assertTrue(all(len(batch) <= 64 for batch in batches))
        self.assertEqual(result.parsed, 2)

    def test_cancellation_stops_before_next_match_and_file(self):
        stop = threading.Event()
        with patch("retext.text_search.DocumentSession") as factory:
            factory.return_value.open_document.return_value = self.document("目标 " * 150)
            result = search_text_targets(
                [BusinessFileTarget("first.dat", "first.dat"), BusinessFileTarget("next.dat", "next.dat")],
                "目标", options=SessionOptions(), cancelled=stop.is_set,
                on_hits=lambda _hits: stop.set(),
            )
            self.assertEqual(factory.return_value.open_document.call_count, 1)
        self.assertTrue(result.cancelled)
        self.assertEqual(len(result.hits), 1)

    def test_pac_snapshot_keeps_identity_and_does_not_materialize_live_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pac = root / "source.pac"
            pac.write_bytes(_make_pac([("script/scena/a.dat", _minimal_exact_dat())]))
            archive = FpacArchiveService().inspect(pac)
            cached = root / "live-cache.dat"
            target = BusinessFileTarget(str(cached), "script/scena/a.dat", "workspace")
            hits = search_pac_targets([(target, archive, False)], "阵", options=SessionOptions()).hits
            self.assertEqual(len(hits), 1)
            self.assertEqual((hits[0].file, hits[0].source_id), (str(cached), "workspace"))
            self.assertFalse(cached.exists())
            # A deleted dirty cache must never silently fall back to original data.
            result = search_pac_targets([(target, archive, True)], "阵", options=SessionOptions())
            self.assertEqual(result.hits, [])
            self.assertIn("丢失", result.errors[0])
            cached.write_bytes(_minimal_exact_dat().replace("阵".encode(), "金".encode()))
            result = search_pac_targets([(target, archive, True)], "金", options=SessionOptions())
            self.assertEqual(len(result.hits), 1)

    def app(self):
        app = RetextTkApp.__new__(RetextTkApp)
        app._content_generation = 1
        app._content_running = True
        app._content_queue = queue.Queue()
        app._content_stop = threading.Event()
        app._content_run_key = ("pac", "目标", "options")
        app._content_targets = ["a.dat", "b.dat"]
        app._content_hits = []
        app._content_cursor = -1
        app._content_error_count = 0
        app._content_processed = 0
        app._content_signature = None
        app.content_query_var = SimpleNamespace(get=lambda: "目标")
        app.resource_search_mode_var = SimpleNamespace(get=lambda: "正文")
        app.content_status_var = Mock()
        app.current_resource_mode = lambda: "pac"
        app._content_source_stamp = lambda: "stamp"
        app._open_text_hit = Mock()
        app.root = Mock()
        return app

    def test_gui_navigates_on_first_chunk_and_keeps_cursor_on_completion(self):
        app = self.app()
        app._content_queue.put((1, "hits", ["first"]))
        app._poll_content_search(1)
        self.assertTrue(app._content_running)
        app._open_text_hit.assert_called_once_with("first", edit=False)
        app._content_queue.put((1, "hits", ["second"]))
        app._content_queue.put((1, "done", TextSearchResult(parsed=2)))
        app._poll_content_search(1)
        self.assertFalse(app._content_running)
        self.assertEqual(app._content_cursor, 0)
        self.assertEqual(app._content_hits, ["first", "second"])
        self.assertEqual(app._open_text_hit.call_count, 1)

    def test_old_generation_and_changed_query_cannot_publish_results(self):
        app = self.app()
        app._content_queue.put((0, "hits", ["stale"]))
        app._poll_content_search(1)
        self.assertEqual(app._content_hits, [])
        app.content_query_var = SimpleNamespace(get=lambda: "new query")
        app._poll_content_search(1)
        self.assertTrue(app._content_stop.is_set())
        self.assertFalse(app._content_running)
        app._content_queue.put((1, "hits", ["late"]))
        app._poll_content_search(1)
        self.assertEqual(app._content_hits, [])

    def test_mutating_business_action_cancels_readonly_search(self):
        app = self.app()
        app._busy = False
        self.assertTrue(app._ensure_idle())
        self.assertTrue(app._content_stop.is_set())

    def test_edit_and_save_actions_stop_search_before_touching_document(self):
        for action, args in (("save_single", ()), ("export_single", ()),
                             ("_begin_single_edit", ("0",)), ("replace_all", ())):
            app = self.app()
            app._ensure_idle = Mock(return_value=False)
            getattr(app, action)(*args)
            app._ensure_idle.assert_called_once()


class ExportStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pac"
        self.source.write_bytes(_make_pac([("a.tbl", b"original")]))
        self.manager = PacWorkspaceManager(workspaces_root=self.root / "ws", trash_root=self.root / "trash")
        self.workspace = self.manager.open(self.source)
        self.addCleanup(lambda: self.workspace.close())
        self.cached = self.workspace.materialize("a.tbl")
        self.cached.write_bytes(b"edited")
        self.workspace.refresh_entry("a.tbl")

    def test_save_as_clears_unbuilt_not_original_dirty_and_repeat_export_keeps_edits(self):
        workspace = self.workspace
        self.assertEqual(workspace.unbuilt_entry_names(), ["a.tbl"])
        workspace.build(self.root / "output.pac")
        self.assertEqual(workspace.unbuilt_entry_names(), [])
        self.assertEqual(workspace.dirty_entry_names(), ["a.tbl"])
        self.assertEqual(workspace.state(), "exported")
        workspace.build(self.root / "second.pac")
        service = FpacArchiveService()
        self.assertEqual(service.read_entry_bytes(service.inspect(self.root / "second.pac"), "a.tbl"), b"edited")
        self.assertEqual(service.read_entry_bytes(service.inspect(self.source), "a.tbl"), b"original")

    def test_new_edit_and_revert_after_export_are_unbuilt(self):
        workspace = self.workspace
        workspace.build(self.root / "output.pac")
        self.cached.write_bytes(b"edited again")
        workspace.refresh_entry("a.tbl")
        self.assertEqual(workspace.unbuilt_entry_names(), ["a.tbl"])
        workspace.discard_entry("a.tbl")
        self.assertEqual(workspace.dirty_entry_names(), [])
        self.assertEqual(workspace.unbuilt_entry_names(), ["a.tbl"])

    def test_failed_build_never_clears_pending_edits(self):
        with patch.object(self.workspace.service, "build", side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                self.workspace.build(self.root / "output.pac")
        self.assertEqual(self.workspace.unbuilt_entry_names(), ["a.tbl"])

    def test_missing_or_replaced_export_does_not_clear_pending_edits(self):
        output = self.root / "output.pac"
        self.workspace.build(output)
        output.unlink()
        self.assertEqual(self.workspace.unbuilt_entry_names(), ["a.tbl"])
        self.workspace.build(output)
        output.write_bytes(b"replaced")
        self.assertEqual(self.workspace.unbuilt_entry_names(), ["a.tbl"])

    def test_export_state_survives_reopen_and_removed_addition_is_a_new_change(self):
        addition = self.root / "add.dat"
        addition.write_bytes(b"new")
        self.workspace.insert_files({"add.dat": addition})
        self.workspace.build(self.root / "output.pac")
        self.workspace.close()
        self.workspace = self.manager.open(self.source)
        self.assertEqual(self.workspace.unbuilt_entry_names(), [])
        self.workspace.discard_entry("add.dat")
        self.assertEqual(self.workspace.unbuilt_entry_names(), ["add.dat"])


class RepairOptimizationTests(unittest.TestCase):
    def test_identical_payload_is_still_parsed_but_only_once(self):
        import retext.archive.repair as repair
        with patch.object(repair, "parse_dat_references", wraps=repair.parse_dat_references) as parse:
            with patch.object(repair, "repair_dat_references_from_reference") as transform:
                result = _analyze_dat_payload(_minimal_exact_dat(), logical_path="a.dat",
                                              reference_payload=_minimal_exact_dat())
        self.assertEqual(result.status, "clean")
        self.assertEqual(parse.call_count, 1)
        transform.assert_not_called()
        with self.assertRaises(ValueError):
            _analyze_dat_payload(b"#scp-broken", logical_path="a.dat", reference_payload=b"#scp-broken")

    def test_pac_scan_reports_progress_without_extracting_temporary_files(self):
        with tempfile.TemporaryDirectory() as folder:
            pac = Path(folder) / "source.pac"
            pac.write_bytes(_make_pac([("a.dat", _minimal_exact_dat()), ("not.dat", b"not-scp")]))
            service = PacDatReferenceRepairService()
            progress = Mock()
            with patch.object(service.archive_service, "extract_entry", side_effect=AssertionError("unexpected disk roundtrip")):
                result = service.scan(pac, pac, progress=progress)
            self.assertEqual(result.parsed_dat_count, 1)
            self.assertEqual(progress.call_count, 2)
            self.assertEqual(progress.call_args.args[:2], (2, 2))

    def test_read_entry_bytes_rejects_silent_payload_change(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.pac"
            source.write_bytes(_make_pac([("a.bin", b"original")]))
            service = FpacArchiveService()
            archive = service.inspect(source)
            raw = source.read_bytes().replace(b"original", b"tampered")
            source.write_bytes(raw)
            os.utime(source, ns=(archive.source_mtime_ns, archive.source_mtime_ns))
            with self.assertRaisesRegex(RuntimeError, "verification"):
                service.read_entry_bytes(archive, "a.bin")


class PresentationAndBatchCloseTests(unittest.TestCase):
    def test_unchecked_background_is_white_but_capability_label_is_retained(self):
        hit = SimpleNamespace(checked=True, pair_old="旧", pair_new="新", writable=True, write_mode="slot")
        self.assertNotEqual(hit_row_presentation(hit).background, "#ffffff")
        hit.checked = False
        self.assertEqual(hit_row_presentation(hit).background, "#ffffff")
        self.assertEqual(hit_row_presentation(hit).label, hit_presentation(hit).label)

    def test_diff_spans_locate_actual_changed_characters_and_long_text(self):
        self.assertEqual(difference_spans("约书亚", "约修亚"), ((1, 2), (1, 2)))
        self.assertEqual(difference_spans("相同", "相同"), ((0, 0), (0, 0)))
        self.assertEqual(difference_spans("", "新增"), ((0, 0), (0, 2)))
        prefix = "利贝尔通信" * 20000
        self.assertEqual(difference_spans(prefix + "旧尾", prefix + "新尾"),
                         ((len(prefix), len(prefix) + 1), (len(prefix), len(prefix) + 1)))

    def test_batch_close_releases_results_keeps_mappings_and_does_not_touch_workspace(self):
        app = RetextTkApp.__new__(RetextTkApp)
        app._ensure_idle = lambda: True
        app.batch_mapping = object()
        mapping = app.batch_mapping
        app.batch_scan_complete = True
        app.batch_hits = [object()]
        app.batch_target_by_file = {"cached": object()}
        app.batch_targets = [object()]
        app.batch_behavior_operations = [{"checked": False}]
        app._refresh_hit_tree = Mock()
        app.batch_hit_tree = Mock()
        app._set_text = Mock()
        app.batch_log = Mock()
        app._reset_task_progress = Mock()
        app.batch_progress = Mock()
        app.batch_progress_text_var = Mock()
        app.nb = Mock()
        app.preview_tab = object()
        app.status_var = Mock()
        app.workbench = Mock()
        app.close_batch()
        self.assertEqual(app.batch_hits, [])
        self.assertEqual(app.batch_targets, [])
        self.assertEqual(app.batch_behavior_operations, [])
        self.assertFalse(app.batch_scan_complete)
        self.assertIs(app.batch_mapping, mapping)
        self.assertEqual(app.workbench.mock_calls, [])
        app.nb.select.assert_called_once_with(app.preview_tab)


if __name__ == "__main__":
    unittest.main()
