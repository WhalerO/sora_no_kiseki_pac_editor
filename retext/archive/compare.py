from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..paths import ensure_staging_root
from .domain import PacArchive, PacEntry
from .fpac import FpacArchiveService


@dataclass(slots=True, frozen=True)
class PacDiffFileRow:
    rel: str
    old_path: str | None
    new_path: str | None
    old_size: int
    new_size: int
    status: str
    old_archive: PacArchive | None
    old_entry: PacEntry | None
    new_archive: PacArchive | None
    new_entry: PacEntry | None


class PacComparisonSession:
    """Compares PAC files/groups without joining the editable PAC workbench."""

    def __init__(
        self,
        service: FpacArchiveService | None = None,
        *,
        staging_root: str | Path | None = None,
    ) -> None:
        self.service = service or FpacArchiveService()
        self.staging_root = (
            Path(staging_root).resolve()
            if staging_root is not None
            else ensure_staging_root()
        )
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.root = self.staging_root / f"pac_compare_{uuid4().hex}"

    def compare(
        self,
        old_inputs: list[str | Path],
        new_inputs: list[str | Path],
    ) -> list[PacDiffFileRow]:
        old_sources = self._collect_pacs(old_inputs, "旧版本")
        new_sources = self._collect_pacs(new_inputs, "新版本")
        if len(old_sources) == len(new_sources) == 1:
            old_sources = {"": next(iter(old_sources.values()))}
            new_sources = {"": next(iter(new_sources.values()))}
        else:
            old_sources = {key.casefold(): value for key, value in old_sources.items()}
            new_sources = {key.casefold(): value for key, value in new_sources.items()}

        old_archives = {
            key: self.service.inspect(path)
            for key, path in old_sources.items()
        }
        new_archives = {
            key: self.service.inspect(path)
            for key, path in new_sources.items()
        }
        rows: list[PacDiffFileRow] = []
        for pac_key in sorted(set(old_archives) | set(new_archives)):
            old_archive = old_archives.get(pac_key)
            new_archive = new_archives.get(pac_key)
            old_entries = (
                {entry.name: entry for entry in old_archive.entries}
                if old_archive
                else {}
            )
            new_entries = (
                {entry.name: entry for entry in new_archive.entries}
                if new_archive
                else {}
            )
            for entry_name in sorted(set(old_entries) | set(new_entries)):
                old_entry = old_entries.get(entry_name)
                new_entry = new_entries.get(entry_name)
                if old_entry and new_entry:
                    status = (
                        "same"
                        if old_entry.size == new_entry.size
                        and old_entry.sha256 == new_entry.sha256
                        else "modified"
                    )
                elif old_entry:
                    status = "only_old"
                else:
                    status = "only_new"
                rel = f"{pac_key} :: {entry_name}" if pac_key else entry_name
                rows.append(
                    PacDiffFileRow(
                        rel=rel,
                        old_path=None,
                        new_path=None,
                        old_size=old_entry.size if old_entry else 0,
                        new_size=new_entry.size if new_entry else 0,
                        status=status,
                        old_archive=old_archive,
                        old_entry=old_entry,
                        new_archive=new_archive,
                        new_entry=new_entry,
                    )
                )
        return rows

    def materialize_row(
        self,
        row: PacDiffFileRow,
    ) -> tuple[str | None, str | None, str]:
        logical_path = (
            row.old_entry.name
            if row.old_entry is not None
            else row.new_entry.name
            if row.new_entry is not None
            else row.rel
        )
        old_path = self._materialize("old", row.old_archive, row.old_entry)
        new_path = self._materialize("new", row.new_archive, row.new_entry)
        return (
            str(old_path) if old_path else None,
            str(new_path) if new_path else None,
            logical_path,
        )

    def close(self) -> None:
        if not self.root.exists():
            return
        resolved = self.root.resolve()
        if (
            resolved.parent != self.staging_root.resolve()
            or not resolved.name.startswith("pac_compare_")
        ):
            raise RuntimeError(f"拒绝清理异常 PAC 对比暂存目录：{resolved}")
        shutil.rmtree(resolved)

    def _materialize(
        self,
        side: str,
        archive: PacArchive | None,
        entry: PacEntry | None,
    ) -> Path | None:
        if archive is None or entry is None:
            return None
        suffix = entry.suffix if entry.suffix else ".bin"
        target = (
            self.root
            / side
            / archive.source_sha256[:16]
            / f"{entry.data_order:06d}{suffix}"
        )
        if not target.is_file():
            self.service.extract_entry(archive, entry.name, target)
        return target

    @staticmethod
    def _collect_pacs(
        inputs: list[str | Path],
        side_label: str,
    ) -> dict[str, Path]:
        collected: dict[str, Path] = {}
        keys_by_casefold: dict[str, str] = {}
        duplicates: set[str] = set()
        for raw in inputs:
            path = Path(raw).resolve()
            if path.is_file():
                candidates = [(path.name, path)] if path.suffix.lower() == ".pac" else []
            elif path.is_dir():
                candidates = [
                    (candidate.relative_to(path).as_posix(), candidate)
                    for candidate in sorted(path.rglob("*.pac"))
                    if candidate.is_file()
                ]
            else:
                continue
            for key, candidate in candidates:
                normalized = key.casefold()
                if normalized in keys_by_casefold:
                    duplicates.add(key)
                    continue
                keys_by_casefold[normalized] = key
                collected[key] = candidate
        if duplicates:
            names = "、".join(sorted(duplicates)[:5])
            raise ValueError(f"{side_label} PAC 组中存在重复相对路径：{names}")
        if not collected:
            raise ValueError(f"{side_label}没有找到 PAC 文件。")
        return collected
