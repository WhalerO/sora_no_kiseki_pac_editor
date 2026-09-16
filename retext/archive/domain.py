from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..domain import is_text_document_path


@dataclass(slots=True, frozen=True)
class PacEntry:
    header_index: int
    name_order: int
    data_order: int
    name: str
    path_hash: int
    reserved: int
    name_offset: int
    size: int
    data_offset: int
    sha256: str

    @property
    def suffix(self) -> str:
        return Path(self.name).suffix.lower()

    @property
    def editable(self) -> bool:
        return is_text_document_path(self.name)


@dataclass(slots=True)
class PacArchive:
    source_path: Path
    source_size: int
    source_mtime_ns: int
    source_sha256: str
    header_size: int
    format_version: int
    entries: list[PacEntry]
    _entry_by_name: dict[str, PacEntry] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._entry_by_name = {entry.name: entry for entry in self.entries}
        if len(self._entry_by_name) != len(self.entries):
            raise ValueError("PAC contains duplicate entry names.")

    def get_entry(self, name: str) -> PacEntry:
        try:
            return self._entry_by_name[name]
        except KeyError as exc:
            raise KeyError(f"PAC entry does not exist: {name}") from exc

    def editable_entries(self) -> list[PacEntry]:
        return [entry for entry in self.entries if entry.editable]


@dataclass(slots=True, frozen=True)
class PacBuildReport:
    output_path: Path
    output_size: int
    output_sha256: str
    entry_count: int
    changed_entries: tuple[str, ...]
    verified_archive: PacArchive | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(slots=True, frozen=True)
class PacWorkspaceSummary:
    workspace_id: str
    root: Path
    source_path: Path
    state: str
    entry_count: int
    dirty_count: int
    materialized_count: int
    size_bytes: int
    last_opened: str
    last_export: str
