from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..io_utils import atomic_write_bytes
from ..session import DocumentSession, SessionOptions
from .domain import PacBuildReport, PacEntry
from .workspace import PacWorkspace


class PacProjectSession:
    """Binds one managed PAC workspace to the existing text editor session."""

    def __init__(self, workspace: PacWorkspace) -> None:
        self.workspace = workspace
        self.document_session = DocumentSession()
        self.current_entry: PacEntry | None = None

    @property
    def document(self):
        return self.document_session.document

    def open_entry(
        self,
        entry_name: str,
        *,
        options: SessionOptions | None = None,
    ):
        entry = self.workspace.get_entry(entry_name)
        if not entry.editable:
            raise ValueError(f"PAC entry is not a supported text document: {entry_name}")
        materialized = self.workspace.materialize(entry_name)
        chosen = options or SessionOptions()
        chosen = replace(chosen, do_backup=False)
        if entry.suffix == ".tbl" and not chosen.schema_hint:
            # Managed cache filenames are numeric and intentionally do not
            # expose archive paths. Preserve filename-based schema routing
            # explicitly for TBL engines.
            chosen = replace(chosen, schema_hint=Path(entry.name).stem)
        document = self.document_session.open_document(materialized, options=chosen)
        document.metadata["pac_entry_name"] = entry.name
        document.metadata["pac_workspace_id"] = self.workspace.workspace_id
        self.current_entry = entry
        return document

    def save_current(self) -> Path:
        if self.current_entry is None:
            raise RuntimeError("No PAC entry is currently open.")
        saved = self.document_session.save()
        self.workspace.refresh_entry(self.current_entry.name)
        return saved

    def export_current(self, target: str | Path) -> Path:
        if self.current_entry is None:
            raise RuntimeError("No PAC entry is currently open.")
        if self.document_session.changed_count():
            self.save_current()
        source = self.workspace.entry_path(self.current_entry)
        output = Path(target).resolve()
        if output == self.workspace.archive.source_path.resolve():
            raise ValueError("提取文件不能覆盖源 PAC。请使用“导出 PAC”重建容器。")
        atomic_write_bytes(output, source.read_bytes(), do_backup=True)
        return output

    def build(
        self,
        output_path: str | Path,
        *,
        do_backup: bool = True,
        allow_integrity_errors: bool = False,
    ) -> PacBuildReport:
        current_name = self.current_entry.name if self.current_entry is not None else None
        if self.current_entry is not None and self.document_session.changed_count():
            self.save_current()
        report = self.workspace.build(
            output_path,
            do_backup=do_backup,
            allow_integrity_errors=allow_integrity_errors,
        )
        if current_name is not None:
            self.current_entry = self.workspace.get_entry(current_name)
            if self.document_session.document.source_path != self.workspace.entry_path(self.current_entry):
                self.open_entry(current_name, options=self.document_session.options)
        return report

    def close(self) -> None:
        self.current_entry = None
        self.document_session.document = None
        self.workspace.close()
