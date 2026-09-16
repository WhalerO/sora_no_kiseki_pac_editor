from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


VLC_RUNTIME_ENV = "TIS_RETEXT_VLC_DIR"


@dataclass(frozen=True)
class VlcRuntime:
    """A complete Windows LibVLC runtime usable by python-vlc."""

    root: Path
    libvlc: Path
    plugins: Path
    source: str


_DLL_DIRECTORY_HANDLES: list[object] = []
_DLL_DIRECTORY_ROOTS: set[str] = set()


def find_vlc_runtime() -> VlcRuntime | None:
    """Find a complete LibVLC runtime without importing python-vlc.

    Explicit configuration wins over the bundled runtime. Invalid candidates are
    skipped here so source runs can still fall back to an installed VLC; the
    release build script performs strict validation of explicit build inputs.
    """

    seen: set[str] = set()
    for root, source in _runtime_candidates():
        try:
            resolved = root.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)

        runtime = _runtime_at(resolved, source)
        if runtime is not None:
            return runtime
    return None


def configure_vlc_runtime() -> VlcRuntime | None:
    """Configure DLL and plugin lookup before the caller imports ``vlc``.

    Returns ``None`` when there is no application-managed runtime. That is not
    itself an error: python-vlc may still discover LibVLC through the operating
    system, and the playback layer can report an actionable import/load error.
    """

    runtime = find_vlc_runtime()
    if runtime is None:
        return None

    # python-vlc supports these two explicit overrides. Setting them avoids
    # relying on its registry/current-directory fallback, which is especially
    # important inside a PyInstaller onedir bundle.
    os.environ["PYTHON_VLC_LIB_PATH"] = str(runtime.libvlc)
    os.environ["PYTHON_VLC_MODULE_PATH"] = str(runtime.plugins)
    os.environ["VLC_PLUGIN_PATH"] = str(runtime.plugins)
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if not any(
        os.path.normcase(entry) == os.path.normcase(str(runtime.root))
        for entry in path_entries
        if entry
    ):
        os.environ["PATH"] = os.pathsep.join(
            [str(runtime.root), *[entry for entry in path_entries if entry]]
        )

    add_dll_directory = getattr(os, "add_dll_directory", None)
    root_key = os.path.normcase(str(runtime.root))
    if add_dll_directory is not None and root_key not in _DLL_DIRECTORY_ROOTS:
        handle = add_dll_directory(str(runtime.root))
        # Closing the handle removes the search path, so retain it for the
        # process lifetime.
        _DLL_DIRECTORY_HANDLES.append(handle)
        _DLL_DIRECTORY_ROOTS.add(root_key)
    return runtime


def _runtime_candidates() -> Iterator[tuple[Path, str]]:
    explicit = os.environ.get(VLC_RUNTIME_ENV)
    if explicit:
        yield Path(explicit), VLC_RUNTIME_ENV

    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        yield Path(bundle_root) / "libvlc", "bundled"

    executable_dir = Path(sys.executable).resolve().parent
    yield executable_dir / "_internal" / "libvlc", "bundled"

    project_root = Path(__file__).resolve().parent.parent
    yield project_root / "_vendor" / "libvlc" / "win-x64", "vendored"

    if sys.platform != "win32":
        return

    yield from _registry_candidates()
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        program_files = os.environ.get(variable)
        if program_files:
            yield Path(program_files) / "VideoLAN" / "VLC", "installed"


def _registry_candidates() -> Iterator[tuple[Path, str]]:
    try:
        import winreg
    except ImportError:
        return

    locations = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VideoLAN\VLC"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\VideoLAN\VLC"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\VideoLAN\VLC"),
    )
    for hive, key_name in locations:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                install_dir, _ = winreg.QueryValueEx(key, "InstallDir")
        except OSError:
            continue
        if install_dir:
            yield Path(install_dir), "installed"


def _runtime_at(root: Path, source: str) -> VlcRuntime | None:
    libvlc = root / "libvlc.dll"
    core = root / "libvlccore.dll"
    plugins = root / "plugins"
    try:
        has_plugin = plugins.is_dir() and any(
            child.is_file() and child.suffix.casefold() == ".dll"
            for child in plugins.rglob("*")
        )
    except OSError:
        return None
    if not (libvlc.is_file() and core.is_file() and has_plugin):
        return None
    return VlcRuntime(root=root, libvlc=libvlc, plugins=plugins, source=source)
