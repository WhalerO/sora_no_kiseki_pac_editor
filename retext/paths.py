from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
ENGINES_ROOT = PACKAGE_ROOT / "engines"
LEGACY_ROOT = ENGINES_ROOT / "legacy"
KURO_ROOT = ENGINES_ROOT / "kuro"

DATA_DIR_ENV = "TIS_RETEXT_DATA_DIR"


def _resolve_data_root(
    *,
    configured: str,
    frozen: bool,
    executable: str | Path,
) -> Path:
    if configured.strip():
        return Path(configured).expanduser().resolve()
    if not frozen:
        return REPO_ROOT / ".runtime"
    # Keep portable caches beside the exe, not in _MEIPASS or on the system drive.
    return Path(executable).resolve().parent / "TIS_Retext_Data"


DATA_ROOT = _resolve_data_root(
    configured=os.environ.get(DATA_DIR_ENV, ""),
    frozen=bool(getattr(sys, "frozen", False)),
    executable=sys.executable,
)

RUNTIME_ROOT = DATA_ROOT / "transient"
WORKSPACES_ROOT = DATA_ROOT / "workspaces"
STAGING_ROOT = DATA_ROOT / "staging"
TRASH_ROOT = DATA_ROOT / "trash"


def ensure_data_root() -> Path:
    try:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"无法创建程序数据目录：{DATA_ROOT}。"
            f"请将程序放在可写目录，或设置 {DATA_DIR_ENV}。"
        ) from exc
    return DATA_ROOT


def ensure_runtime_root() -> Path:
    ensure_data_root()
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    return RUNTIME_ROOT


def ensure_workspaces_root() -> Path:
    ensure_data_root()
    WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)
    return WORKSPACES_ROOT


def ensure_staging_root() -> Path:
    ensure_data_root()
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    return STAGING_ROOT


def ensure_trash_root() -> Path:
    ensure_data_root()
    TRASH_ROOT.mkdir(parents=True, exist_ok=True)
    return TRASH_ROOT


def create_runtime_dir(prefix: str) -> Path:
    # mkdtemp reserves the name exclusively; a 32-character UUID needlessly
    # consumed Windows' path budget before a PAC entry was even materialized.
    return Path(tempfile.mkdtemp(prefix=f"{prefix}_", dir=ensure_runtime_root()))


def prepare_runtime_dir(name: str) -> Path:
    runtime_dir = ensure_runtime_root() / name
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir, ignore_errors=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def cleanup_runtime_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
