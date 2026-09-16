from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable, Literal

from .fpac import _validate_entry_name
from .domain import PacEntry
from .session import PacProjectSession
from .workspace import PacWorkspaceManager


PacNodeKind = Literal["pac", "folder", "file"]


@dataclass(slots=True, frozen=True)
class PacNodeRef:
    workspace_id: str
    kind: PacNodeKind
    path: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return self.workspace_id, self.kind, self.path


@dataclass(slots=True, frozen=True)
class PacMaterializedEntry:
    workspace_id: str
    pac_path: Path
    entry_name: str
    path: Path
    size: int
    suffix: str

    @property
    def logical_path(self) -> str:
        return self.entry_name


class PacWorkbench:
    """Owns the set of PAC projects visible in one application window."""

    def __init__(self, manager: PacWorkspaceManager | None = None) -> None:
        self.manager = manager or PacWorkspaceManager()
        self._projects: dict[str, PacProjectSession] = {}

    def projects(self) -> list[PacProjectSession]:
        return list(self._projects.values())

    def open(self, pac_path: str | Path) -> PacProjectSession:
        archive = self.manager.service.inspect(pac_path)
        for workspace_id, project in list(self._projects.items()):
            current = project.workspace.archive
            if not _same_path(current.source_path, archive.source_path):
                continue
            if current.source_sha256 == archive.source_sha256:
                return project
            # The caller explicitly opened the same source path again.  Rotate
            # to a workspace for its current bytes while retaining the old
            # cache generation on disk for recovery.
            self.close(workspace_id)
            break

        workspace = self.manager.open_archive(archive)
        existing = self._projects.get(workspace.workspace_id)
        if existing is not None:
            return existing
        project = PacProjectSession(workspace)
        self._projects[workspace.workspace_id] = project
        return project

    def get(self, workspace_id: str) -> PacProjectSession:
        try:
            return self._projects[workspace_id]
        except KeyError as exc:
            raise KeyError(f"PAC project is not open: {workspace_id}") from exc

    def close(self, workspace_id: str) -> None:
        project = self.get(workspace_id)
        project.close()
        del self._projects[workspace_id]

    def close_all(self) -> None:
        for workspace_id in list(self._projects):
            self.close(workspace_id)

    def reload_from_source(
        self,
        workspace_id: str,
        *,
        discard_cache: bool = False,
    ) -> PacProjectSession:
        """Close one project and reopen the current bytes at its source path.

        Persistent managers move an explicitly discarded workspace to managed
        trash. Ephemeral managers always destroy the closed session workspace,
        so reopening necessarily materializes fresh bytes from the source PAC.
        """

        project = self.get(workspace_id)
        source = project.workspace.archive.source_path
        self.close(workspace_id)
        try:
            if discard_cache and not self.manager.ephemeral:
                self.manager.delete(workspace_id, allow_dirty=True)
            return self.open(source)
        except Exception:
            # If deletion itself failed, the old workspace is still available
            # and should be reopened rather than leaving the UI detached.
            old_workspace_still_exists = (
                self.manager.workspaces_root / workspace_id
            ).exists()
            if (
                not self.manager.ephemeral
                and (not discard_cache or old_workspace_still_exists)
            ):
                try:
                    self.open(source)
                except Exception:
                    pass
            raise

    def expand_refs(
        self,
        refs: Iterable[PacNodeRef],
        *,
        editable_only: bool = True,
    ) -> list[tuple[PacProjectSession, PacEntry]]:
        resolved: list[tuple[PacProjectSession, PacEntry]] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            project = self.get(ref.workspace_id)
            entries = self._entries_for_ref(project, ref)
            for entry in entries:
                key = (ref.workspace_id, entry.name)
                if key in seen or (editable_only and not entry.editable):
                    continue
                seen.add(key)
                resolved.append((project, entry))
        return resolved

    def materialize_refs(
        self,
        refs: Iterable[PacNodeRef],
        *,
        editable_only: bool = True,
    ) -> list[PacMaterializedEntry]:
        resolved = self.expand_refs(refs, editable_only=editable_only)
        names_by_workspace: dict[str, list[str]] = {}
        for project, entry in resolved:
            names_by_workspace.setdefault(project.workspace.workspace_id, []).append(entry.name)
        for workspace_id, names in names_by_workspace.items():
            self.get(workspace_id).workspace.materialize_entries(names)

        targets: list[PacMaterializedEntry] = []
        for project, entry in resolved:
            targets.append(
                PacMaterializedEntry(
                    workspace_id=project.workspace.workspace_id,
                    pac_path=project.workspace.archive.source_path,
                    entry_name=entry.name,
                    path=project.workspace.entry_path(entry),
                    size=project.workspace.current_size(entry.name),
                    suffix=entry.suffix,
                )
            )
        return targets

    def export_refs(self, refs: Iterable[PacNodeRef], output_directory: str | Path) -> list[Path]:
        """Extract selected files/folders with their logical paths and staged edits."""
        resolved = self.expand_refs(refs, editable_only=False)
        root = Path(output_directory).resolve()
        projects = {project.workspace.workspace_id: project for project, _ in resolved}
        folders = {key: project.workspace.archive.source_path.name for key, project in projects.items()}
        if len(set(value.casefold() for value in folders.values())) != len(folders):
            folders = {key: f"{value}-{key}" for key, value in folders.items()}
        planned: list[tuple[PacProjectSession, PacEntry, Path]] = []
        targets: set[str] = set()
        source_pacs = {project.workspace.archive.source_path for project in self.projects()}
        for project, entry in resolved:
            _validate_entry_name(entry.name)
            prefix = root / folders[project.workspace.workspace_id] if len(projects) > 1 else root
            target = (prefix / entry.name).resolve()
            if not target.is_relative_to(root) or target in source_pacs:
                raise ValueError(f"无效的提取目标：{target}")
            folded = os.path.normcase(str(target))
            if folded in targets or target.exists():
                raise FileExistsError(f"提取目标已存在，请选择空目录：{target}")
            if any(parent.exists() and not parent.is_dir() for parent in target.parents):
                raise ValueError(f"提取路径与已有文件冲突：{target}")
            targets.add(folded)
            planned.append((project, entry, target))
        for _project, _entry, target in planned:
            if any(os.path.normcase(str(parent)) in targets for parent in target.parents):
                raise ValueError(f"包内文件与文件夹路径冲突：{target}")
        written: list[Path] = []
        for project, entry, target in planned:
            project.workspace.export_entry(entry.name, target)
            written.append(target)
        return written

    def mirror_refs(
        self,
        workspace_id: str,
        refs: Iterable[PacNodeRef],
    ) -> list[PacNodeRef]:
        """Apply one logical PAC-tree scope to another open PAC.

        PAC and folder scopes can legitimately resolve to no entries on one
        side. File scopes that do not exist on the target side are omitted so
        the diff layer can report them as only-old or only-new.
        """

        entry_names = {entry.name for entry in self.get(workspace_id).workspace.entries()}
        mirrored: list[PacNodeRef] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            key = (ref.kind, ref.path)
            if key in seen:
                continue
            seen.add(key)
            if ref.kind == "file" and ref.path not in entry_names:
                continue
            mirrored.append(PacNodeRef(workspace_id, ref.kind, ref.path))
        return mirrored

    def refresh_materialized(
        self,
        targets: Iterable[PacMaterializedEntry],
    ) -> None:
        seen: set[tuple[str, str]] = set()
        for target in targets:
            key = (target.workspace_id, target.entry_name)
            if key in seen:
                continue
            seen.add(key)
            self.get(target.workspace_id).workspace.refresh_entry(target.entry_name)

    @staticmethod
    def _entries_for_ref(
        project: PacProjectSession,
        ref: PacNodeRef,
    ) -> list[PacEntry]:
        workspace = project.workspace
        if ref.kind == "pac":
            return workspace.entries()
        if ref.kind == "file":
            return [workspace.get_entry(ref.path)]
        prefix = ref.path.rstrip("/") + "/"
        return [
            entry
            for entry in workspace.entries()
            if entry.name.startswith(prefix)
        ]


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(
        str(right.resolve())
    )
