from pathlib import Path
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import Mock

from retext.archive.collection import PacNodeRef
from retext.pac_files_view import PacFilesView


class PacFilesViewTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.records = {"table/a.tbl": 21, "table/sub/b.dat": 42}
        workspace = SimpleNamespace(workspace_id="one", archive=SimpleNamespace(source_path=Path("one.pac")),
            entries=lambda: [SimpleNamespace(name=name) for name in self.records],
            current_size=self.records.__getitem__, entry_state=lambda name: "clean")
        self.projects = [SimpleNamespace(workspace=workspace)]
        workbench = SimpleNamespace(projects=lambda: self.projects, get=lambda _: self.projects[0])
        self.open_file = Mock()
        self.extract, self.replace, self.insert, self.export = Mock(), Mock(), Mock(), Mock()
        self.view = PacFilesView(self.root, workbench=workbench, open_pacs=Mock(),
            extract=self.extract, replace=self.replace, insert=self.insert, export=self.export,
            open_file=self.open_file, format_size=str, state_label=str, ensure_idle=lambda: True)

    def tearDown(self):
        self.root.destroy()

    def test_navigation_and_current_folder_insertion_are_independent_of_left_tree(self):
        self.view.show_ref(PacNodeRef("one", "pac"))
        item = next(i for i, ref in self.view.refs.items() if ref.path == "table")
        self.view.tree.selection_set(item)
        self.view._activate()
        self.assertEqual(self.view.folder, "table")
        self.assertEqual(self.view.folder_refs(), [PacNodeRef("one", "folder", "table")])
        file_ref = PacNodeRef("one", "file", "table/a.tbl")
        self.view.show_ref(file_ref)
        self.assertEqual(self.view.selected_refs(), [file_ref])
        self.view._activate()
        self.open_file.assert_called_once_with(file_ref)
        self.view.up()
        self.assertEqual(self.view.folder, "")

    def test_refresh_includes_inserted_files_and_clears_closed_pacs(self):
        self.view.show_ref(PacNodeRef("one", "folder", "table"))
        self.records["table/new.tbl"] = 99
        self.view.refresh()
        self.assertIn("table/new.tbl", [ref.path for ref in self.view.refs.values()])
        self.projects.clear()
        self.view.refresh()
        self.assertEqual(self.view.folder_refs(), [])
        self.assertEqual(self.view.selected_refs(), [])
        self.assertEqual(self.view.pac_var.get(), "")


if __name__ == "__main__":
    unittest.main()
