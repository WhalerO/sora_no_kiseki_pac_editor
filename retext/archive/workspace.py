from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..domain import is_text_document_path
from ..engines.kuro.processcle import unwrapCLE
from ..engines.relocation import parse_dat_references, parse_tbl_headers
from ..io_utils import atomic_copy_file, atomic_write_bytes
from ..paths import (
    DATA_ROOT,
    ensure_trash_root,
    ensure_workspaces_root,
)
from .domain import PacArchive, PacBuildReport, PacEntry, PacWorkspaceSummary
from .fpac import FpacArchiveService, _validate_entry_name
from .lock_guard import workspace_lock_guard


MANIFEST_VERSION = 1


class PacWorkspaceError(RuntimeError):
    pass


class PacWorkspaceIntegrityError(PacWorkspaceError):
    """A candidate PAC contains parse failures that the user may override."""

    def __init__(self, failures: list[str] | tuple[str, ...]) -> None:
        self.failures = tuple(failures)
        details = "\n".join(self.failures[:20])
        remainder = len(self.failures) - 20
        if remainder > 0:
            details += f"\n……另有 {remainder} 个失败条目。"
        super().__init__(
            "PAC 构建前完整性检查失败；检测到损坏或无法完整解析的 TBL/DAT。"
            "这类条目即使不是本次修改目标，也可能造成游戏卡死。"
            "可以在明确确认风险后保留这些条目并继续导出。\n\n"
            + details
        )


