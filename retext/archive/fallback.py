from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ..paths import REPO_ROOT, ensure_staging_root
from .fpac import FpacArchiveService


class PacFallbackTools:
    """Explicit adapter for the original upstream scripts.

    This adapter is intentionally not used by the normal PAC service.  It
    exists only as a user-selected recovery path and runs the scripts in an
    isolated project staging directory.
    """

    def __init__(self, tools_root: str | Path | None = None) -> None:
        self.tools_root = (
            Path(tools_root).resolve()
            if tools_root is not None
            else REPO_ROOT / "pac_tools"
        )
        self.extract_script = self.tools_root / "extract_pac.py"
        self.create_script = self.tools_root / "create_pac.py"

    def available(self) -> bool:
        return self._can_extract() and self._can_build()

    def extract(self, pac_path: str | Path, output_dir: str | Path) -> Path:
        self._require_tools()
        source = Path(pac_path).resolve()
        target = Path(output_dir).resolve()
        archive = FpacArchiveService().inspect(source)
        target.mkdir(parents=True, exist_ok=True)
        if any(target.iterdir()):
            raise RuntimeError("回退解包只允许写入空目录，以避免覆盖现有文件。")

        staging_root = ensure_staging_root()
        with tempfile.TemporaryDirectory(prefix="pac_fallback_", dir=staging_root) as temp_name:
            run_root = Path(temp_name) / "run"
            run_root.mkdir()
            if getattr(sys, "frozen", False):
                self._run(
                    [
                        sys.executable,
                        "--pac-fallback-child",
                        "extract",
                        str(self.extract_script),
                        str(source),
                        str(run_root),
                    ],
                    cwd=run_root,
                )
            else:
                code = (
                    "import importlib.util, os, sys;"
                    "script,pac,out=sys.argv[1:4];"
                    "spec=importlib.util.spec_from_file_location('pac_fallback_extract',script);"
                    "mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);"
                    "os.chdir(out);mod.process_pac(pac)"
                )
                self._run(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(self.extract_script),
                        str(source),
                        str(run_root),
                    ],
                    cwd=run_root,
                )

            for entry in archive.entries:
                extracted = run_root.joinpath(*entry.name.split("/"))
                if not extracted.is_file() or _hash_file(extracted) != entry.sha256:
                    raise RuntimeError(f"原始回退工具解包校验失败：{entry.name}")
            for entry in archive.entries:
                extracted = run_root.joinpath(*entry.name.split("/"))
                destination = target.joinpath(*entry.name.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(extracted, destination)

        roots = [path for path in target.iterdir() if path.is_dir()]
        return roots[0] if len(roots) == 1 else target

    def build(
        self,
        folder: str | Path,
        output_path: str | Path,
        *,
        verify: bool = True,
    ) -> Path:
        self._require_tools()
        source_folder = Path(folder).resolve()
        if not source_folder.is_dir():
            raise FileNotFoundError(f"Fallback source folder does not exist: {source_folder}")
        target = Path(output_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target_existed = target.exists()
        target_digest = _hash_file(target) if target_existed else None
        staging_root = ensure_staging_root()
        with tempfile.TemporaryDirectory(prefix="pac_fallback_", dir=staging_root) as temp_name:
            temp = Path(temp_name)
            copied = temp / source_folder.name
            shutil.copytree(source_folder, copied)
            staged_name = "fallback_output.pac"
            if getattr(sys, "frozen", False):
                self._run(
                    [
                        sys.executable,
                        "--pac-fallback-child",
                        "build",
                        str(self.create_script),
                        str(temp),
                        copied.name,
                        staged_name,
                    ],
                    cwd=temp,
                )
            else:
                code = (
                    "import importlib.util, os, sys;"
                    "script,root,folder,name=sys.argv[1:5];"
                    "spec=importlib.util.spec_from_file_location('pac_fallback_create',script);"
                    "mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);"
                    "os.chdir(root);mod.pack_folder(folder,name,overwrite=True)"
                )
                self._run(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(self.create_script),
                        str(temp),
                        copied.name,
                        staged_name,
                    ],
                    cwd=temp,
                )
            staged = temp / staged_name
            if not staged.is_file():
                raise RuntimeError("原始 PAC 回退工具没有生成输出文件。")
            if verify:
                FpacArchiveService().inspect(staged)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                shutil.copyfile(staged, temporary)
                if target_existed:
                    if not target.exists() or _hash_file(target) != target_digest:
                        raise RuntimeError(f"目标 PAC 在回退构建期间发生变化：{target}")
                    backup = Path(f"{target}.bak")
                    if not backup.exists():
                        shutil.copyfile(target, backup)
                elif target.exists():
                    raise RuntimeError(f"目标 PAC 在回退构建期间被其他程序创建：{target}")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return target

    def _can_extract(self) -> bool:
        return self.extract_script.is_file()

    def _can_build(self) -> bool:
        return self.create_script.is_file()

    def _require_tools(self) -> None:
        if not self.available():
            raise FileNotFoundError(
                f"当前运行方式缺少可用的原始 PAC 回退工具：{self.tools_root}"
            )

    @staticmethod
    def _run(command: list[str], *, cwd: Path) -> None:
        options: dict[str, object] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "check": False,
            "cwd": cwd,
            "env": {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        completed = subprocess.run(command, **options)
        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                f"原始 PAC 回退工具执行失败（exit={completed.returncode}）：{details}"
            )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run_fallback_child(arguments: list[str]) -> int:
    """Run a bundled GPL fallback script in a separate app subprocess."""

    if not arguments:
        raise ValueError("Missing PAC fallback child operation.")
    operation = arguments[0]
    if operation == "extract" and len(arguments) == 4:
        _operation, script, pac_path, output_dir = arguments
        module = _load_fallback_module(Path(script), "pac_fallback_extract")
        output = Path(output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        previous = Path.cwd()
        try:
            os.chdir(output)
            module.process_pac(str(Path(pac_path).resolve()))
        finally:
            os.chdir(previous)
        return 0
    if operation == "build" and len(arguments) == 5:
        _operation, script, root, folder_name, output_name = arguments
        module = _load_fallback_module(Path(script), "pac_fallback_build")
        run_root = Path(root).resolve()
        previous = Path.cwd()
        try:
            os.chdir(run_root)
            module.pack_folder(folder_name, output_name, overwrite=True)
        finally:
            os.chdir(previous)
        return 0
    raise ValueError(f"Invalid PAC fallback child arguments: {arguments!r}")


def _load_fallback_module(path: Path, module_name: str):
    source = path.resolve()
    if not source.is_file() or source.suffix.casefold() != ".py":
        raise FileNotFoundError(f"PAC fallback script is unavailable: {source}")
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load PAC fallback script: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
