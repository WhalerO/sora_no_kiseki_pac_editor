from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tk_gui import RetextTkApp


class SearchNavigationTests(unittest.TestCase):
    def test_unified_search_dispatches_shared_direction_by_selected_mode(self):
        app = RetextTkApp.__new__(RetextTkApp)
        app.resource_search_mode_var = Mock()
        app.current_resource_mode = lambda: "pac"
        app.find_next_pac_file = Mock()
        app.find_content = Mock()
        app.resource_search_mode_var.get.return_value = "文件名"
        app.find_resource(-1)
        app.find_next_pac_file.assert_called_once_with(direction=-1)
        app.find_content.assert_not_called()
        app.resource_search_mode_var.get.return_value = "正文"
        app.find_resource(1)
        app.find_content.assert_called_once_with(1)

    def test_content_navigation_wraps_both_directions_without_rescan(self):
        app = RetextTkApp.__new__(RetextTkApp)
        app._content_hits = ["first", "second"]
        app._content_cursor = -1
        app.content_status_var = Mock()
        app._open_text_hit = Mock()
        for direction in (1, 1, 1, -1):
            app._navigate_content(direction)
        self.assertEqual([call.args[0] for call in app._open_text_hit.call_args_list], ["first", "second", "first", "second"])
        self.assertTrue(all(call.kwargs == {"edit": False} for call in app._open_text_hit.call_args_list))

    def test_batch_text_columns_open_but_checkbox_does_not(self):
        app = RetextTkApp.__new__(RetextTkApp)
        app.batch_hit_tree = SimpleNamespace(identify_row=lambda y: "0", identify_column=lambda x: x)
        app._open_batch_hit = Mock(return_value="break")
        for column in ("#1", "#2", "#3", "#4"):
            self.assertEqual(app._on_batch_hit_double_click(SimpleNamespace(x=column, y=1)), "break")
        self.assertIsNone(app._on_batch_hit_double_click(SimpleNamespace(x="#0", y=1)))
        self.assertEqual(app._open_batch_hit.call_count, 4)

    def test_batch_navigation_preserves_specific_occurrence(self):
        app = RetextTkApp.__new__(RetextTkApp)
        app._ensure_idle = lambda: True
        app.batch_hits = [SimpleNamespace(file="test.tbl", original_text="目标前文目标后文", match_start=4, match_end=6,
                                         unit_index=1, source_id="", logical_file="test.tbl")]
        app.batch_target_by_file = {}
        app.current_resource_mode = lambda: "unpacked"
        app._open_unpacked_file = Mock()
        app.current_origin = "unpacked"
        app.current_file = Path("test.tbl").resolve()
        app._select_single_unit = Mock(return_value=True)
        app.single_tree = SimpleNamespace(selection=lambda: ("row",))
        app.session = SimpleNamespace(document=SimpleNamespace(get_unit=lambda index: SimpleNamespace(current_text="目标前文目标后文")))
        app._show_single_match = Mock()
        app._begin_single_edit = Mock()
        self.assertEqual(app._open_batch_hit("0"), "break")
        app._show_single_match.assert_called_once_with("row", (4, 6))
        app._begin_single_edit.assert_called_once_with("row")

    def test_tree_restore_mutes_queued_selection_events_until_idle(self):
        app = RetextTkApp.__new__(RetextTkApp)
        callbacks = []
        app.root = SimpleNamespace(after_idle=callbacks.append)
        app.pac_tree_refs = {}
        app.pac_tree = Mock()
        app.pac_tree.selection.return_value = ()
        app.pac_tree.get_children.return_value = ()
        app.pac_tree.yview.return_value = (0, 1)
        app._reset_pac_search_cycle = Mock()
        app.workbench = SimpleNamespace(projects=lambda: [])
        app.pac_meta_var = Mock()
        app.preview_selection = None
        app._render_preview_message = Mock()
        app.refresh_pac_tree()
        self.assertTrue(app._suspend_tree_preview)
        for callback in callbacks:
            callback()
        self.assertFalse(app._suspend_tree_preview)


if __name__ == "__main__":
    unittest.main()
