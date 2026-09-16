from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .paths import RUNTIME_ROOT, ensure_runtime_root

TRANSIENT_PREFIXES = (
    "kuro_dat_work",
    "kuro_dat_build",
    "tis_retext_dat_",
    "tis_retext_dat_build_",
)


@dataclass(slots=True)
class RuntimeEntry:
    name: str
    path: Path
    is_dir: bool
    size_bytes: int


def list_runtime_entries() -> list[RuntimeEntry]:
    root = ensure_runtime_root()
    entries: list[RuntimeEntry] = []
    for path in _safe_iterdir(root):
        entries.append(
            RuntimeEntry(
                name=path.name,
                path=path,
                is_dir=path.is_dir(),
                size_bytes=_compute_size(path),
            )
        )
    return entries


def cleanup_runtime(*, remove_all: bool = False) -> list[Path]:
    root = ensure_runtime_root()
    removed: list[Path] = []
    for path in _safe_iterdir(root):
        if not remove_all and not _is_transient(path):
            continue
        _remove_path(path)
        if not path.exists():
            removed.append(path)
    return sorted(removed)


def describe_runtime() -> dict[str, int]:
    entries = list_runtime_entries()
    return {
        "entry_count": len(entries),
        "total_bytes": sum(entry.size_bytes for entry in entries),
        "transient_count": sum(1 for entry in entries if _is_transient(entry.path)),
    }


def _is_transient(path: Path) -> bool:
    if path.name == "__pycache__":
        return True
    return any(path.name.startswith(prefix) for prefix in TRANSIENT_PREFIXES)


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _compute_size(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def _safe_iterdir(root: Path) -> list[Path]:
    try:
        return sorted(root.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return []
