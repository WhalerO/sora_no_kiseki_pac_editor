"""Read-only-source integration check against a user's current game PACs."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retext.archive import FpacArchiveService, PacNodeRef, PacWorkbench, PacWorkspaceManager
from retext.paths import create_runtime_dir, cleanup_runtime_dir
from retext.session import DocumentSession, SessionOptions
from retext.business import WorkspaceBusiness, BusinessFileTarget, BatchMapping


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pac-dir", type=Path, required=True)
    parser.add_argument("--keep", action="store_true", help="Keep managed test outputs for manual UI checks")
    args = parser.parse_args()
    run = create_runtime_dir("editor_review")
    workbench = PacWorkbench(PacWorkspaceManager(workspaces_root=run / "workspaces", trash_root=run / "trash"))
    service = FpacArchiveService()
    started = time.perf_counter()
    try:
        table = service.inspect(args.pac_dir / "table_sc.pac")
        script = service.inspect(args.pac_dir / "script_sc.pac")
        tbl_entry = next(e for e in table.entries if e.name.endswith("/t_books.tbl"))
        dat_entry = next(e for e in script.entries if e.name.endswith("/scena/mp0000.dat"))
        tbl = service.extract_entry(table, tbl_entry.name, run / "t_books.tbl")
        dat = service.extract_entry(script, dat_entry.name, run / "mp0000.dat")
        for engine, source in (("kuro_tbl", tbl), ("legacy", dat), ("kuro_dat", dat)):
            session = DocumentSession()
            session.open_document(source, options=SessionOptions(engine_override=engine, do_backup=False))
            editable = next(u for u in session.document.units if u.current_text.strip() and any('\u4e00' <= c <= '\u9fff' for c in u.current_text))
            session.update_unit(editable.index, editable.current_text + "【编辑器回归测试】")
            session.save_as(run / f"{engine}{source.suffix}")
            session.update_unit(editable.index, session.document.get_unit(editable.index).current_text + "再次保存")
            session.save()
            assert session.changed_count() == 0
            print(f"[ok] {engine}: {len(session.document.units)} strings, growth + repeated save", flush=True)
        subprocess.run([sys.executable, str(ROOT / "scripts/smoke_test.py"), "--tbl", str(tbl), "--dat", str(dat)], check=True)
        business = WorkspaceBusiness()
        for source in (tbl, dat):
            original_document = DocumentSession().open_document(source)
            old = next(u.current_text for u in original_document.units if len(u.current_text) > 80)
            target = run / ("ordered_" + source.name)
            target.write_bytes(source.read_bytes())
            rules = [BatchMapping(old, old + "【顺序测试甲】", True),
                     BatchMapping(old + "【顺序测试甲】", old + "【顺序测试乙】", True)]
            scan = business.scan_mixed_batch_targets([BusinessFileTarget(str(target), source.name)], "*.tbl,*.dat", rules, use_equal_fast_path=False, ordered=True)
            assert scan.complete and len(scan.hits) >= 2 and all(h.writable for h in scan.hits), scan.errors
            ok, fail, logs = business.execute_mixed_batch(scan.hits, rules, do_backup=False)
            assert ok == len(scan.hits) and not fail, logs
            verified = DocumentSession().open_document(target)
            assert [u.current_text for u in verified.units] == [old + "【顺序测试乙】" if u.current_text == old else u.current_text for u in original_document.units]
            print(f"[ok] {source.name}: ordered batch chained {ok} hits; final full-text verification", flush=True)
        project = workbench.open(args.pac_dir / "image_sc.pac")
        workspace = project.workspace
        images = [e for e in workspace.entries() if e.name.endswith(".dds")]
        replacement = workspace.export_entry(images[-1].name, run / "replacement.dds")
        workspace.import_file(images[0].name, replacement, replace_existing=True)
        added_name = "editor_review/inserted.dds"
        workspace.insert_files({added_name: replacement})
        report = project.build(run / "image_sc.pac", do_backup=False)
        rebuilt = report.verified_archive
        expected = workspace.get_entry(images[-1].name).sha256
        assert rebuilt.get_entry(images[0].name).sha256 == expected
        assert rebuilt.get_entry(added_name).sha256 == expected
        for entry in workspace.archive.entries:
            if entry.name != images[0].name:
                assert rebuilt.get_entry(entry.name).sha256 == entry.sha256
        outputs = workbench.export_refs([PacNodeRef(workspace.workspace_id, "folder", "editor_review")], run / "extracted")
        assert outputs[0].read_bytes() == replacement.read_bytes()
        assert service.source_is_current(workspace.archive, deep=True)
        assert service.source_is_current(table, deep=True) and service.source_is_current(script, deep=True)
        print(f"[ok] image PAC: {len(workspace.entries())} entries, replace/add/extract/rebuild; unchanged payloads verified", flush=True)
        print(f"[ok] Sources unchanged. Total {time.perf_counter() - started:.2f}s. Output: {run}", flush=True)
    finally:
        workbench.close_all()
        if not args.keep:
            cleanup_runtime_dir(run)


if __name__ == "__main__":
    main()
