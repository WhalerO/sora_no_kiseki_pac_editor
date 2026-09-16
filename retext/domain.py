from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


TEXT_DOCUMENT_SUFFIXES = frozenset({".tbl", ".dat"})


def is_text_document_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in TEXT_DOCUMENT_SUFFIXES


class DocumentKind(str, Enum):
    TBL = "tbl"
    DAT = "dat"

    @classmethod
    def from_path(cls, path: str | Path) -> "DocumentKind":
        suffix = Path(path).suffix.lower()
        if suffix == ".tbl":
            return cls.TBL
        if suffix == ".dat":
            return cls.DAT
        raise ValueError(f"Unsupported file extension: {suffix}")


class CapabilityLevel(str, Enum):
    STABLE = "stable"
    EXPERIMENTAL = "experimental"
    UNSUPPORTED = "unsupported"


class WorkflowMode(str, Enum):
    AGILE = "agile"
    SAFE = "safe"


class GameVersion(str, Enum):
    """Schema family requested by the caller.

    PAC is only a container and does not carry a trustworthy 1st/2nd marker.
    ``AUTO`` therefore means "infer from each TBL layout", while an explicit
    value is a user override that is kept through load, save and verification.
    """

    AUTO = "auto"
    SORA1 = "Sora1"
    SORA2 = "Sora2"

    @classmethod
    def normalize(cls, value: "GameVersion | str | None") -> "GameVersion":
        if isinstance(value, cls):
            return value
        probe = str(value or cls.AUTO.value).strip().lower()
        aliases = {
            "auto": cls.AUTO,
            "sora1": cls.SORA1,
            "1st": cls.SORA1,
            "sora2": cls.SORA2,
            "2nd": cls.SORA2,
        }
        try:
            return aliases[probe]
        except KeyError as exc:
            raise ValueError(f"Unsupported game version: {value}") from exc


@dataclass(slots=True)
class EngineCapability:
    engine: str
    kind: DocumentKind
    operation: str
    level: CapabilityLevel
    notes: str = ""


@dataclass(slots=True)
class TextUnit:
    index: int
    original_text: str
    current_text: str
    location: str
    context: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.original_text != self.current_text


@dataclass(slots=True)
class SavePlan:
    engine: str
    mode: str
    safe: bool
    requires_rebuild: bool
    notes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TextDocument:
    source_path: Path
    kind: DocumentKind
    engine: str
    units: list[TextUnit]
    metadata: dict[str, Any] = field(default_factory=dict)
    state: Any = None
    _unit_by_index: dict[int, TextUnit] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.rebuild_index()

    def changed_units(self) -> list[TextUnit]:
        return [unit for unit in self.units if unit.changed]

    def rebuild_index(self) -> None:
        self._unit_by_index = {unit.index: unit for unit in self.units}
        if len(self._unit_by_index) != len(self.units):
            raise ValueError("Text document contains duplicate unit indexes.")

    def get_unit(self, index: int) -> TextUnit:
        try:
            return self._unit_by_index[index]
        except KeyError as exc:
            raise IndexError(f"Text unit index out of range: {index}") from exc

    def update_unit(self, index: int, new_text: str) -> None:
        self.get_unit(index).current_text = new_text
