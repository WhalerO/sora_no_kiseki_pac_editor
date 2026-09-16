from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path


def atomic_write_bytes(
    path: str | Path,
    payload: bytes,
    *,
    do_backup: bool = False,
    expected_bytes: bytes | None = None,
) -> Path:
    """Write bytes without exposing a partially written target.

    When ``expected_bytes`` is provided, an existing target must still match the
    bytes that were originally loaded.  This prevents a stale editor session
    from silently overwriting an externally changed file.
    """

    target = Path(path).resolve()
    current = target.read_bytes() if target.exists() else None
    if expected_bytes is not None and current is not None and current != expected_bytes:
        raise RuntimeError(f"Refusing to overwrite a file that changed on disk: {target}")

    if do_backup and current is not None:
        backup = Path(f"{target}.bak")
        if not backup.exists():
            _write_new_file(backup, current)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_with_retry(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def atomic_copy_file(
    source_path: str | Path,
    target_path: str | Path,
) -> Path:
    """Copy a file without exposing a partial destination or loading it in memory."""

    source = Path(source_path).resolve()
    target = Path(target_path).resolve()
    before = source.stat()
    target.parent.mkdir(parents=True, exist_ok=True)
    target_before = target.stat() if target.exists() else None
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_stream, os.fdopen(fd, "wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())

        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError(f"源文件在复制期间发生变化：{source}")

        if target_before is None:
            if target.exists():
                raise RuntimeError(f"目标文件在复制期间被其他程序创建：{target}")
        else:
            if not target.exists():
                raise RuntimeError(f"目标文件在复制期间被其他程序删除：{target}")
            target_after = target.stat()
            if (
                target_before.st_size,
                target_before.st_mtime_ns,
            ) != (
                target_after.st_size,
                target_after.st_mtime_ns,
            ):
                raise RuntimeError(f"目标文件在复制期间发生变化：{target}")

        _replace_with_retry(temporary, target)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return target


def _write_new_file(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return


def _replace_with_retry(source: Path, target: Path) -> None:
    """Tolerate short Windows/OneDrive scanner locks around atomic replace."""

    delays = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0)
    for delay in delays:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt":
                raise
            time.sleep(delay)
    os.replace(source, target)
