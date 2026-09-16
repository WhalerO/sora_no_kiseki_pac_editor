from __future__ import annotations

import hashlib
import os
import shutil
import struct
import tempfile
import zlib
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Mapping

from .domain import PacArchive, PacBuildReport, PacEntry


MAGIC = b"FPAC"
HEADER_STRUCT = struct.Struct("<4s3I")
ENTRY_STRUCT = struct.Struct("<2I3Q")
SUPPORTED_VERSION = 1
MAX_ENTRIES = 1_000_000
MAX_NAME_BYTES = 32_768
COPY_CHUNK_SIZE = 1024 * 1024


class FpacFormatError(ValueError):
    pass


class FpacArchiveService:
    """Strict reader and streaming builder for Trails in the Sky 1st FPAC."""

    def inspect(self, path: str | Path) -> PacArchive:
        source = Path(path).resolve()
        before = source.stat()
        with source.open("rb") as stream:
            header = _read_exact(stream, HEADER_STRUCT.size, "PAC header")
            magic, count, header_size, version = HEADER_STRUCT.unpack(header)
            if magic != MAGIC:
                raise FpacFormatError(f"Unsupported PAC magic: {magic!r}")
            if version != SUPPORTED_VERSION:
                raise FpacFormatError(
                    f"Unsupported FPAC version field: {version}; expected {SUPPORTED_VERSION}."
                )
            if count > MAX_ENTRIES:
                raise FpacFormatError(f"PAC entry count is unreasonable: {count}")

            table_end = HEADER_STRUCT.size + count * ENTRY_STRUCT.size
            if table_end > before.st_size:
                raise FpacFormatError("PAC entry table extends beyond the file.")
            if header_size < table_end or header_size > before.st_size:
                raise FpacFormatError(
                    f"Invalid PAC header size: {header_size} for a {before.st_size}-byte file."
                )

            raw_entries: list[tuple[int, int, int, int, int]] = []
            for index in range(count):
                raw = _read_exact(stream, ENTRY_STRUCT.size, f"PAC entry #{index}")
                raw_entries.append(ENTRY_STRUCT.unpack(raw))

            decoded: list[dict[str, object]] = []
            names_seen: set[str] = set()
            names_casefolded: set[str] = set()
            for header_index, (path_hash, reserved, name_offset, size, data_offset) in enumerate(raw_entries):
                if name_offset < table_end or name_offset >= header_size:
                    raise FpacFormatError(
                        f"PAC entry #{header_index} has an invalid name offset: {name_offset}"
                    )
                name = _read_name(stream, name_offset, header_size)
                _validate_entry_name(name)
                if name in names_seen or name.casefold() in names_casefolded:
                    raise FpacFormatError(f"PAC contains a duplicate or case-colliding name: {name}")
                names_seen.add(name)
                names_casefolded.add(name.casefold())

                expected_hash = zlib.crc32(name.encode("utf-8")) ^ 0xFFFFFFFF
                if path_hash != expected_hash:
                    raise FpacFormatError(
                        f"PAC path hash mismatch for {name}: 0x{path_hash:08X} != 0x{expected_hash:08X}"
                    )
                if data_offset < header_size or data_offset > before.st_size:
                    raise FpacFormatError(f"PAC entry data offset is invalid for {name}.")
                if size > before.st_size - data_offset:
                    raise FpacFormatError(f"PAC entry extends beyond the file: {name}")

                decoded.append(
                    {
                        "header_index": header_index,
                        "name": name,
                        "path_hash": path_hash,
                        "reserved": reserved,
                        "name_offset": name_offset,
                        "size": size,
                        "data_offset": data_offset,
                    }
                )

            if any(
                int(decoded[index]["path_hash"]) > int(decoded[index + 1]["path_hash"])
                for index in range(len(decoded) - 1)
            ):
                raise FpacFormatError("PAC entry table is not ordered by path hash.")

            _validate_non_overlapping_payloads(decoded)
            name_order = {
                id(item): order
                for order, item in enumerate(sorted(decoded, key=lambda item: int(item["name_offset"])))
            }
            data_order = {
                id(item): order
                for order, item in enumerate(sorted(decoded, key=lambda item: int(item["data_offset"])))
            }
            entry_digests, source_digest = _hash_archive_ranges(
                stream,
                decoded,
                before.st_size,
            )

            entries: list[PacEntry] = []
            for item in decoded:
                entries.append(
                    PacEntry(
                        header_index=int(item["header_index"]),
                        name_order=name_order[id(item)],
                        data_order=data_order[id(item)],
                        name=str(item["name"]),
                        path_hash=int(item["path_hash"]),
                        reserved=int(item["reserved"]),
                        name_offset=int(item["name_offset"]),
                        size=int(item["size"]),
                        data_offset=int(item["data_offset"]),
                        sha256=entry_digests[int(item["header_index"])],
                    )
                )

        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"PAC changed while it was being inspected: {source}")
        return PacArchive(
            source_path=source,
            source_size=after.st_size,
            source_mtime_ns=after.st_mtime_ns,
            source_sha256=source_digest,
            header_size=header_size,
            format_version=version,
            entries=entries,
        )

    def source_is_current(self, archive: PacArchive, *, deep: bool = False) -> bool:
        try:
            stat = archive.source_path.stat()
        except OSError:
            return False
        if stat.st_size != archive.source_size or stat.st_mtime_ns != archive.source_mtime_ns:
            return False
        return not deep or _hash_file(archive.source_path) == archive.source_sha256

    def read_entry_prefix(
        self,
        archive: PacArchive,
        entry_name: str,
        limit: int = 512,
    ) -> bytes:
        if limit < 0:
            raise ValueError("Entry prefix limit cannot be negative.")
        if not self.source_is_current(archive):
            raise RuntimeError("源 PAC 已变化，不能继续读取条目。")
        entry = archive.get_entry(entry_name)
        size = min(limit, entry.size)
        with archive.source_path.open("rb") as source:
            source.seek(entry.data_offset)
            return _read_exact(source, size, f"PAC entry prefix: {entry.name}")

    def read_entry_bytes(self, archive: PacArchive, entry_name: str) -> bytes:
        """Read one verified entry without fsync/copying an inspection-only file."""
        if not self.source_is_current(archive):
            raise RuntimeError("源 PAC 已变化，不能继续读取条目。")
        entry = archive.get_entry(entry_name)
        with archive.source_path.open("rb") as source:
            source.seek(entry.data_offset)
            payload = _read_exact(source, entry.size, f"PAC entry: {entry.name}")
        if hashlib.sha256(payload).hexdigest() != entry.sha256:
            raise RuntimeError(f"PAC entry failed read verification: {entry.name}")
        if not self.source_is_current(archive):
            raise RuntimeError("源 PAC 在读取期间发生变化。")
        return payload

    def extract_entry(
        self,
        archive: PacArchive,
        entry_name: str,
        output_path: str | Path,
    ) -> Path:
        # The extracted payload is verified against the digest captured during
        # inspect(), so repeating a whole-archive hash for every lazy entry
        # would add no protection and makes "materialize all" quadratic.
        if not self.source_is_current(archive):
            raise RuntimeError("源 PAC 已变化，不能继续物化缓存条目。")
        entry = archive.get_entry(entry_name)
        target = Path(output_path).resolve()
        if target == archive.source_path.resolve():
            raise ValueError("PAC entry extraction cannot overwrite the source archive.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target_before = target.stat() if target.exists() else None
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with archive.source_path.open("rb") as source, os.fdopen(fd, "wb") as output:
                source.seek(entry.data_offset)
                digest = _copy_range(source, output, entry.size)
                output.flush()
                os.fsync(output.fileno())
            if digest != entry.sha256:
                raise RuntimeError(f"PAC entry failed extraction verification: {entry.name}")
            if target_before is None:
                if target.exists():
                    raise RuntimeError(
                        f"目标文件在提取期间被其他程序创建：{target}"
                    )
            else:
                if not target.exists():
                    raise RuntimeError(
                        f"目标文件在提取期间被其他程序删除：{target}"
                    )
                target_after = target.stat()
                if (
                    target_before.st_size,
                    target_before.st_mtime_ns,
                ) != (
                    target_after.st_size,
                    target_after.st_mtime_ns,
                ):
                    raise RuntimeError(
                        f"目标文件在提取期间发生变化：{target}"
                    )
            os.replace(temporary, target)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return target

    def build(
        self,
        archive: PacArchive,
        replacements: Mapping[str, str | Path],
        output_path: str | Path,
        *,
        do_backup: bool = True,
        additions: Mapping[str, str | Path] | None = None,
    ) -> PacBuildReport:
        replacement_paths = {
            name: Path(path).resolve()
            for name, path in replacements.items()
        }
        addition_paths = {name: Path(path).resolve() for name, path in (additions or {}).items()}
        names = {entry.name.casefold() for entry in archive.entries}
        for name in addition_paths:
            _validate_entry_name(name)
            if name.casefold() in names:
                raise ValueError(f"新增条目与已有路径冲突：{name}")
            names.add(name.casefold())
        if len(names) > MAX_ENTRIES:
            raise ValueError("PAC 条目数量超过支持的上限。")
        unknown = sorted(set(replacement_paths) - {entry.name for entry in archive.entries})
        if unknown:
            raise KeyError(f"Replacement entries are not present in the PAC: {unknown}")
        payload_paths = {**replacement_paths, **addition_paths}
        for name, path in payload_paths.items():
            if not path.is_file():
                raise FileNotFoundError(f"Replacement payload is missing for {name}: {path}")

        if not self.source_is_current(archive, deep=True):
            raise RuntimeError("源 PAC 已变化，已阻止回包以避免覆盖新版本。")

        target = Path(output_path).resolve()
        if target in payload_paths.values():
            raise ValueError("输出 PAC 不能覆盖导入资源文件。")
        target.parent.mkdir(parents=True, exist_ok=True)
        target_existed = target.exists()
        target_digest = _hash_file(target) if target_existed else None
        fd, staged_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".pac.tmp",
            dir=target.parent,
        )
        os.close(fd)
        staged = Path(staged_name)
        try:
            candidate = self._with_additions(archive, addition_paths)
            if payload_paths:
                self._write_rebuilt(candidate, payload_paths, staged)
            else:
                _copy_file_verified(archive.source_path, staged, archive.source_sha256)

            verified = self.inspect(staged)
            self._verify_output(candidate, verified, payload_paths)

            if not self.source_is_current(archive, deep=True):
                raise RuntimeError("源 PAC 在构建期间发生变化，已阻止输出。")
            if target_existed:
                if not target.exists() or _hash_file(target) != target_digest:
                    raise RuntimeError(f"目标 PAC 在构建期间发生变化：{target}")
                if do_backup:
                    backup = Path(f"{target}.bak")
                    if not backup.exists():
                        _copy_file_verified(target, backup, target_digest or "")
            elif target.exists():
                raise RuntimeError(f"目标 PAC 在构建期间被其他程序创建：{target}")
            os.replace(staged, target)
            target_stat = target.stat()
            verified.source_path = target
            verified.source_size = target_stat.st_size
            verified.source_mtime_ns = target_stat.st_mtime_ns
            return PacBuildReport(
                output_path=target,
                output_size=verified.source_size,
                output_sha256=verified.source_sha256,
                entry_count=len(verified.entries),
                changed_entries=tuple(sorted(payload_paths)),
                verified_archive=verified,
            )
        finally:
            staged.unlink(missing_ok=True)

    @staticmethod
    def _with_additions(archive: PacArchive, additions: Mapping[str, Path]) -> PacArchive:
        if not additions:
            return archive
        entries = list(archive.entries)
        for name, payload in additions.items():
            order = len(entries)
            entries.append(PacEntry(
                header_index=order, name_order=order, data_order=order,
                name=name, path_hash=zlib.crc32(name.encode("utf-8")) ^ 0xFFFFFFFF,
                reserved=0, name_offset=0, size=payload.stat().st_size,
                data_offset=0, sha256="",
            ))
        # The game looks up the hash-sorted directory. Payload/name order can
        # stay stable, preserving the workspace cache identities after insert.
        entries = [replace(entry, header_index=index) for index, entry in enumerate(
            sorted(entries, key=lambda entry: (entry.path_hash, entry.header_index))
        )]
        return replace(archive, entries=entries)

    def _write_rebuilt(
        self,
        archive: PacArchive,
        replacements: Mapping[str, Path],
        output: Path,
    ) -> None:
        entries_by_name_order = sorted(archive.entries, key=lambda entry: entry.name_order)
        entries_by_data_order = sorted(archive.entries, key=lambda entry: entry.data_order)
        names = bytearray()
        name_offsets: dict[str, int] = {}
        table_end = HEADER_STRUCT.size + len(archive.entries) * ENTRY_STRUCT.size
        for entry in entries_by_name_order:
            name_offsets[entry.name] = table_end + len(names)
            names.extend(entry.name.encode("utf-8"))
            names.append(0)
        header_size = table_end + len(names)
        if header_size > 0xFFFFFFFF:
            raise OverflowError("PAC header exceeds the 32-bit header-size field.")

        sizes = {
            entry.name: (
                replacements[entry.name].stat().st_size
                if entry.name in replacements
                else entry.size
            )
            for entry in archive.entries
        }
        data_offsets: dict[str, int] = {}
        cursor = header_size
        for entry in entries_by_data_order:
            data_offsets[entry.name] = cursor
            # Distinct offsets also give empty files an unambiguous data order.
            cursor += max(1, sizes[entry.name])

        with output.open("wb") as stream:
            stream.write(
                HEADER_STRUCT.pack(
                    MAGIC,
                    len(archive.entries),
                    header_size,
                    archive.format_version,
                )
            )
            for entry in sorted(archive.entries, key=lambda item: item.header_index):
                stream.write(
                    ENTRY_STRUCT.pack(
                        entry.path_hash,
                        entry.reserved,
                        name_offsets[entry.name],
                        sizes[entry.name],
                        data_offsets[entry.name],
                    )
                )
            stream.write(names)
            with archive.source_path.open("rb") as original:
                for entry in entries_by_data_order:
                    replacement = replacements.get(entry.name)
                    if replacement is not None:
                        with replacement.open("rb") as payload:
                            shutil.copyfileobj(payload, stream, COPY_CHUNK_SIZE)
                    else:
                        original.seek(entry.data_offset)
                        _copy_range(original, stream, entry.size)
                    if sizes[entry.name] == 0:
                        stream.write(b"\0")
            stream.flush()
            os.fsync(stream.fileno())

    def _verify_output(
        self,
        original: PacArchive,
        rebuilt: PacArchive,
        replacements: Mapping[str, Path],
    ) -> None:
        if rebuilt.format_version != original.format_version:
            raise RuntimeError("Rebuilt PAC changed the format-version field.")
        if len(rebuilt.entries) != len(original.entries):
            raise RuntimeError("Rebuilt PAC changed the entry count.")
        expected_by_name = {entry.name: entry for entry in original.entries}
        actual_by_name = {entry.name: entry for entry in rebuilt.entries}
        if actual_by_name.keys() != expected_by_name.keys():
            raise RuntimeError("Rebuilt PAC changed the entry-name set.")
        replacement_hashes = {
            name: _hash_file(path)
            for name, path in replacements.items()
        }
        for name, expected in expected_by_name.items():
            actual = actual_by_name[name]
            if (actual.path_hash, actual.reserved) != (expected.path_hash, expected.reserved):
                raise RuntimeError(f"Rebuilt PAC changed metadata for {name}.")
            if (
                actual.header_index,
                actual.name_order,
                actual.data_order,
            ) != (
                expected.header_index,
                expected.name_order,
                expected.data_order,
            ):
                raise RuntimeError(f"Rebuilt PAC changed entry ordering for {name}.")
            wanted_digest = replacement_hashes.get(name, expected.sha256)
            if actual.sha256 != wanted_digest:
                raise RuntimeError(f"Rebuilt PAC payload verification failed for {name}.")