class PacWorkspace:
    def __init__(
        self,
        manager: "PacWorkspaceManager",
        archive: PacArchive,
        root: Path,
        manifest: dict[str, object],
    ) -> None:
        self.manager = manager
        self.service = manager.service
        self.archive = archive
        self.root = root
        self.manifest = manifest
        self.workspace_id = str(manifest["workspace_id"])
        self.files_root = root / "files"
        self.preview_root = root / "preview"
        self.recovery_root = root / "recovery"
        self.logs_root = root / "logs"
        self.lock_path = root / "lock.json"
        self._lock_token = uuid4().hex
        self._record_by_name: dict[str, dict[str, object]] = {
            str(record["name"]): record
            for record in self._entry_records()
        }
        self._additions: dict[str, PacEntry] = {}
        for record in self._entry_records():
            if record.get("added"):
                name = str(record["name"])
                _validate_entry_name(name)
                self._additions[name] = PacEntry(
                    header_index=int(record["header_index"]), name_order=int(record["name_order"]),
                    data_order=int(record["data_order"]), name=name,
                    path_hash=int(record["path_hash"]), reserved=0, name_offset=0,
                    size=int(record["current_size"]), data_offset=0, sha256="",
                )
        self._acquire_lock()
        try:
            self.files_root.mkdir(parents=True, exist_ok=True)
            self.preview_root.mkdir(parents=True, exist_ok=True)
            self.recovery_root.mkdir(parents=True, exist_ok=True)
            self.logs_root.mkdir(parents=True, exist_ok=True)
            self.reconcile_materialized()
            self.manifest["last_opened"] = _utc_now()
            self._save_manifest()
        except Exception:
            self._release_lock()
            raise

    def close(self) -> None:
        try:
            self.reconcile_materialized()
        finally:
            self._release_lock()
            self.manager._forget(self.workspace_id)
            if self.manager.ephemeral:
                self.manager._discard_ephemeral_root(self.root)

    def editable_entries(self) -> list[PacEntry]:
        return [entry for entry in self.entries() if entry.editable]

    def entries(self) -> list[PacEntry]:
        return [*self.archive.entries, *self._additions.values()]

    def get_entry(self, name: str) -> PacEntry:
        return self._additions[name] if name in self._additions else self.archive.get_entry(name)

    def current_size(self, name: str) -> int:
        record = self._record(name)
        if record.get("materialized") or record.get("dirty"):
            return int(record["current_size"])
        return self.get_entry(name).size

    def entry_path(self, entry_or_name: PacEntry | str) -> Path:
        entry = (
            entry_or_name
            if isinstance(entry_or_name, PacEntry)
            else self.get_entry(entry_or_name)
        )
        suffix = entry.suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", entry.suffix) else ".bin"
        cache_root = self.files_root if entry.editable else self.preview_root
        return cache_root / f"{entry.data_order:06d}{suffix}"

    def materialize(self, entry_name: str) -> Path:
        return self._materialize(entry_name, save_manifest=True)

    def read_entry_prefix(self, entry_name: str, limit: int = 512) -> bytes:
        if limit < 0:
            raise ValueError("Entry prefix limit cannot be negative.")
        if self._record(entry_name).get("dirty"):
            with self.materialize(entry_name).open("rb") as stream:
                return stream.read(limit)
        return self.service.read_entry_prefix(self.archive, entry_name, limit)

    def export_entry(
        self,
        entry_name: str,
        output_path: str | Path,
        *,
        prefer_current: bool = True,
    ) -> Path:
        entry = self.get_entry(entry_name)
        output = Path(output_path).resolve()
        if output == self.archive.source_path.resolve():
            raise PacWorkspaceError("提取目标不能覆盖源 PAC 文件。")
        if entry_name in self._additions or prefer_current:
            record = self._record(entry_name)
            cached = self.entry_path(entry)
            if record.get("dirty") or (cached.is_file() and bool(record.get("materialized"))):
                current = self._materialize(entry_name, save_manifest=True)
                return atomic_copy_file(current, output)
        return self.service.extract_entry(self.archive, entry_name, output)

    def _materialize(self, entry_name: str, *, save_manifest: bool) -> Path:
        entry = self.get_entry(entry_name)
        record = self._record(entry_name)
        target = self.entry_path(entry)
        imported = bool(record.get("imported") or record.get("added"))
        if target.is_file():
            stat = target.stat()
            unchanged_preview_cache = (
                not entry.editable
                and bool(record.get("materialized"))
                and str(record.get("current_sha256", "")) == entry.sha256
                and int(record.get("current_size", -1)) == stat.st_size
                and int(record.get("current_mtime_ns", -1)) == stat.st_mtime_ns
            )
            digest = entry.sha256 if unchanged_preview_cache else _hash_file(target)
            if not entry.editable and not imported and digest != entry.sha256:
                self.service.extract_entry(self.archive, entry_name, target)
                stat = target.stat()
                digest = entry.sha256
            values = {
                "materialized": True,
                "current_sha256": digest,
                "current_size": stat.st_size,
                "current_mtime_ns": stat.st_mtime_ns,
                "dirty": bool(record.get("added")) or (entry.editable or imported) and digest != entry.sha256,
            }
            if any(record.get(key) != value for key, value in values.items()):
                record.update(values)
                if save_manifest:
                    self._save_manifest()
            return target

        if bool(record.get("dirty")):
            raise PacWorkspaceError(
                f"缓存中的已修改条目丢失，无法静默恢复：{entry_name}"
            )
        self.service.extract_entry(self.archive, entry_name, target)
        stat = target.stat()
        record.update(
            {
                "materialized": True,
                "current_sha256": entry.sha256,
                "current_size": entry.size,
                "current_mtime_ns": stat.st_mtime_ns,
                "dirty": False,
            }
        )
        if save_manifest:
            self._save_manifest()
        return target

    def materialize_all_editable(self) -> list[Path]:
        return self.materialize_entries(
            entry.name
            for entry in self.editable_entries()
        )

    def materialize_entries(self, entry_names) -> list[Path]:
        paths: list[Path] = []
        seen: set[str] = set()
        try:
            for entry_name in entry_names:
                if entry_name in seen:
                    continue
                seen.add(entry_name)
                paths.append(self._materialize(entry_name, save_manifest=False))
        finally:
            if paths:
                self._save_manifest()
        return paths

    def refresh_entry(self, entry_name: str) -> bool:
        entry = self.get_entry(entry_name)
        if not entry.editable and not self._record(entry_name).get("imported"):
            raise PacWorkspaceError(
                f"非 TBL/DAT 条目是只读预览缓存，不能登记为 PAC 修改：{entry_name}"
            )
        target = self.entry_path(entry)
        if not target.is_file():
            raise FileNotFoundError(f"PAC 工作区条目不存在：{entry_name}")
        digest = _hash_file(target)
        record = self._record(entry_name)
        record.update(
            {
                "materialized": True,
                "current_sha256": digest,
                "current_size": target.stat().st_size,
                "current_mtime_ns": target.stat().st_mtime_ns,
                "dirty": entry_name in self._additions or digest != entry.sha256,
            }
        )
        self._save_manifest()
        return bool(record["dirty"])

    def reconcile_materialized(self) -> None:
        changed = False
        for entry in self.entries():
            record = self._record(entry.name)
            target = self.entry_path(entry)
            if not target.is_file():
                if record.get("materialized") and not record.get("dirty"):
                    record["materialized"] = False
                    record["current_sha256"] = entry.sha256
                    record["current_size"] = entry.size
                    record["current_mtime_ns"] = 0
                    changed = True
                continue
            stat = target.stat()
            if not entry.editable and not record.get("imported"):
                cache_unchanged = (
                    bool(record.get("materialized"))
                    and str(record.get("current_sha256", "")) == entry.sha256
                    and int(record.get("current_size", -1)) == stat.st_size
                    and int(record.get("current_mtime_ns", -1)) == stat.st_mtime_ns
                )
                values = {
                    "materialized": cache_unchanged,
                    "current_sha256": entry.sha256 if cache_unchanged else "",
                    "current_size": stat.st_size,
                    "current_mtime_ns": stat.st_mtime_ns,
                    "dirty": False,
                }
                if any(record.get(key) != value for key, value in values.items()):
                    record.update(values)
                    changed = True
                continue
            digest = _hash_file(target)
            dirty = bool(record.get("added")) or digest != entry.sha256
            values = {
                "materialized": True,
                "current_sha256": digest,
                "current_size": stat.st_size,
                "current_mtime_ns": stat.st_mtime_ns,
                "dirty": dirty,
            }
            if any(record.get(key) != value for key, value in values.items()):
                record.update(values)
                changed = True
        if changed:
            self._save_manifest()

    def discard_entry(self, entry_name: str) -> Path:
        if entry_name in self._additions:
            target = self.entry_path(entry_name)
            # Keep the payload in managed recovery until the workspace closes.
            if target.exists():
                target.replace(self.recovery_root / f"{uuid4().hex}{target.suffix}")
            del self._additions[entry_name]
            del self._record_by_name[entry_name]
            self.manifest["entries"] = list(self._record_by_name.values())
            self._save_manifest()
            return target
        entry = self.archive.get_entry(entry_name)
        target = self.entry_path(entry)
        self.service.extract_entry(self.archive, entry_name, target)
        stat = target.stat()
        record = self._record(entry_name)
        record.update(
            {
                "materialized": True,
                "current_sha256": entry.sha256,
                "current_size": entry.size,
                "current_mtime_ns": stat.st_mtime_ns,
                "dirty": False,
            }
        )
        self._save_manifest()
        return target

    def discard_all(self) -> None:
        for name in self.dirty_entry_names():
            self.discard_entry(name)

    def dirty_entry_names(self) -> list[str]:
        return [
            str(record["name"])
            for record in self._entry_records()
            if bool(record.get("dirty"))
        ]

    def entry_state(self, entry_name: str) -> str:
        record = self._record(entry_name)
        if record.get("added"):
            return "added"
        if record.get("dirty"):
            return "modified"
        if record.get("materialized"):
            return "cached"
        return "source"

    def unbuilt_entry_names(self) -> list[str]:
        """Changes not represented by the latest successful PAC export.

        Dirty remains relative to the original PAC: clearing it after Save As
        would silently omit those edits from subsequent exports.
        """
        current = {
            str(record["name"]): str(record.get("current_sha256", ""))
            for record in self._entry_records() if record.get("dirty")
        }
        exported = self.manifest.get("last_built_changes", {})
        if not isinstance(exported, dict):
            exported = {}
        outputs = self.manifest.get("outputs", [])
        if exported and outputs:
            # A deleted/replaced export must not silently authorize disposal
            # of the only remaining edited copy. No whole-PAC hash on close.
            latest = outputs[-1]
            try:
                stat = Path(latest["path"]).stat()
                available = stat.st_size == latest["size"] and (
                    "mtime_ns" not in latest or stat.st_mtime_ns == latest["mtime_ns"]
                )
            except (OSError, KeyError, TypeError):
                available = False
            if not available:
                return sorted(current.keys() | exported.keys())
        return sorted(name for name in current.keys() | exported.keys()
                      if current.get(name) != exported.get(name))

    def state(self) -> str:
        if not self.archive.source_path.exists():
            return "missing-source"
        try:
            stat = self.archive.source_path.stat()
        except OSError:
            return "missing-source"
        if (
            stat.st_size != self.archive.source_size
            or stat.st_mtime_ns != self.archive.source_mtime_ns
        ):
            return "stale"
        if self.unbuilt_entry_names():
            return "dirty"
        if self.manifest.get("last_export"):
            return "exported"
        return "clean"

    def build(
        self,
        output_path: str | Path,
        *,
        do_backup: bool = True,
        allow_integrity_errors: bool = False,
    ) -> PacBuildReport:
        self.reconcile_materialized()
        integrity_failures = self.text_entry_structure_failures()
        if integrity_failures and not allow_integrity_errors:
            raise PacWorkspaceIntegrityError(integrity_failures)
        dirty_names = self.dirty_entry_names()
        noneditable_dirty = [
            name
            for name in dirty_names
            if not self.get_entry(name).editable and not self._record(name).get("imported")
        ]
        if noneditable_dirty:
            names = "、".join(noneditable_dirty[:5])
            raise PacWorkspaceError(
                f"检测到非 TBL/DAT 缓存变化，已阻止其进入 PAC 回包：{names}"
            )
        replacements = {
            name: self.entry_path(name)
            for name in dirty_names
            if name not in self._additions
        }
        report = self.service.build(
            self.archive,
            replacements,
            output_path,
            do_backup=do_backup,
            additions={name: self.entry_path(name) for name in self._additions},
        )
        self.manifest["last_export"] = _utc_now()
        outputs = self.manifest.setdefault("outputs", [])
        if isinstance(outputs, list):
            outputs.append(
                {
                    "path": str(report.output_path),
                    "sha256": report.output_sha256,
                    "size": report.output_size,
                    "mtime_ns": report.output_path.stat().st_mtime_ns,
                    "changed_entries": list(report.changed_entries),
                    "integrity_warnings": list(integrity_failures),
                    "created": self.manifest["last_export"],
                }
            )
            del outputs[:-20]

        if report.output_path.resolve() == self.archive.source_path.resolve():
            self._rebase_to_output(
                report.output_path,
                verified_archive=report.verified_archive,
            )
        verified = report.verified_archive or self.service.inspect(report.output_path)
        self.manifest["last_built_changes"] = {
            name: verified.get_entry(name).sha256
            for name in self.dirty_entry_names()
        }
        self._save_manifest()
        return report

    def text_entry_structure_failures(self) -> list[str]:
        """Return every structurally broken TBL/#scp DAT in the candidate PAC.

        A damaged source entry can otherwise pass through unchanged when the
        user's current batch scope does not include it.  That is precisely how
        an older corrupted DAT survived later Legacy-only edits.  Validation
        therefore covers the complete candidate PAC, using dirty materialized
        bytes where present and original archive bytes everywhere else.
        """

        if not self.service.source_is_current(self.archive):
            raise PacWorkspaceError(
                "源 PAC 内容已发生变化。请先使用“更新所选 PAC”重新载入工作区。"
            )
        failures: list[str] = []
        with self.archive.source_path.open("rb") as source:
            for entry in self.editable_entries():
                record = self._record(entry.name)
                cached = self.entry_path(entry)
                if bool(record.get("materialized")) and cached.is_file():
                    raw = cached.read_bytes()
                else:
                    source.seek(entry.data_offset)
                    raw = source.read(entry.size)
                    if len(raw) != entry.size:
                        failures.append(f"{entry.name}：PAC 条目读取不完整")
                        continue
                try:
                    payload, _layers = unwrapCLE(raw)
                    if entry.suffix == ".dat" and payload.startswith(b"#scp"):
                        parse_dat_references(payload)
                    elif entry.suffix == ".tbl" and payload.startswith(b"#TBL"):
                        parse_tbl_headers(payload)
                except Exception as exc:
                    failures.append(f"{entry.name}：{exc}")
        if failures:
            return failures
        return []

    def _rebase_to_output(
        self,
        output_path: Path,
        *,
        verified_archive: PacArchive | None = None,
    ) -> None:
        rebuilt = verified_archive or self.service.inspect(output_path)
        if not _same_path(rebuilt.source_path, output_path):
            raise PacWorkspaceError(
                "PAC build verification metadata does not match the output path."
            )
        previous_records = self._record_by_name
        self.archive = rebuilt
        self._additions = {}
        new_records: list[dict[str, object]] = []
        for entry in rebuilt.entries:
            old = previous_records[entry.name]
            # Removing an uncommitted addition can leave gaps in cache IDs.
            # Rebuilt PAC data orders are dense: a moved entry must be read
            # from the verified output, never another entry's old cache slot.
            materialized = bool(old.get("materialized"))
            if int(old["data_order"]) != entry.data_order:
                self.service.extract_entry(rebuilt, entry.name, self.entry_path(entry))
                materialized = True
            new_records.append(
                _entry_record(
                    entry,
                    materialized=materialized,
                    current_sha256=entry.sha256,
                    current_size=entry.size,
                    dirty=False,
                )
            )
        self.manifest["source"] = _source_record(rebuilt)
        self.manifest["archive"] = _archive_record(rebuilt)
        self.manifest["entries"] = new_records
        self._record_by_name = {
            str(record["name"]): record
            for record in new_records
        }

    def import_file(self, entry_name: str, source_path: str | Path, *, replace_existing: bool = False) -> PacEntry:
        """Stage a private copy; later external edits cannot change the PAC."""
        if not replace_existing:
            return self.insert_files({entry_name: source_path})[0]
        _validate_entry_name(entry_name)
        source = Path(source_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"找不到导入文件：{source}")
        if source == self.archive.source_path:
            raise ValueError("不能把源 PAC 本身导入为包内文件。")
        entry = self.get_entry(entry_name)
        target = self.entry_path(entry)
        atomic_copy_file(source, target)
        self._record(entry_name)["imported"] = True
        self.refresh_entry(entry_name)
        return entry

    def insert_files(self, sources: dict[str, str | Path]) -> list[PacEntry]:
        """Validate and copy the entire selection before publishing its manifest."""
        if not sources:
            return []
        from .fpac import MAX_ENTRIES, MAX_NAME_BYTES

        entries = self.entries()
        if len(entries) + len(sources) > MAX_ENTRIES:
            raise ValueError("插入后超过 PAC 条目数量上限。")
        known = {entry.name.casefold() for entry in entries}
        order = max((entry.data_order for entry in entries), default=-1) + 1
        planned: list[tuple[PacEntry, Path]] = []
        for name, path in sources.items():
            _validate_entry_name(name)
            if len(name.encode("utf-8")) >= MAX_NAME_BYTES:
                raise ValueError(f"包内路径过长：{name}")
            folded = name.casefold()
            parts = folded.split("/")
            if (folded in known
                or any("/".join(parts[:i]) in known for i in range(1, len(parts)))
                or any(item.startswith(folded + "/") for item in known)):
                raise ValueError(f"包内路径已存在或与文件夹冲突：{name}；同名文件请使用替换操作。")
            source = Path(path).resolve()
            if not source.is_file():
                raise FileNotFoundError(f"找不到导入文件：{source}")
            if source == self.archive.source_path:
                raise ValueError("不能把源 PAC 本身导入为包内文件。")
            known.add(folded)
            entry = PacEntry(order, order, order, name,
                             zlib.crc32(name.encode("utf-8")) ^ 0xFFFFFFFF,
                             0, 0, source.stat().st_size, 0, "")
            planned.append((entry, source))
            order += 1
        records: list[dict[str, object]] = []
        # These paths are new, unused cache identities. An interrupted copy
        # cannot become a live entry until the atomic manifest publish.
        for entry, source in planned:
            target = self.entry_path(entry)
            atomic_copy_file(source, target)
            stat = target.stat()
            record = _entry_record(entry, materialized=True,
                current_sha256=_hash_file(target), current_size=stat.st_size, dirty=True)
            record.update(added=True, imported=True, current_mtime_ns=stat.st_mtime_ns)
            records.append(record)
        previous = self.manifest["entries"]
        self.manifest["entries"] = [*self._entry_records(), *records]
        try:
            self._save_manifest()
        except Exception:
            self.manifest["entries"] = previous
            raise
        for (entry, _source), record in zip(planned, records):
            self._additions[entry.name] = entry
            self._record_by_name[entry.name] = record
        return [entry for entry, _source in planned]

    def _record(self, name: str) -> dict[str, object]:
        try:
            return self._record_by_name[name]
        except KeyError as exc:
            raise PacWorkspaceError(f"PAC workspace manifest is missing: {name}") from exc

    def _entry_records(self) -> list[dict[str, object]]:
        records = self.manifest.get("entries")
        if not isinstance(records, list):
            raise PacWorkspaceError("PAC workspace manifest has no entry list.")
        return [record for record in records if isinstance(record, dict)]

    def _save_manifest(self) -> None:
        payload = json.dumps(
            self.manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        atomic_write_bytes(self.root / "manifest.json", payload)

    def _acquire_lock(self) -> None:
        _acquire_workspace_lock(self.root, self._lock_token)

    def _release_lock(self) -> None:
        _release_workspace_lock(self.lock_path, self._lock_token)


class PacWorkspaceManager:
    def __init__(
        self,
        service: FpacArchiveService | None = None,
        *,
        workspaces_root: str | Path | None = None,
        trash_root: str | Path | None = None,
        ephemeral: bool = False,
    ) -> None:
        self.service = service or FpacArchiveService()
        self.ephemeral = bool(ephemeral)
        self.workspaces_root = (
            Path(workspaces_root).resolve()
            if workspaces_root is not None
            else ensure_workspaces_root()
        )
        self.trash_root = (
            Path(trash_root).resolve()
            if trash_root is not None
            else ensure_trash_root()
        )
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.trash_root.mkdir(parents=True, exist_ok=True)
        self._opened: dict[str, PacWorkspace] = {}

    @property
    def data_root(self) -> Path:
        return DATA_ROOT

    def open(self, pac_path: str | Path) -> PacWorkspace:
        archive = self.service.inspect(pac_path)
        return self.open_archive(archive)

    def open_archive(self, archive: PacArchive) -> PacWorkspace:
        """Open an already inspected archive without hashing it a second time.

        A workspace name is normally derived from the source path and content
        hash.  Older releases, interrupted creations, or an exceptionally rare
        short-hash collision can leave a different manifest at that name.  Such
        a stale cache must never make the current source PAC impossible to
        open: retain it for recovery and allocate an alternate workspace.
        """

        for workspace in self._opened.values():
            if _same_path(workspace.archive.source_path, archive.source_path):
                if workspace.archive.source_sha256 == archive.source_sha256:
                    return workspace
                raise PacWorkspaceError(
                    "同一路径的 PAC 已在当前程序中打开，但磁盘内容已经变化。"
                )

        workspace_id = _workspace_id(archive)
        if workspace_id in self._opened:
            return self._opened[workspace_id]
        root = self.workspaces_root / workspace_id
        matching_root = self._find_matching_root(archive)
        if matching_root is not None:
            root = matching_root
            workspace_id = root.name
        manifest_path = root / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = _load_manifest(manifest_path)
                _validate_manifest(manifest, archive)
                manifest["source"] = _source_record(archive)
            except PacWorkspaceError:
                # Do not overwrite or delete the conflicting workspace: it may
                # contain the user's only copy of materialized edits.  A fresh,
                # collision-resistant root lets the current PAC open normally.
                root = self._alternate_workspace_root(archive)
                workspace_id = root.name
                root.mkdir(parents=True, exist_ok=False)
                manifest = _new_manifest(workspace_id, archive)
        else:
            root.mkdir(parents=True, exist_ok=True)
            manifest = _new_manifest(workspace_id, archive)
        workspace = PacWorkspace(self, archive, root, manifest)
        self._opened[workspace.workspace_id] = workspace
        return workspace

    def _alternate_workspace_root(self, archive: PacArchive) -> Path:
        base_id = _workspace_id(archive)
        identity = _workspace_identity(archive)
        for end in (24, 32, 48, 64):
            candidate = self.workspaces_root / f"{base_id}-{identity[12:end]}"
            if not candidate.exists():
                return candidate
        return self.workspaces_root / f"{base_id}-{uuid4().hex}"

    def _find_matching_root(self, archive: PacArchive) -> Path | None:
        for root in self.workspaces_root.iterdir():
            manifest_path = root / "manifest.json"
            if not root.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = _load_manifest(manifest_path)
                source = manifest.get("source")
                if not isinstance(source, dict):
                    continue
                if (
                    str(source.get("sha256", "")) == archive.source_sha256
                    and _same_path(
                        Path(str(source.get("path", ""))),
                        archive.source_path,
                    )
                ):
                    return root
            except (OSError, PacWorkspaceError):
                continue
        return None

    def list_summaries(self) -> list[PacWorkspaceSummary]:
        summaries: list[PacWorkspaceSummary] = []
        for root in sorted(self.workspaces_root.iterdir(), key=lambda item: item.name.lower()):
            manifest_path = root / "manifest.json"
            if not root.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = _load_manifest(manifest_path)
                records = [
                    record
                    for record in manifest.get("entries", [])
                    if isinstance(record, dict)
                ]
                state, dirty_count, materialized_count, source_path = (
                    _manifest_cache_state(root, manifest)
                )
                lock_active = _lock_is_active_or_unknown(root / "lock.json")
                manifest_id = str(manifest["workspace_id"])
                if lock_active and manifest_id not in self._opened:
                    state = "locked"
                summaries.append(
                    PacWorkspaceSummary(
                        workspace_id=manifest_id,
                        root=root,
                        source_path=source_path,
                        state=state,
                        entry_count=len(records),
                        dirty_count=dirty_count,
                        materialized_count=materialized_count,
                        size_bytes=_directory_size(root),
                        last_opened=str(manifest.get("last_opened", "")),
                        last_export=str(manifest.get("last_export", "")),
                    )
                )
            except Exception:
                summaries.append(
                    PacWorkspaceSummary(
                        workspace_id=root.name,
                        root=root,
                        source_path=Path(),
                        state="damaged",
                        entry_count=0,
                        dirty_count=0,
                        materialized_count=0,
                        size_bytes=_directory_size(root),
                        last_opened="",
                        last_export="",
                    )
                )
        return summaries

    def summaries_for_source(
        self,
        pac_path: str | Path,
    ) -> list[PacWorkspaceSummary]:
        """Return every historical workspace belonging to one source path."""

        source = Path(pac_path).resolve()
        return [
            summary
            for summary in self.list_summaries()
            if summary.source_path != Path()
            and _same_path(summary.source_path, source)
        ]

    def delete_for_source(
        self,
        pac_path: str | Path,
        *,
        allow_dirty: bool = False,
    ) -> list[Path]:
        """Move all closed cache generations for a source PAC to trash."""

        summaries = self.summaries_for_source(pac_path)
        opened = [
            summary.workspace_id
            for summary in summaries
            if summary.workspace_id in self._opened
        ]
        if opened:
            raise PacWorkspaceError(
                "请先关闭该源 PAC 当前打开的工程，再清理它的全部工作区。"
            )
        if any(summary.state == "locked" for summary in summaries):
            raise PacWorkspaceError(
                "该源 PAC 至少有一个工作区正被另一个程序实例使用，不能清理。"
            )
        if not allow_dirty and any(summary.dirty_count for summary in summaries):
            raise PacWorkspaceError(
                "该源 PAC 的历史工作区包含未回包修改，已阻止自动清理。"
            )

        removed: list[Path] = []
        for summary in summaries:
            removed.append(
                self.delete(summary.workspace_id, allow_dirty=allow_dirty)
            )
        return removed

    def delete(
        self,
        workspace_id: str,
        *,
        allow_dirty: bool = False,
    ) -> Path:
        if workspace_id in self._opened:
            raise PacWorkspaceError("请先关闭当前 PAC 工程，再清理它的工作区。")
        summary = next(
            (item for item in self.list_summaries() if item.workspace_id == workspace_id),
            None,
        )
        if summary is None:
            raise FileNotFoundError(f"PAC workspace does not exist: {workspace_id}")
        if summary.state == "locked":
            raise PacWorkspaceError("PAC 工作区正被另一个程序实例使用，不能清理。")
        return self._move_to_trash(
            summary,
            allow_dirty=allow_dirty,
            require_safe_state=False,
        )

    def clean_safe(self) -> list[Path]:
        removed: list[Path] = []
        for summary in self.list_summaries():
            if summary.workspace_id in self._opened:
                continue
            if summary.state in {"clean", "exported"} and summary.dirty_count == 0:
                try:
                    removed.append(
                        self._move_to_trash(
                            summary,
                            allow_dirty=False,
                            require_safe_state=True,
                        )
                    )
                except PacWorkspaceError:
                    # The workspace may have become dirty or been opened by
                    # another process since the summary snapshot. It is no
                    # longer an automatic-cleanup candidate.
                    continue
        return removed

    def _move_to_trash(
        self,
        summary: PacWorkspaceSummary,
        *,
        allow_dirty: bool,
        require_safe_state: bool,
    ) -> Path:
        token = uuid4().hex
        lock_path = summary.root / "lock.json"
        moved_lock: Path | None = None
        _acquire_workspace_lock(summary.root, token, create_root=False)
        try:
            try:
                manifest = _load_manifest(summary.root / "manifest.json")
                state, dirty_count, _materialized_count, _source_path = (
                    _manifest_cache_state(summary.root, manifest)
                )
            except Exception as exc:
                if require_safe_state:
                    raise PacWorkspaceError(
                        "工作区清单在清理前发生变化，已跳过自动清理。"
                    ) from exc
                state = "damaged"
                dirty_count = 0

            if dirty_count and not allow_dirty:
                raise PacWorkspaceError("工作区包含未回包修改，已阻止自动清理。")
            if require_safe_state and state not in {"clean", "exported"}:
                raise PacWorkspaceError("工作区状态已经变化，已跳过自动清理。")

            destination = self.trash_root / (
                f"{summary.workspace_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            shutil.move(str(summary.root), str(destination))
            moved_lock = destination / "lock.json"
            return destination
        finally:
            _release_workspace_lock(moved_lock or lock_path, token)

    def _forget(self, workspace_id: str) -> None:
        self._opened.pop(workspace_id, None)

    def _discard_ephemeral_root(self, root: Path) -> None:
        """Permanently remove one closed session workspace.

        Ephemeral managers are created below a unique per-process runtime
        directory.  Require a direct child of that configured root so a bad
        manifest or caller can never broaden cleanup beyond the current PAC
        workspace.
        """

        candidate = root.resolve()
        base = self.workspaces_root.resolve()
        if candidate.parent != base:
            raise PacWorkspaceError(
                f"拒绝清理会话工作区之外的路径：{candidate}"
            )
        if candidate.exists():
            shutil.rmtree(candidate)


def _new_manifest(workspace_id: str, archive: PacArchive) -> dict[str, object]:
    now = _utc_now()
    return {
        "schema_version": MANIFEST_VERSION,
        "workspace_id": workspace_id,
        "created": now,
        "last_opened": now,
        "last_export": "",
        "source": _source_record(archive),
        "archive": _archive_record(archive),
        "entries": [_entry_record(entry) for entry in archive.entries],
        "outputs": [],
    }


def _source_record(archive: PacArchive) -> dict[str, object]:
    return {
        "path": str(archive.source_path),
        "size": archive.source_size,
        "mtime_ns": archive.source_mtime_ns,
        "sha256": archive.source_sha256,
    }


def _archive_record(archive: PacArchive) -> dict[str, object]:
    return {
        "format": "FPAC",
        "format_version": archive.format_version,
        "header_size": archive.header_size,
        "entry_count": len(archive.entries),
    }


def _entry_record(
    entry: PacEntry,
    *,
    materialized: bool = False,
    current_sha256: str | None = None,
    current_size: int | None = None,
    current_mtime_ns: int = 0,
    dirty: bool = False,
) -> dict[str, object]:
    return {
        "header_index": entry.header_index,
        "name_order": entry.name_order,
        "data_order": entry.data_order,
        "name": entry.name,
        "path_hash": entry.path_hash,
        "reserved": entry.reserved,
        "name_offset": entry.name_offset,
        "original_size": entry.size,
        "data_offset": entry.data_offset,
        "original_sha256": entry.sha256,
        "materialized": materialized,
        "current_sha256": current_sha256 or entry.sha256,
        "current_size": entry.size if current_size is None else current_size,
        "current_mtime_ns": current_mtime_ns,
        "dirty": dirty,
    }


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PacWorkspaceError(f"无法读取 PAC 工作区清单：{path}") from exc
    if not isinstance(payload, dict):
        raise PacWorkspaceError(f"PAC workspace manifest is not an object: {path}")
    if int(payload.get("schema_version", -1)) != MANIFEST_VERSION:
        raise PacWorkspaceError("PAC 工作区清单版本不受支持。")
    return payload


def _validate_manifest(manifest: dict[str, object], archive: PacArchive) -> None:
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise PacWorkspaceError("PAC workspace manifest has no source record.")
    recorded_path = Path(str(source.get("path", "")))
    if recorded_path == Path() or not _same_path(recorded_path, archive.source_path):
        raise PacWorkspaceError("PAC 工作区属于另一个源文件，需要建立新的工作区。")
    if str(source.get("sha256", "")) != archive.source_sha256:
        raise PacWorkspaceError("PAC 内容已经变化，需要建立新的工作区。")
    records = manifest.get("entries")
    if not isinstance(records, list):
        raise PacWorkspaceError("PAC workspace manifest has no entry records.")
    names = {
        str(record.get("name"))
        for record in records
        if isinstance(record, dict) and not record.get("added")
    }
    if names != {entry.name for entry in archive.entries}:
        raise PacWorkspaceError("PAC workspace entry list no longer matches the source.")
    all_names: set[str] = set()
    orders: set[int] = set()
    try:
        for record in records:
            name = record["name"]
            _validate_entry_name(name)
            order = int(record["data_order"])
            if name.casefold() in all_names or order in orders or order < 0:
                raise ValueError("Duplicate name or cache identity")
            all_names.add(name.casefold())
            orders.add(order)
            if record.get("added"):
                if order < len(archive.entries) or int(record["current_size"]) < 0:
                    raise ValueError("Invalid addition cache identity")
            elif archive.get_entry(name).data_order != order:
                raise ValueError("Original cache identity changed")
    except (ValueError, TypeError, KeyError) as exc:
        raise PacWorkspaceError("PAC 工作区清单中的路径或缓存编号无效；保留旧缓存并建立新工作区。") from exc


def _workspace_id(archive: PacArchive) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", archive.source_path.stem).strip("._")
    identity = _workspace_identity(archive)
    return f"{stem or 'pac'}-{identity[:12]}"


def _workspace_identity(archive: PacArchive) -> str:
    normalized_path = os.path.normcase(str(archive.source_path.resolve()))
    return hashlib.sha256(
        f"{normalized_path}\0{archive.source_sha256}".encode("utf-8")
    ).hexdigest()


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _cached_entry_counts(
    root: Path,
    records: list[dict[str, object]],
) -> tuple[int, int]:
    """Derive cache state from disk instead of trusting a possibly stale manifest."""

    dirty_count = 0
    materialized_count = 0
    files_root = root / "files"
    preview_root = root / "preview"
    for record in records:
        name = str(record.get("name", ""))
        suffix = Path(name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            suffix = ".bin"
        data_order = int(record["data_order"])
        editable = is_text_document_path(name)
        cache_root = files_root if editable else preview_root
        cached = cache_root / f"{data_order:06d}{suffix}"
        if cached.is_file():
            materialized_count += 1
            if (
                (editable or bool(record.get("imported")) or bool(record.get("added")))
                and _hash_file(cached) != str(record.get("original_sha256", ""))
            ):
                dirty_count += 1
        elif bool(record.get("dirty")):
            # A missing file previously recorded as dirty represents possible
            # unrecovered work and must never be classified as safe to clean.
            dirty_count += 1
    return dirty_count, materialized_count


def _manifest_cache_state(
    root: Path,
    manifest: dict[str, object],
) -> tuple[str, int, int, Path]:
    records = [
        record
        for record in manifest.get("entries", [])
        if isinstance(record, dict)
    ]
    dirty_count, materialized_count = _cached_entry_counts(root, records)
    source = manifest.get("source")
    if not isinstance(source, dict) or not str(source.get("path", "")):
        raise PacWorkspaceError("PAC workspace manifest has no valid source path.")
    source_path = Path(str(source["path"]))
    if not source_path.exists():
        state = "missing-source"
    else:
        try:
            stat = source_path.stat()
        except OSError:
            state = "missing-source"
        else:
            if (
                stat.st_size != int(source.get("size", -1))
                or stat.st_mtime_ns != int(source.get("mtime_ns", -1))
            ):
                state = "stale"
            elif dirty_count:
                state = "dirty"
            elif manifest.get("last_export"):
                state = "exported"
            else:
                state = "clean"
    return state, dirty_count, materialized_count, source_path


def _read_lock(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, dict):
            return payload, raw
        return {}, raw
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, b""


def _lock_pid(payload: dict[str, object]) -> int:
    try:
        return int(payload.get("pid", -1))
    except (TypeError, ValueError):
        return -1


def _lock_is_active_or_unknown(path: Path) -> bool:
    if not path.exists():
        return False
    payload, _raw = _read_lock(path)
    pid = _lock_pid(payload)
    if pid <= 0:
        return True
    return _pid_is_alive(pid)


def _acquire_workspace_lock(
    root: Path,
    token: str,
    *,
    create_root: bool = True,
) -> Path:
    try:
        # Serialize stale-lock reclamation as well as publication. Comparing
        # bytes then unlinking without a guard can delete another contender's
        # newly published lock between those two operations.
        with workspace_lock_guard(root):
            return _acquire_workspace_lock_guarded(root, token, create_root=create_root)
    except OSError as exc:
        raise PacWorkspaceError(
            f"无法建立 PAC 缓存工作区：{root}\n"
            "缓存目录的创建、写入或锁定失败，并非 PAC 内容解析错误。"
            "请将完整工具目录移到本地可写的较短路径，或通过 "
            "TIS_RETEXT_DATA_DIR 指定其他数据目录后重新启动。\n"
            f"系统信息：{exc}"
        ) from exc


def _acquire_workspace_lock_guarded(
    root: Path, token: str, *, create_root: bool,
) -> Path:
    if create_root:
        root.mkdir(parents=True, exist_ok=True)
    elif not root.is_dir():
        raise PacWorkspaceError("PAC 工作区已被其他操作移走。")
    lock_path = root / "lock.json"
    payload = {
        "pid": os.getpid(),
        "token": token,
        "created": _utc_now(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    # Exclusive creation supplies a short collision-safe name. The ownership
    # token remains full length inside the JSON, not duplicated in the path.
    fd, prepared_name = tempfile.mkstemp(prefix=".l-", suffix=".tmp", dir=root)
    prepared = Path(prepared_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        for _attempt in range(3):
            try:
                if os.name == "nt":
                    # Windows rename never overwrites an existing destination.
                    # Publish complete JSON without requiring NTFS hard links.
                    # Do NOT use os.replace or shutil.move here.
                    os.rename(prepared, lock_path)
                else:
                    # POSIX rename would overwrite the current owner's lock.
                    os.link(prepared, lock_path)
                return lock_path
            except FileExistsError:
                current, raw = _read_lock(lock_path)
                pid = _lock_pid(current)
                if pid <= 0:
                    raise PacWorkspaceError(
                        f"无法确认工作区锁的归属，已保留缓存：{lock_path}。"
                        "请关闭其他工具实例，或选择其他数据目录后重试。"
                    )
                if _pid_is_alive(pid):
                    raise PacWorkspaceError(
                        f"PAC 工作区正被另一个程序实例使用（PID {pid}）。"
                    )
                # Only remove the stale file if it is still the exact lock we
                # inspected. This avoids deleting a concurrently acquired
                # live lock after a stale-lock race.
                try:
                    if lock_path.read_bytes() != raw:
                        continue
                    lock_path.unlink()
                except FileNotFoundError:
                    continue
        raise PacWorkspaceError("无法取得 PAC 工作区锁，请稍后重试。")
    finally:
        prepared.unlink(missing_ok=True)


def _release_workspace_lock(path: Path, token: str) -> None:
    with workspace_lock_guard(path.parent):
        current, _raw = _read_lock(path)
        if current.get("token") == token:
            path.unlink(missing_ok=True)


def _directory_size(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a portable existence probe on Windows; zero
        # is passed to TerminateProcess there.  Query a synchronization handle
        # instead so checking a workspace lock can never signal the process.
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        error_access_denied = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        )
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True
