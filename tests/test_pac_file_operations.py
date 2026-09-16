from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from retext.archive import FpacArchiveService, PacNodeRef, PacWorkbench, PacWorkspaceManager
from tests.test_pac_archive import _make_pac


class PacFileOperationTests(unittest.TestCase):
    def test_open_added_document_remains_editable_after_cache_order_rebase(self):
        from tests.test_regressions import minimal_exact_dat
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.pac"
            source.write_bytes(_make_pac([("keep.bin", b"original")]))
            payload = root / "new.dat"
            payload.write_bytes(minimal_exact_dat())
            workbench = PacWorkbench(PacWorkspaceManager(workspaces_root=root / "ws", trash_root=root / "trash"))
            project = workbench.open(source)
            project.workspace.insert_files({"a.dat": payload, "b.dat": payload})
            project.open_entry("b.dat")
            project.workspace.discard_entry("a.dat")
            project.build(source)
            session = project.document_session
            unit = next(u for u in session.document.units if u.current_text == "阵")
            session.update_unit(unit.index, "新增脚本再次编辑")
            project.save_current()
            report = project.build(root / "edited.pac")
            self.assertEqual(report.changed_entries, ("b.dat",))
            with self.assertRaisesRegex(ValueError, "源 PAC"):
                project.export_current(source)
            workbench.close_all()

    def test_cancelled_addition_gap_rebases_text_cache_and_supports_empty_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.pac"
            source.write_bytes(_make_pac([("keep.bin", b"original")]))
            a, b, empty = root / "a.tbl", root / "b.tbl", root / "empty.bin"
            a.write_bytes(b"first")
            b.write_bytes(b"second")
            empty.touch()
            manager = PacWorkspaceManager(workspaces_root=root / "ws", trash_root=root / "trash")
            workspace = manager.open(source)
            workspace.insert_files({"a.tbl": a, "b.tbl": b, "empty.bin": empty, "empty2.bin": empty})
            workspace.discard_entry("a.tbl")
            workspace.build(source)
            self.assertEqual(workspace.materialize("b.tbl").read_bytes(), b"second")
            self.assertEqual(workspace.materialize("empty.bin").read_bytes(), b"")
            self.assertEqual(workspace.dirty_entry_names(), [])
            workspace.close()
            workspace = manager.open(source)
            self.assertEqual(workspace.materialize("b.tbl").read_bytes(), b"second")
            workspace.close()

    def test_insert_failure_keeps_manifest_unchanged_and_export_collision_is_preflighted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.pac"
            source.write_bytes(_make_pac([("data/item.bin", b"original")]))
            payload = root / "new.bin"
            payload.write_bytes(b"new")
            manager = PacWorkspaceManager(workspaces_root=root / "ws", trash_root=root / "trash")
            workbench = PacWorkbench(manager)
            workspace = workbench.open(source).workspace
            manifest = (workspace.root / "manifest.json").read_bytes()
            from retext.io_utils import atomic_copy_file
            calls = 0
            def fail_second(src, dst):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated disk error")
                return atomic_copy_file(src, dst)
            with patch("retext.archive.workspace.atomic_copy_file", side_effect=fail_second):
                with self.assertRaises(OSError):
                    workspace.insert_files({"a.bin": payload, "b.bin": payload})
            self.assertEqual((workspace.root / "manifest.json").read_bytes(), manifest)
            self.assertEqual(len(workspace.entries()), 1)
            self.assertEqual(workspace.dirty_entry_names(), [])
            workspace.insert_files({"a.bin": payload, "b.bin": payload})
            output = root / "export"
            output.mkdir()
            (output / "b.bin").write_bytes(b"do not overwrite")
            with self.assertRaises(FileExistsError):
                workbench.export_refs([PacNodeRef(workspace.workspace_id, "pac")], output)
            self.assertFalse((output / "a.bin").exists())
            self.assertEqual((output / "b.bin").read_bytes(), b"do not overwrite")
            workbench.close_all()

    def test_binary_replace_insert_reopen_export_and_build_preserve_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "image.pac"
            original = _make_pac([("image/keep.bin", b"keep"), ("image/change.dds", b"old")])
            source.write_bytes(original)
            replacement = root / "external.dds"
            replacement.write_bytes(b"replacement")
            manager = PacWorkspaceManager(workspaces_root=root / "workspaces", trash_root=root / "trash")
            workbench = PacWorkbench(manager)
            project = workbench.open(source)
            workspace = project.workspace
            workspace.import_file("image/change.dds", replacement, replace_existing=True)
            self.assertEqual(workspace.current_size("image/change.dds"), len(b"replacement"))
            workspace.import_file("image/new.dds", replacement)
            replacement.write_bytes(b"external was edited later")
            workspace_id = workspace.workspace_id
            workbench.close_all()
            self.assertEqual(manager.list_summaries()[0].dirty_count, 2)
            project = workbench.open(source)
            workspace = project.workspace
            self.assertEqual(workspace.entry_state("image/new.dds"), "added")
            self.assertEqual(workspace.read_entry_prefix("image/change.dds"), b"replacement")
            outputs = workbench.export_refs([PacNodeRef(workspace_id, "folder", "image")], root / "extracted")
            self.assertEqual(len(outputs), 3)
            self.assertEqual((root / "extracted/image/new.dds").read_bytes(), b"replacement")
            report = project.build(root / "rebuilt.pac")
            self.assertEqual(report.entry_count, 3)
            archive = report.verified_archive
            self.assertEqual(archive.get_entry("image/new.dds").sha256, archive.get_entry("image/change.dds").sha256)
            self.assertEqual(source.read_bytes(), original)
            # In-place rebuild rebases the current workspace, including new entries.
            project.build(source)
            self.assertEqual(workspace.dirty_entry_names(), [])
            self.assertEqual(workspace.materialize("image/new.dds").read_bytes(), b"replacement")
            workbench.close_all()

    def test_duplicate_and_unsafe_insert_do_not_modify_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.pac"
            source.write_bytes(_make_pac([("data/item.bin", b"original")]))
            payload = root / "new.bin"
            payload.write_bytes(b"new")
            manager = PacWorkspaceManager(workspaces_root=root / "workspaces", trash_root=root / "trash")
            workspace = manager.open(source)
            for name in ["data/item.bin", "DATA/ITEM.BIN", "../escape.bin", "data//x.bin", "data/./x.bin", "/x.bin",
                         "data", "data/item.bin/nested.bin", "CON.txt", "NUL", "file.", "file ", "x?.txt"]:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    workspace.import_file(name, payload)
            self.assertEqual(workspace.dirty_entry_names(), [])
            self.assertEqual(len(workspace.entries()), 1)
            workspace.close()

    def test_added_missing_payload_is_never_treated_as_clean_or_read_from_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.pac"
            source.write_bytes(_make_pac([("data/item.bin", b"original")]))
            payload = root / "new.bin"
            payload.write_bytes(b"new")
            manager = PacWorkspaceManager(workspaces_root=root / "workspaces", trash_root=root / "trash")
            workspace = manager.open(source)
            workspace.import_file("data/new.bin", payload)
            workspace.entry_path("data/new.bin").unlink()
            with self.assertRaises(RuntimeError):
                workspace.materialize("data/new.bin")
            with self.assertRaises(RuntimeError):
                workspace.export_entry("data/new.bin", root / "missing.bin")
            with self.assertRaises(FileNotFoundError):
                workspace.build(root / "out.pac")
            self.assertEqual(manager.list_summaries()[0].dirty_count, 1)
            workspace.close()


if __name__ == "__main__":
    unittest.main()