def _read_exact(stream: BinaryIO, size: int, label: str) -> bytes:
    payload = stream.read(size)
    if len(payload) != size:
        raise FpacFormatError(f"Unexpected EOF while reading {label}.")
    return payload


def _read_name(stream: BinaryIO, offset: int, header_size: int) -> str:
    stream.seek(offset)
    limit = min(MAX_NAME_BYTES, header_size - offset)
    payload = bytearray()
    while len(payload) < limit:
        block = stream.read(min(256, limit - len(payload)))
        end = block.find(b"\x00")
        payload.extend(block if end < 0 else block[:end])
        if end >= 0:
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise FpacFormatError("PAC entry name is not valid UTF-8.") from exc
        if not block:
            break
    raise FpacFormatError("PAC entry name is not NUL-terminated inside the header.")


def _validate_entry_name(name: str) -> None:
    if not name:
        raise FpacFormatError("PAC entry name must not be empty.")
    if "\\" in name or "\x00" in name:
        raise FpacFormatError(f"Unsafe PAC entry name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
        raise FpacFormatError(f"Unsafe PAC entry path: {name!r}")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if any(any(char in '<>:"|?*' or ord(char) < 32 for char in part)
           or part.endswith((".", " ")) or part.split(".")[0].upper() in reserved
           for part in path.parts):
        raise FpacFormatError(f"Unsafe PAC entry path segment: {name!r}")
    if len(name.encode("utf-8")) >= MAX_NAME_BYTES:
        raise FpacFormatError("PAC entry name exceeds the supported length.")


def _validate_non_overlapping_payloads(entries: list[dict[str, object]]) -> None:
    previous_end = 0
    previous_name = ""
    for entry in sorted(entries, key=lambda item: int(item["data_offset"])):
        start = int(entry["data_offset"])
        end = start + int(entry["size"])
        if start < previous_end:
            raise FpacFormatError(
                f"PAC payloads overlap: {previous_name!r} and {entry['name']!r}"
            )
        previous_end = max(previous_end, end)
        previous_name = str(entry["name"])


def _hash_archive_ranges(
    stream: BinaryIO,
    entries: list[dict[str, object]],
    source_size: int,
) -> tuple[dict[int, str], str]:
    """Hash the source and every payload in one sequential pass."""

    source_digest = hashlib.sha256()
    entry_digests: dict[int, str] = {}
    cursor = 0
    stream.seek(0)

    for entry in sorted(entries, key=lambda item: int(item["data_offset"])):
        start = int(entry["data_offset"])
        gap = start - cursor
        while gap:
            chunk = stream.read(min(COPY_CHUNK_SIZE, gap))
            if not chunk:
                raise FpacFormatError("Unexpected EOF before a PAC payload.")
            source_digest.update(chunk)
            gap -= len(chunk)
            cursor += len(chunk)

        payload_digest = hashlib.sha256()
        remaining = int(entry["size"])
        while remaining:
            chunk = stream.read(min(COPY_CHUNK_SIZE, remaining))
            if not chunk:
                raise FpacFormatError("Unexpected EOF inside a PAC payload.")
            source_digest.update(chunk)
            payload_digest.update(chunk)
            remaining -= len(chunk)
            cursor += len(chunk)
        entry_digests[int(entry["header_index"])] = payload_digest.hexdigest()

    trailing = source_size - cursor
    while trailing:
        chunk = stream.read(min(COPY_CHUNK_SIZE, trailing))
        if not chunk:
            raise FpacFormatError("Unexpected EOF after the final PAC payload.")
        source_digest.update(chunk)
        trailing -= len(chunk)
        cursor += len(chunk)
    return entry_digests, source_digest.hexdigest()


def _copy_range(source: BinaryIO, output: BinaryIO, size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = source.read(min(COPY_CHUNK_SIZE, remaining))
        if not chunk:
            raise FpacFormatError("Unexpected EOF while copying a PAC payload.")
        output.write(chunk)
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(COPY_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file_verified(source: Path, target: Path, expected_digest: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_stream, os.fdopen(fd, "wb") as output_stream:
            digest = hashlib.sha256()
            while chunk := input_stream.read(COPY_CHUNK_SIZE):
                output_stream.write(chunk)
                digest.update(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if expected_digest and digest.hexdigest() != expected_digest:
            raise RuntimeError(f"File changed while it was being copied: {source}")
        os.replace(temporary, target)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
