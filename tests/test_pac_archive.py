from __future__ import annotations

import hashlib
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from retext.archive import (
    DatReferenceRepairService,
    FpacArchiveService,
    FpacFormatError,
    PacComparisonSession,
    PacDatReferenceRepairService,
    PacFallbackTools,
    PacNodeRef,
    PacProjectSession,
    PacWorkbench,
    PacWorkspaceError,
    PacWorkspaceManager,
)
from retext.archive.workspace import _workspace_id
from retext.core import RetextService
from retext.domain import WorkflowMode
from retext.engines.relocation import parse_dat_references
from retext.paths import REPO_ROOT
from retext.session import SessionOptions
from tests.corpus import CORPUS_ROOT


class FpacServiceTests(unittest.TestCase):
    def test_single_dat_scan_distinguishes_silent_mislink_and_repairs_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            reference = root / "reference.dat"
            damaged = root / "damaged.dat"
            output = root / "repaired.dat"
            reference_payload = _minimal_exact_dat()
            damaged_payload = bytearray(reference_payload)
            damaged_payload[67:67] = b"X"
            reference.write_bytes(reference_payload)
            damaged.write_bytes(damaged_payload)
            service = DatReferenceRepairService()

            unreferenced = service.scan(damaged)
            compared = service.scan(damaged, reference)
            report = service.repair(reference, damaged, output, do_backup=False)

            self.assertEqual(unreferenced.status, "clean-uncompared")
            self.assertIsNone(unreferenced.silent_pointer_count)
            self.assertEqual(compared.status, "repairable")
            self.assertEqual(compared.invalid_pointer_count, 0)
            self.assertEqual(compared.corrected_pointer_count, 1)
            self.assertEqual(compared.silent_pointer_count, 1)
            self.assertEqual(report.output_path, output.resolve())
            layout = parse_dat_references(output.read_bytes())
            self.assertEqual(
                sorted(item.text for item in layout.strings.values() if item.raw),
                ["funcX", "阵"],
            )

    def test_pac_integrity_scan_supports_unreferenced_and_reference_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            reference_payload = _minimal_exact_dat()
            damaged_payload = bytearray(reference_payload)
            damaged_payload[67:67] = b"X"
            reference_pac = root / "reference.pac"
            damaged_pac = root / "damaged.pac"
            entry_name = "script_sc/test.dat"
            reference_pac.write_bytes(_make_pac([(entry_name, reference_payload)]))
            damaged_pac.write_bytes(_make_pac([(entry_name, bytes(damaged_payload))]))
            service = PacDatReferenceRepairService()

            unreferenced = service.scan(damaged_pac)
            compared = service.scan(damaged_pac, reference_pac)

            self.assertTrue(unreferenced.complete)
            self.assertEqual(unreferenced.requested_dat_count, 1)
            self.assertEqual(unreferenced.items[0].status, "clean-uncompared")
            self.assertIsNone(unreferenced.silent_pointer_count)
            self.assertTrue(compared.complete)
            self.assertEqual(compared.corrected_pointer_count, 1)
            self.assertEqual(compared.silent_pointer_count, 1)
            self.assertEqual(compared.items[0].status, "repairable")

    def test_pac_integrity_scan_reports_one_bad_entry_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            valid = _minimal_exact_dat()
            reference_pac = root / "reference.pac"
            damaged_pac = root / "damaged.pac"
            reference_pac.write_bytes(
                _make_pac(
                    [
                        ("script_sc/good.dat", valid),
                        ("script_sc/bad.dat", valid),
                    ]
                )
            )
            damaged_pac.write_bytes(
                _make_pac(
                    [
                        ("script_sc/good.dat", valid),
                        ("script_sc/bad.dat", b"#scp" + b"\0" * 20),
                    ]
                )
            )

            report = PacDatReferenceRepairService().scan(
                damaged_pac,
                reference_pac,
            )

            self.assertFalse(report.complete)
            self.assertEqual(report.requested_dat_count, 2)
            self.assertEqual(report.parsed_dat_count, 1)
            by_name = {item.logical_path: item for item in report.items}
            self.assertEqual(by_name["script_sc/good.dat"].status, "clean")
            self.assertEqual(by_name["script_sc/bad.dat"].status, "incompatible")

    def test_reference_repair_fixes_silent_dat_mislink_without_changing_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            reference_payload = _minimal_exact_dat()
            damaged_payload = bytearray(reference_payload)
            damaged_payload[67:67] = b"X"
            reference_pac = root / "reference.pac"
            damaged_pac = root / "damaged.pac"
            output_pac = root / "repaired.pac"
            entry_name = "script_sc/test.dat"
            reference_pac.write_bytes(_make_pac([(entry_name, reference_payload)]))
            damaged_pac.write_bytes(_make_pac([(entry_name, bytes(damaged_payload))]))

            report = PacDatReferenceRepairService().repair(
                reference_pac,
                damaged_pac,
                output_pac,
            )

            self.assertEqual(report.repaired_dat_count, 1)
            self.assertEqual(report.corrected_pointer_count, 1)
            service = FpacArchiveService()
            archive = service.inspect(output_pac)
            extracted = root / "repaired.dat"
            service.extract_entry(archive, entry_name, extracted)
            repaired = extracted.read_bytes()
            layout = parse_dat_references(repaired)
            self.assertEqual(
                int.from_bytes(repaired[58:62], "little") & 0x3FFFFFFF,
                69,
            )
            self.assertEqual(
                sorted(item.text for item in layout.strings.values() if item.raw),
                ["funcX", "阵"],
            )

    def test_pac_reference_repair_continues_after_an_incompatible_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            reference_payload = _minimal_exact_dat()
            damaged_payload = bytearray(reference_payload)
            damaged_payload[67:67] = b"X"
            reference_pac = root / "reference.pac"
            damaged_pac = root / "damaged.pac"
            output_pac = root / "partially-repaired.pac"
            repaired_name = "script_sc/repaired.dat"
            missing_name = "script_sc/missing-from-reference.dat"
            reference_pac.write_bytes(
                _make_pac([(repaired_name, reference_payload)])
            )
            damaged_pac.write_bytes(
                _make_pac(
                    [
                        (repaired_name, bytes(damaged_payload)),
                        (missing_name, reference_payload),
                    ]
                )
            )

            report = PacDatReferenceRepairService().repair(
                reference_pac,
                damaged_pac,
                output_pac,
            )

            self.assertFalse(report.complete)
            self.assertEqual(report.repaired_entries, (repaired_name,))
            self.assertEqual(report.failed_entries[0][0], missing_name)
            self.assertIn("缺少同路径", report.failed_entries[0][1])
            rebuilt = FpacArchiveService().inspect(output_pac)
            preserved = root / "preserved.dat"
            FpacArchiveService().extract_entry(rebuilt, missing_name, preserved)
            self.assertEqual(preserved.read_bytes(), reference_payload)

    def test_workbench_open_rotates_changed_source_to_new_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"old")]))
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workbench = PacWorkbench(manager)
            first = workbench.open(source)
            old_id = first.workspace.workspace_id
            old_root = first.workspace.root

            source.write_bytes(_make_pac([("table_sc/a.tbl", b"new")]))
            second = workbench.open(source)

            self.assertNotEqual(second.workspace.workspace_id, old_id)
            self.assertTrue(old_root.exists())
            self.assertEqual(len(workbench.projects()), 1)
            self.assertEqual(
                second.workspace.materialize("table_sc/a.tbl").read_bytes(),
                b"new",
            )
            second.close()

    def test_ephemeral_workspace_is_destroyed_when_pac_closes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"old")]))
            manager = PacWorkspaceManager(
                workspaces_root=root / "session" / "workspaces",
                trash_root=root / "session" / "trash",
                ephemeral=True,
            )
            workspace = manager.open(source)
            workspace_root = workspace.root
            cached = workspace.materialize("table_sc/a.tbl")
            cached.write_bytes(b"unsaved session edit")
            workspace.refresh_entry("table_sc/a.tbl")

            workspace.close()

            self.assertFalse(workspace_root.exists())
            self.assertEqual(manager.list_summaries(), [])
            self.assertEqual(list(manager.trash_root.iterdir()), [])

            reopened = manager.open(source)
            self.assertEqual(
                reopened.materialize("table_sc/a.tbl").read_bytes(),
                b"old",
            )
            reopened.close()

    def test_ephemeral_workbench_reload_reextracts_without_delete_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"old")]))
            manager = PacWorkspaceManager(
                workspaces_root=root / "session" / "workspaces",
                trash_root=root / "session" / "trash",
                ephemeral=True,
            )
            workbench = PacWorkbench(manager)
            first = workbench.open(source)
            workspace_id = first.workspace.workspace_id
            first.workspace.materialize("table_sc/a.tbl").write_bytes(b"dirty")
            first.workspace.refresh_entry("table_sc/a.tbl")

            reopened = workbench.reload_from_source(
                workspace_id,
                discard_cache=True,
            )

            self.assertEqual(
                reopened.workspace.materialize("table_sc/a.tbl").read_bytes(),
                b"old",
            )
            workbench.close_all()

    def test_manifest_collision_allocates_recoverable_alternate_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"old")]))
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            old = manager.open(source)
            old_root = old.root
            old.close()

            source.write_bytes(_make_pac([("table_sc/a.tbl", b"new")]))
            current_archive = manager.service.inspect(source)
            conflicting_root = manager.workspaces_root / _workspace_id(current_archive)
            old_root.rename(conflicting_root)

            current = manager.open(source)

            self.assertNotEqual(current.root, conflicting_root)
            self.assertTrue(conflicting_root.exists())
            self.assertTrue(current.root.exists())
            self.assertEqual(
                current.materialize("table_sc/a.tbl").read_bytes(),
                b"new",
            )
            current.close()

    def test_manager_can_clear_all_closed_generations_by_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"old")]))
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            first = manager.open(source)
            first.close()
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"new")]))
            second = manager.open(source)
            second.close()

            self.assertEqual(len(manager.summaries_for_source(source)), 2)
            removed = manager.delete_for_source(source, allow_dirty=True)

            self.assertEqual(len(removed), 2)
            self.assertEqual(manager.summaries_for_source(source), [])
            self.assertEqual(len(list((root / "trash").iterdir())), 2)

    def test_build_rejects_preexisting_broken_scp_dat_outside_edit_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "broken.pac"
            source.write_bytes(
                _make_pac(
                    [
                        ("script_sc/broken.dat", b"#scp" + b"\0" * 20),
                        ("table_sc/edited.tbl", b"ordinary test payload"),
                    ]
                )
            )
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workspace = manager.open(source)
            edited = workspace.materialize("table_sc/edited.tbl")
            edited.write_bytes(b"changed ordinary payload")
            workspace.refresh_entry("table_sc/edited.tbl")
            output = root / "blocked.pac"

            with self.assertRaisesRegex(PacWorkspaceError, "完整性检查失败"):
                workspace.build(output)

            self.assertFalse(output.exists())
            workspace.close()

    def test_build_can_explicitly_export_with_integrity_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "broken.pac"
            broken = b"#scp" + b"\0" * 20
            source.write_bytes(
                _make_pac(
                    [
                        ("script_sc/broken.dat", broken),
                        ("table_sc/edited.tbl", b"ordinary test payload"),
                    ]
                )
            )
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workspace = manager.open(source)
            edited = workspace.materialize("table_sc/edited.tbl")
            edited.write_bytes(b"changed ordinary payload")
            workspace.refresh_entry("table_sc/edited.tbl")
            output = root / "warned.pac"

            report = workspace.build(
                output,
                allow_integrity_errors=True,
            )

            self.assertTrue(output.is_file())
            self.assertEqual(report.changed_entries, ("table_sc/edited.tbl",))
            self.assertTrue(
                workspace.manifest["outputs"][-1]["integrity_warnings"]
            )
            extracted = root / "broken.dat"
            manager.service.extract_entry(
                manager.service.inspect(output),
                "script_sc/broken.dat",
                extracted,
            )
            self.assertEqual(extracted.read_bytes(), broken)
            workspace.close()

    def test_workbench_can_update_or_clear_a_selected_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"old")]))
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workbench = PacWorkbench(manager)
            first = workbench.open(source)
            old_id = first.workspace.workspace_id
            old_root = first.workspace.root
            cached = first.workspace.materialize("table_sc/a.tbl")
            cached.write_bytes(b"dirty")
            first.workspace.refresh_entry("table_sc/a.tbl")

            source.write_bytes(_make_pac([("table_sc/a.tbl", b"new source")]))
            updated = workbench.reload_from_source(old_id)
            updated_id = updated.workspace.workspace_id

            self.assertNotEqual(updated_id, old_id)
            self.assertTrue(old_root.exists())
            self.assertEqual(
                updated.workspace.materialize("table_sc/a.tbl").read_bytes(),
                b"new source",
            )

            refreshed_root = updated.workspace.root
            clean = workbench.reload_from_source(updated_id, discard_cache=True)
            self.assertEqual(clean.workspace.workspace_id, updated_id)
            self.assertTrue(refreshed_root.exists())
            self.assertTrue(any((root / "trash").iterdir()))
            clean.close()

    def test_reopening_an_unchanged_pac_revalidates_and_reuses_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"table")]))
            service = FpacArchiveService()
            manager = PacWorkspaceManager(
                service,
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )

            with patch.object(service, "inspect", wraps=service.inspect) as inspect:
                first = manager.open(source)
                second = manager.open(source)

            self.assertIs(first, second)
            self.assertEqual(inspect.call_count, 2)
            first.close()

    def test_reopening_rejects_same_size_and_mtime_payload_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"alpha")]))
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workspace = manager.open(source)
            original_stat = source.stat()
            changed = bytearray(source.read_bytes())
            changed[-1] ^= 0x01
            source.write_bytes(changed)
            os.utime(
                source,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )

            with self.assertRaisesRegex(PacWorkspaceError, "磁盘内容已经变化"):
                manager.open(source)
            workspace.close()

    def test_pac_group_comparison_pairs_relative_archive_paths_case_insensitively(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            old_group = root / "old"
            new_group = root / "new"
            (old_group / "Arc").mkdir(parents=True)
            (new_group / "arc").mkdir(parents=True)
            (old_group / "Arc" / "common.pac").write_bytes(
                _make_pac([("table_sc/a.tbl", b"old")])
            )
            (new_group / "arc" / "common.pac").write_bytes(
                _make_pac([("table_sc/a.tbl", b"new")])
            )
            (old_group / "retired.pac").write_bytes(
                _make_pac([("table_sc/old.tbl", b"old-only")])
            )
            (new_group / "added.pac").write_bytes(
                _make_pac([("table_sc/new.tbl", b"new-only")])
            )

            session = PacComparisonSession(staging_root=root / "staging")
            rows = session.compare([old_group], [new_group])
            self.assertEqual(
                {(row.rel, row.status) for row in rows},
                {
                    ("added.pac :: table_sc/new.tbl", "only_new"),
                    ("arc/common.pac :: table_sc/a.tbl", "modified"),
                    ("retired.pac :: table_sc/old.tbl", "only_old"),
                },
            )
            old_path, new_path, logical_path = session.materialize_row(
                next(row for row in rows if row.status == "only_new")
            )
            self.assertIsNone(old_path)
            self.assertEqual(Path(new_path).read_bytes(), b"new-only")
            self.assertEqual(logical_path, "table_sc/new.tbl")
            session.close()

    def test_independent_pac_comparison_is_lazy_and_uses_app_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            old_pac = root / "old.pac"
            new_pac = root / "new.pac"
            old_pac.write_bytes(
                _make_pac(
                    [
                        ("table_sc/a.tbl", b"old"),
                        ("asset.bin", b"same"),
                    ]
                )
            )
            new_pac.write_bytes(
                _make_pac(
                    [
                        ("table_sc/a.tbl", b"new"),
                        ("asset.bin", b"same"),
                    ]
                )
            )
            session = PacComparisonSession(staging_root=root / "staging")
            rows = session.compare([old_pac], [new_pac])
            self.assertEqual(
                [(row.rel, row.status) for row in rows],
                [("asset.bin", "same"), ("table_sc/a.tbl", "modified")],
            )
            self.assertFalse(session.root.exists())

            modified = rows[1]
            old_path, new_path, logical_path = session.materialize_row(modified)
            self.assertEqual(logical_path, "table_sc/a.tbl")
            self.assertEqual(Path(old_path).read_bytes(), b"old")
            self.assertEqual(Path(new_path).read_bytes(), b"new")
            self.assertEqual(Path(old_path).parents[2], session.root)
            session.close()
            self.assertFalse(session.root.exists())

    def test_workbench_opens_multiple_pacs_and_expands_tree_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            first = root / "first.pac"
            second = root / "second.pac"
            first.write_bytes(
                _make_pac(
                    [
                        ("table_sc/items/a.tbl", b"alpha"),
                        ("table_sc/items/readme.txt", b"notes"),
                        ("script_sc/event.dat", b"event"),
                    ]
                )
            )
            second.write_bytes(_make_pac([("table_sc/b.tbl", b"beta")]))
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workbench = PacWorkbench(manager)
            first_project = workbench.open(first)
            second_project = workbench.open(second)

            self.assertEqual(len(workbench.projects()), 2)
            folder = PacNodeRef(
                first_project.workspace.workspace_id,
                "folder",
                "table_sc/items",
            )
            expanded = workbench.expand_refs([folder], editable_only=False)
            self.assertEqual(
                {entry.name for _project, entry in expanded},
                {
                    "table_sc/items/a.tbl",
                    "table_sc/items/readme.txt",
                },
            )
            materialized = workbench.materialize_refs([folder])
            self.assertEqual(
                [item.logical_path for item in materialized],
                ["table_sc/items/a.tbl"],
            )
            self.assertTrue(materialized[0].path.is_file())
            missing_file = PacNodeRef(
                first_project.workspace.workspace_id,
                "file",
                "table_sc/items/a.tbl",
            )
            self.assertEqual(
                workbench.mirror_refs(
                    second_project.workspace.workspace_id,
                    [folder, missing_file],
                ),
                [
                    PacNodeRef(
                        second_project.workspace.workspace_id,
                        "folder",
                        "table_sc/items",
                    )
                ],
            )
            workbench.close(second_project.workspace.workspace_id)
            self.assertEqual(len(workbench.projects()), 1)
            workbench.close_all()

    def test_strict_parser_and_noop_build_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(
                _make_pac(
                    [
                        ("table_sc/a.tbl", b"alpha"),
                        ("table_sc/b.dat", b"beta"),
                    ]
                )
            )
            service = FpacArchiveService()
            archive = service.inspect(source)
            self.assertEqual(
                archive.source_sha256,
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            expected_payloads = {
                "table_sc/a.tbl": b"alpha",
                "table_sc/b.dat": b"beta",
            }
            self.assertEqual(
                {
                    entry.name: entry.sha256
                    for entry in archive.entries
                },
                {
                    name: hashlib.sha256(payload).hexdigest()
                    for name, payload in expected_payloads.items()
                },
            )
            self.assertEqual([entry.name for entry in archive.editable_entries()], [
                "table_sc/b.dat",
                "table_sc/a.tbl",
            ])

            output = root / "copy.pac"
            report = service.build(archive, {}, output)
            self.assertEqual(output.read_bytes(), source.read_bytes())
            self.assertEqual(report.changed_entries, ())

    def test_entry_prefix_reads_without_materializing_the_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(
                _make_pac([("asset/image/title.png", b"0123456789")])
            )
            service = FpacArchiveService()
            archive = service.inspect(source)

            self.assertEqual(
                service.read_entry_prefix(
                    archive,
                    "asset/image/title.png",
                    4,
                ),
                b"0123",
            )
            with self.assertRaises(ValueError):
                service.read_entry_prefix(
                    archive,
                    "asset/image/title.png",
                    -1,
                )

    def test_non_text_preview_cache_is_read_only_and_never_repacked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            original = _make_pac(
                [
                    ("asset/image/title.png", b"original image"),
                    ("table_sc/a.tbl", b"table"),
                ]
            )
            source.write_bytes(original)
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workspace = manager.open(source)

            cached = workspace.materialize("asset/image/title.png")
            self.assertEqual(cached.parent, workspace.preview_root)
            cached.write_bytes(b"modified image")
            with self.assertRaisesRegex(PacWorkspaceError, "只读预览缓存"):
                workspace.refresh_entry("asset/image/title.png")

            output = root / "rebuilt.pac"
            report = workspace.build(output)
            self.assertEqual(report.changed_entries, ())
            self.assertEqual(output.read_bytes(), original)
            workspace.close()

    def test_workspace_entry_export_preserves_read_only_and_current_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(
                _make_pac(
                    [
                        ("asset/image/title.png", b"image payload"),
                        ("table_sc/a.tbl", b"original table"),
                    ]
                )
            )
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workspace = manager.open(source)

            image_target = root / "title.png"
            image_target.write_bytes(b"old output")
            image_output = workspace.export_entry(
                "asset/image/title.png",
                image_target,
            )
            self.assertEqual(image_output.read_bytes(), b"image payload")
            self.assertEqual(workspace.entry_state("asset/image/title.png"), "source")
            self.assertEqual(list(workspace.preview_root.iterdir()), [])

            cached = workspace.materialize("table_sc/a.tbl")
            cached.write_bytes(b"current table")
            workspace.refresh_entry("table_sc/a.tbl")
            current_output = workspace.export_entry(
                "table_sc/a.tbl",
                root / "current.tbl",
            )
            original_output = workspace.export_entry(
                "table_sc/a.tbl",
                root / "original.tbl",
                prefer_current=False,
            )
            self.assertEqual(current_output.read_bytes(), b"current table")
            self.assertEqual(original_output.read_bytes(), b"original table")

            with self.assertRaisesRegex(PacWorkspaceError, "不能覆盖源 PAC"):
                workspace.export_entry("table_sc/a.tbl", source)
            self.assertTrue(source.read_bytes().startswith(b"FPAC"))
            workspace.close()

    def test_low_level_extraction_cannot_overwrite_source_pac(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "sample.pac"
            original = _make_pac([("asset.bin", b"payload")])
            source.write_bytes(original)
            service = FpacArchiveService()
            archive = service.inspect(source)

            with self.assertRaisesRegex(ValueError, "cannot overwrite"):
                service.extract_entry(archive, "asset.bin", source)
            self.assertEqual(source.read_bytes(), original)

    def test_modified_payload_roundtrips_without_adding_auxiliary_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(
                _make_pac(
                    [
                        ("table_sc/a.tbl", b"alpha"),
                        ("table_sc/b.dat", b"beta"),
                    ]
                )
            )
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workspace = manager.open(source)
            materialized = workspace.materialize("table_sc/a.tbl")
            materialized.write_bytes(b"changed payload")
            (materialized.parent / "not-in-manifest.bak").write_bytes(b"must not be packed")
            self.assertTrue(workspace.refresh_entry("table_sc/a.tbl"))

            output = root / "changed.pac"
            report = workspace.build(output)
            rebuilt = FpacArchiveService().inspect(output)
            self.assertEqual({entry.name for entry in rebuilt.entries}, {
                "table_sc/a.tbl",
                "table_sc/b.dat",
            })
            self.assertEqual(report.changed_entries, ("table_sc/a.tbl",))
            extracted = root / "a.tbl"
            FpacArchiveService().extract_entry(rebuilt, "table_sc/a.tbl", extracted)
            self.assertEqual(extracted.read_bytes(), b"changed payload")
            workspace.close()

    def test_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "unsafe.pac"
            source.write_bytes(_make_pac([("../escape.tbl", b"payload")]))
            with self.assertRaises(FpacFormatError):
                FpacArchiveService().inspect(source)

    def test_stale_source_is_not_repacked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"alpha")]))
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workspace = manager.open(source)
            materialized = workspace.materialize("table_sc/a.tbl")
            materialized.write_bytes(b"changed")
            workspace.refresh_entry("table_sc/a.tbl")
            source.write_bytes(source.read_bytes() + b"\x00")
            with self.assertRaises(RuntimeError):
                workspace.build(root / "blocked.pac")
            self.assertFalse((root / "blocked.pac").exists())
            workspace.close()

    def test_dirty_workspace_is_protected_from_safe_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"alpha")]))
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workspace = manager.open(source)
            materialized = workspace.materialize("table_sc/a.tbl")
            materialized.write_bytes(b"changed")
            workspace.refresh_entry("table_sc/a.tbl")
            workspace_id = workspace.workspace_id
            workspace.close()

            self.assertEqual(manager.clean_safe(), [])
            with self.assertRaises(PacWorkspaceError):
                manager.delete(workspace_id)
            destination = manager.delete(workspace_id, allow_dirty=True)
            self.assertTrue(destination.exists())

    def test_safe_cleanup_moves_clean_workspace_to_recoverable_trash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"alpha")]))
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workspace = manager.open(source)
            original_root = workspace.root
            workspace.materialize("table_sc/a.tbl")
            workspace.close()

            removed = manager.clean_safe()
            self.assertEqual(len(removed), 1)
            self.assertFalse(original_root.exists())
            self.assertTrue((removed[0] / "manifest.json").is_file())
            self.assertFalse((removed[0] / "lock.json").exists())

    def test_external_cache_edit_is_detected_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"alpha")]))
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workspace = manager.open(source)
            cached = workspace.materialize("table_sc/a.tbl")
            workspace_id = workspace.workspace_id
            workspace.close()

            cached.write_bytes(b"edited outside the application")
            summary = manager.list_summaries()[0]
            self.assertEqual(summary.state, "dirty")
            self.assertEqual(summary.dirty_count, 1)
            self.assertEqual(manager.clean_safe(), [])
            with self.assertRaises(PacWorkspaceError):
                manager.delete(workspace_id)

    def test_live_workspace_lock_blocks_other_managers_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"alpha")]))
            manager_a = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workspace = manager_a.open(source)
            manager_b = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )

            self.assertEqual(manager_b.list_summaries()[0].state, "locked")
            self.assertEqual(manager_b.clean_safe(), [])
            with self.assertRaises(PacWorkspaceError):
                manager_b.open(source)
            with self.assertRaises(PacWorkspaceError):
                manager_b.delete(workspace.workspace_id)
            workspace.close()

    def test_identical_archives_at_different_paths_get_separate_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            payload = _make_pac([("table_sc/a.tbl", b"alpha")])
            source_a = root / "one" / "sample.pac"
            source_b = root / "two" / "sample.pac"
            source_a.parent.mkdir()
            source_b.parent.mkdir()
            source_a.write_bytes(payload)
            source_b.write_bytes(payload)
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workspace_a = manager.open(source_a)
            workspace_b = manager.open(source_b)
            self.assertNotEqual(workspace_a.workspace_id, workspace_b.workspace_id)
            workspace_a.close()
            workspace_b.close()

    def test_extract_detects_same_metadata_payload_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pac"
            source.write_bytes(_make_pac([("table_sc/a.tbl", b"alpha")]))
            service = FpacArchiveService()
            archive = service.inspect(source)
            original_stat = source.stat()
            changed = bytearray(source.read_bytes())
            changed[-1] ^= 0x01
            source.write_bytes(changed)
            source.touch()
            os.utime(
                source,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            output = root / "a.tbl"
            with self.assertRaises(RuntimeError):
                service.extract_entry(archive, "table_sc/a.tbl", output)
            self.assertFalse(output.exists())

    @unittest.skipUnless(
        (REPO_ROOT / "pac_tools" / "create_pac.py").exists(),
        "original PAC fallback scripts are not available",
    )
    def test_original_script_fallback_is_isolated_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            folder = root / "table_sc"
            folder.mkdir()
            (folder / "a.tbl").write_bytes(b"fallback payload")
            tools = PacFallbackTools(REPO_ROOT / "pac_tools")
            output = tools.build(folder, root / "fallback.pac")
            archive = FpacArchiveService().inspect(output)
            self.assertEqual([entry.name for entry in archive.entries], ["table_sc/a.tbl"])

            extracted_root = root / "extracted"
            extracted_root.mkdir()
            extracted = tools.extract(output, extracted_root)
            self.assertEqual(extracted, extracted_root / "table_sc")
            self.assertEqual(
                (extracted / "a.tbl").read_bytes(),
                b"fallback payload",
            )


@unittest.skipUnless(
    (CORPUS_ROOT / "table_sc.pac").exists(),
    "real PAC corpus is not available",
)
class RealPacIntegrationTests(unittest.TestCase):
    def test_real_pac_noop_roundtrip_is_byte_exact(self) -> None:
        service = FpacArchiveService()
        for name in ("table_sc.pac", "script_sc.pac"):
            source = CORPUS_ROOT / name
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_name:
                output = Path(temp_name) / name
                archive = service.inspect(source)
                service.build(archive, {}, output)
                self.assertEqual(_sha256(output), _sha256(source))

    def test_real_tbl_edit_survives_pac_roundtrip(self) -> None:
        source = CORPUS_ROOT / "table_sc.pac"
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            project = PacProjectSession(manager.open(source))
            document = project.open_entry(
                "table_sc/t_books.tbl",
                options=SessionOptions(mode=WorkflowMode.SAFE),
            )
            expected = document.units[0].current_text + " [PAC-TEST]"
            document.units[0].current_text = expected
            project.save_current()
            output = root / "table_sc.changed.pac"
            report = project.build(output)
            self.assertEqual(report.changed_entries, ("table_sc/t_books.tbl",))

            rebuilt = FpacArchiveService().inspect(output)
            extracted = root / "t_books.tbl"
            FpacArchiveService().extract_entry(
                rebuilt,
                "table_sc/t_books.tbl",
                extracted,
            )
            verified = RetextService().load(extracted, mode=WorkflowMode.SAFE)
            self.assertEqual(verified.units[0].current_text, expected)
            project.close()

    def test_overwrite_source_rebases_open_project_entry(self) -> None:
        source = CORPUS_ROOT / "table_sc.pac"
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            working_source = root / source.name
            working_source.write_bytes(source.read_bytes())
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            project = PacProjectSession(manager.open(working_source))
            workspace_root = project.workspace.root
            document = project.open_entry(
                "table_sc/t_books.tbl",
                options=SessionOptions(mode=WorkflowMode.SAFE),
            )
            document.units[0].current_text += " [PAC-OVERWRITE]"
            project.save_current()
            report = project.build(working_source)
            self.assertTrue(Path(f"{working_source}.bak").is_file())
            self.assertEqual(report.changed_entries, ("table_sc/t_books.tbl",))
            self.assertIsNotNone(project.current_entry)
            self.assertEqual(
                project.current_entry.sha256,
                project.workspace.archive.get_entry("table_sc/t_books.tbl").sha256,
            )
            self.assertEqual(project.workspace.state(), "exported")
            project.close()
            reopened = manager.open(working_source)
            self.assertEqual(reopened.root, workspace_root)
            reopened.close()


def _minimal_exact_dat() -> bytes:
    function_start = 56
    strings_start = 63
    function_name = b"func\0"
    text = "阵".encode("utf-8") + b"\0"
    text_offset = strings_start + len(function_name)
    payload = bytearray(text_offset + len(text))
    payload[:4] = b"#scp"
    payload[4:8] = (24).to_bytes(4, "little")
    payload[8:12] = (1).to_bytes(4, "little")
    payload[12:16] = function_start.to_bytes(4, "little")
    payload[24:28] = function_start.to_bytes(4, "little")
    payload[32:36] = function_start.to_bytes(4, "little")
    payload[36:40] = function_start.to_bytes(4, "little")
    payload[44:48] = function_start.to_bytes(4, "little")
    payload[52:56] = (0xC0000000 | strings_start).to_bytes(4, "little")
    payload[function_start : function_start + 2] = b"\0\x04"
    payload[function_start + 2 : function_start + 6] = (
        0xC0000000 | text_offset
    ).to_bytes(4, "little")
    payload[function_start + 6] = 13
    payload[strings_start:text_offset] = function_name
    payload[text_offset:] = text
    return bytes(payload)


def _make_pac(items: list[tuple[str, bytes]]) -> bytes:
    header_table_size = 16 + len(items) * 32
    name_block = bytearray()
    records: list[dict[str, object]] = []
    for order, (name, payload) in enumerate(items):
        encoded = name.encode("utf-8")
        records.append(
            {
                "order": order,
                "name": name,
                "payload": payload,
                "hash": zlib.crc32(encoded) ^ 0xFFFFFFFF,
                "name_offset": header_table_size + len(name_block),
            }
        )
        name_block.extend(encoded + b"\x00")
    data_offset = header_table_size + len(name_block)
    for record in records:
        payload = record["payload"]
        record["data_offset"] = data_offset
        data_offset += len(payload)

    entry_table = bytearray()
    for record in sorted(records, key=lambda item: int(item["hash"])):
        payload = record["payload"]
        entry_table.extend(
            struct.pack(
                "<2I3Q",
                record["hash"],
                0,
                record["name_offset"],
                len(payload),
                record["data_offset"],
            )
        )
    payload_block = b"".join(record["payload"] for record in records)
    header_size = header_table_size + len(name_block)
    return (
        b"FPAC"
        + struct.pack("<3I", len(records), header_size, 1)
        + entry_table
        + name_block
        + payload_block
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
