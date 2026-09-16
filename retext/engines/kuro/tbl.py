from __future__ import annotations

import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ...domain import (
    CapabilityLevel,
    DocumentKind,
    EngineCapability,
    GameVersion,
    SavePlan,
    TextDocument,
    TextUnit,
)
from ...io_utils import atomic_write_bytes
from ...paths import KURO_ROOT
from ..base import EngineBase
from ..relocation import (
    StringReference,
    canonical_string_targets,
    discover_tbl_references,
    scan_printable_cstring_targets,
    splice_referenced_strings,
)
from .processcle import unwrapCLE, wrapCLE
from .support import import_kuro_module


@lru_cache(maxsize=512)
def _read_schema_json(path: str) -> dict[str, Any]:
    """Cache immutable bundled Schema documents across service instances."""

    return json.loads(Path(path).read_text(encoding="utf-8"))


@dataclass(slots=True)
class HeaderState:
    name: str
    length: int
    count: int
    start: int
    schema_game: str | None
    schema_content: dict[str, Any] | None
    schema_match: str | None
    data_rows: list[dict[str, Any]]
    text_storage: dict[tuple[int, tuple[str, ...]], tuple[int, str]]
    external_data_ranges: list[tuple[int, int]]
    external_offset_fields: dict[int, int]


@dataclass(slots=True)
class PoolTextState:
    offset: int
    encoding: str
    text: str
    byte_length: int


@dataclass(slots=True)
class KuroTblState:
    original_magic: bytes
    original_bytes: bytes
    filename_stem: str
    headers: list[HeaderState]
    fallback_pool_texts: list[PoolTextState]
    trailing_dump: str | None
    requested_game: str
    resolved_game: str
    game_detection: str
    layout_game: str | None
    original_payload: bytes = b""
    cle_layers: tuple[bytes, ...] = ()
    roundtrip_certified: bool = False

    @property
    def has_mixed_schema_coverage(self) -> bool:
        known = sum(header.schema_content is not None for header in self.headers)
        return 0 < known < len(self.headers)

    @property
    def has_compatible_schema(self) -> bool:
        return any(header.schema_match == "compatible" for header in self.headers)

    @property
    def has_fallback_texts(self) -> bool:
        return bool(self.fallback_pool_texts)


class KuroTblEngine(EngineBase):
    name = "kuro_tbl"

    def __init__(self) -> None:
        parser = import_kuro_module("lib.parser")
        self._process_data = parser.process_data
        self._readint = parser.readint
        self._get_size_from_schema = parser.get_size_from_schema
        self._get_datatype_size = parser.get_datatype_size

        packer = import_kuro_module("lib.packer")
        self._pack_data = packer.pack_data
        self._writehex = packer.writehex
        self._writeint = packer.writeint
        self._writetext = packer.writetext

        crc32 = import_kuro_module("lib.crc32")
        self._compute_crc32 = crc32.compute_crc32

        processcle = import_kuro_module("processcle")
        self._process_cle = processcle.processCLE

    def capabilities(self) -> list[EngineCapability]:
        return [
            EngineCapability(
                engine=self.name,
                kind=DocumentKind.TBL,
                operation="schema-parse-edit-pack",
                level=CapabilityLevel.STABLE,
                notes="基于 KuroTools schema 的 TBL 解析与封包，适合安全回环。",
            )
        ]

    def load(self, path: str | Path, **kwargs) -> TextDocument:
        source = Path(path).resolve()
        requested_game = GameVersion.normalize(kwargs.get("game", GameVersion.AUTO))
        schema_hint = kwargs.get("schema_hint", source.stem)
        state = self._parse_tbl(
            source,
            game=requested_game.value,
            schema_hint=schema_hint,
        )
        units = self._extract_units(state)
        exact_headers = sum(
            header.schema_match == "exact" for header in state.headers
        )
        compatible_headers = sum(
            header.schema_match == "compatible" for header in state.headers
        )
        known_headers = exact_headers + compatible_headers
        return TextDocument(
            source_path=source,
            kind=DocumentKind.TBL,
            engine=self.name,
            units=units,
            metadata={
                "game": requested_game.value,
                "resolved_game": state.resolved_game,
                "game_detection": state.game_detection,
                "layout_game": state.layout_game,
                "header_count": len(state.headers),
                "known_header_count": known_headers,
                "exact_header_count": exact_headers,
                "compatible_header_count": compatible_headers,
                "fallback_text_count": len(state.fallback_pool_texts),
                "roundtrip_certified": state.roundtrip_certified,
                "schema_hint": schema_hint,
            },
            state=state,
        )

    def preview_save(self, document: TextDocument, **kwargs) -> SavePlan:
        changed = len(document.changed_units())
        state: KuroTblState = document.state
        patchable = bool(
            changed
            and self._collect_in_place_patches(document) is not None
        )
        pool_repackable = bool(
            changed
            and not patchable
            and self._collect_pool_splice_changes(document) is not None
        )
        full_repackable = bool(
            changed
            and not patchable
            and not pool_repackable
            and state.roundtrip_certified
        )
        if changed == 0:
            mode = "copy"
        elif patchable:
            mode = "patch"
        elif pool_repackable:
            mode = "pool-repack"
        elif full_repackable:
            mode = "repack"
        else:
            mode = "blocked"
        safe = mode != "blocked"
        return SavePlan(
            engine=self.name,
            mode=mode,
            safe=safe,
            requires_rebuild=changed > 0 and mode in {"repack", "pool-repack"},
            notes=[
                f"KuroTools TBL backend changed entries: {changed}.",
                *(
                    ["等字节长度文本将原位写回；未知 Header 与其余字节保持不变。"]
                    if mode == "patch"
                    else []
                ),
                *(
                    ["兼容 Schema 已通过该文件的无修改字节级回环认证，可安全重打包。"]
                    if mode == "repack"
                    else []
                ),
                *(
                    [
                        "布局保持型字符串池重建：保留 Header、固定记录与未知数据，"
                        "仅伸缩文本并重定位 Schema/记录列确认的 64 位外部偏移。"
                    ]
                    if mode == "pool-repack"
                    else []
                ),
                *(
                    ["未能为修改文本建立完整的字符串存储与外部引用关系。"]
                    if mode == "blocked"
                    else []
                ),
                *(
                    [f"保存后将恢复 {len(state.cle_layers)} 层 CLE 封装。"]
                    if changed and state.cle_layers
                    else []
                ),
            ],
        )

    def save(
        self,
        document: TextDocument,
        *,
        output_path: str | Path | None = None,
        **kwargs,
    ) -> Path:
        target = Path(output_path).resolve() if output_path else document.source_path
        state: KuroTblState = document.state
        changed = len(document.changed_units())
        if changed == 0:
            payload = state.original_bytes
        else:
            plan = self.preview_save(document)
            if not plan.safe:
                raise ValueError("TBL rebuild is unavailable: " + " ".join(plan.notes[1:]))
            if plan.mode == "patch":
                patches = self._collect_in_place_patches(document)
                if patches is None:
                    raise ValueError(
                        "Refusing an unsafe TBL patch: text storage changed after preview."
                    )
                patched = bytearray(state.original_payload)
                for offset, replacement in patches.items():
                    patched[offset : offset + len(replacement)] = replacement
                rebuilt_payload = bytes(patched)
                self._apply_document_changes(document)
            elif plan.mode == "pool-repack":
                rebuilt_payload = self._build_pool_splice_payload(document)
                self._apply_document_changes(document)
            else:
                self._apply_document_changes(document)
                rebuilt_payload = self._build_tbl_bytes(state)
            payload = wrapCLE(rebuilt_payload, state.cle_layers)
            self._verify_payload(document, payload, target.parent)
        expected = state.original_bytes if target == document.source_path.resolve() else None
        atomic_write_bytes(
            target,
            payload,
            do_backup=kwargs.get("do_backup", False),
            expected_bytes=expected,
        )
        refreshed = self.load(
            target,
            game=str(document.metadata.get("resolved_game", GameVersion.SORA1.value)),
            schema_hint=str(document.metadata.get("schema_hint", target.stem)),
        )
        if [unit.current_text for unit in refreshed.units] != [
            unit.current_text for unit in document.units
        ]:
            raise RuntimeError("Saved TBL failed the final text verification.")
        document.source_path = refreshed.source_path
        document.units = refreshed.units
        document.metadata = refreshed.metadata
        document.state = refreshed.state
        document.rebuild_index()
        return target

    def _verify_payload(self, document: TextDocument, payload: bytes, target_dir: Path) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=".kuro_tbl_verify.", suffix=".tbl", dir=target_dir)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            atomic_write_bytes(temporary, payload)
            verified_state = self._parse_tbl(
                temporary,
                game=str(document.metadata.get("resolved_game", GameVersion.SORA1.value)),
                schema_hint=str(document.metadata.get("schema_hint", document.source_path.stem)),
            )
            actual_texts = [unit.current_text for unit in self._extract_units(verified_state)]
            expected_texts = [unit.current_text for unit in document.units]
            if actual_texts != expected_texts:
                raise RuntimeError("Kuro TBL staged output failed the full text roundtrip verification.")
        finally:
            temporary.unlink(missing_ok=True)

    def _parse_tbl(self, path: Path, *, game: str, schema_hint: str) -> KuroTblState:
        raw = path.read_bytes()
        if len(raw) < 8:
            raise ValueError(f"{path.name} is too small to be a TBL payload.")
        outer_magic = raw[:4]
        data, cle_layers = unwrapCLE(raw)
        stream = io.BytesIO(data)
        if stream.read(4) != b"#TBL":
            raise ValueError(f"{path.name} is not a valid TBL payload.")

        header_count = self._readint(stream, 4)
        if header_count > max(0, (len(data) - 8) // 0x50):
            raise ValueError(f"{path.name} declares an invalid TBL header count: {header_count}.")
        headers_meta: list[dict[str, Any]] = []
        for _ in range(header_count):
            header_name_bytes = stream.read(64)
            if len(header_name_bytes) != 64:
                raise EOFError("Unexpected end of file while reading a TBL header name.")
            header_name = header_name_bytes.split(b"\0", 1)[0].decode("utf-8")
            stream.read(4)
            start = self._readint(stream, 4)
            length = self._readint(stream, 4)
            count = self._readint(stream, 4)
            end = start + length * count
            if start > len(data) or end > len(data):
                raise ValueError(f"TBL header {header_name!r} points outside the payload.")
            headers_meta.append(
                {
                    "name": header_name,
                    "start": start,
                    "length": length,
                    "count": count,
                }
            )

        requested_game = GameVersion.normalize(game)
        layout_game, layout_detection = self._detect_game(
            file_stem=schema_hint,
            headers=headers_meta,
        )
        resolved_game, game_detection = self._select_game_for_layout(
            requested_game,
            layout_game,
            layout_detection,
        )

        for header in headers_meta:
            schema_game, schema_content, schema_match = self._resolve_schema(
                file_stem=schema_hint,
                header_name=header["name"],
                entry_length=header["length"],
                game=resolved_game,
            )
            header["schema_game"] = schema_game
            header["schema_content"] = schema_content
            header["schema_match"] = schema_match
        known_header_count = sum(
            header["schema_content"] is not None for header in headers_meta
        )
        record_text_storage = known_header_count > 0

        states: list[HeaderState] = []
        for header in headers_meta:
            schema_game = header["schema_game"]
            schema_content = header["schema_content"]
            schema_match = header["schema_match"]
            stream.seek(header["start"])
            rows: list[dict[str, Any]] = []
            text_storage: dict[tuple[int, tuple[str, ...]], tuple[int, str]] = {}
            external_data_ranges: set[tuple[int, int]] = set()
            external_offset_fields: dict[int, int] = {}
            if schema_content is None:
                for _ in range(header["count"]):
                    rows.append({"data": stream.read(header["length"]).hex(" ").upper()})
            else:
                schema = schema_content["schema"]
                try:
                    for row_index in range(header["count"]):
                        row_start = header["start"] + row_index * header["length"]
                        processed = 0
                        row: dict[str, Any] = {}
                        for key, datatype in schema.items():
                            effective_datatype = datatype
                            if isinstance(datatype, str) and datatype.startswith("comp:"):
                                effective_datatype = schema[datatype[5:]]
                            value, consumed = self._process_data(
                                stream,
                                effective_datatype,
                                header["length"] - processed,
                            )
                            row[key] = value
                            processed += consumed
                        rows.append(row)
                        if record_text_storage:
                            self._collect_row_text_storage(
                                data,
                                schema,
                                row_start,
                                row_index,
                                text_storage,
                                external_data_ranges,
                                external_offset_fields,
                            )
                except (EOFError, LookupError, UnicodeError, ValueError):
                    if schema_match != "compatible":
                        raise
                    schema_game = None
                    schema_content = None
                    schema_match = None
                    rows = []
                    text_storage = {}
                    external_data_ranges = set()
                    external_offset_fields = {}
                    stream.seek(header["start"])
                    for _ in range(header["count"]):
                        rows.append(
                            {"data": stream.read(header["length"]).hex(" ").upper()}
                        )

            states.append(
                HeaderState(
                    name=header["name"],
                    length=header["length"],
                    count=header["count"],
                    start=header["start"],
                    schema_game=schema_game,
                    schema_content=schema_content,
                    schema_match=schema_match,
                    data_rows=rows,
                    text_storage=text_storage,
                    external_data_ranges=sorted(external_data_ranges),
                    external_offset_fields=external_offset_fields,
                )
            )

        known_fields = {
            position: target
            for header in states
            for position, target in header.external_offset_fields.items()
        }
        reference_layout = discover_tbl_references(
            data,
            known_fields=known_fields,
            infer_record_ranges=[
                (
                    header.start,
                    header.start + header.length * header.count,
                )
                for header in states
                if header.schema_content is None
            ],
        )
        for header in states:
            record_end = header.start + header.length * header.count
            header.external_offset_fields.update(
                {
                    position: target
                    for position, target in reference_layout.offset_fields.items()
                    if header.start <= position < record_end
                }
            )

        fallback_pool_texts = self._extract_fallback_pool_texts(
            data,
            states,
            inferred_text_targets=set(reference_layout.inferred_text_fields.values()),
        )

        trailing_dump = None
        if headers_meta and all(header.schema_content is None for header in states):
            trailing_start = max(
                header["start"] + header["length"] * header["count"]
                for header in headers_meta
            )
            if trailing_start < len(data):
                trailing_dump = data[trailing_start:].hex(" ").upper()

        state = KuroTblState(
            original_magic=outer_magic,
            original_bytes=raw,
            original_payload=data,
            cle_layers=cle_layers,
            filename_stem=path.stem,
            headers=states,
            fallback_pool_texts=fallback_pool_texts,
            trailing_dump=trailing_dump,
            requested_game=requested_game.value,
            resolved_game=resolved_game,
            game_detection=game_detection,
            layout_game=layout_game,
        )
        if (
            states
            and all(header.schema_content is not None for header in states)
            and not fallback_pool_texts
        ):
            try:
                state.roundtrip_certified = self._build_tbl_bytes(state) == data
            except (KeyError, LookupError, TypeError, ValueError, UnicodeError):
                state.roundtrip_certified = False
        return state

    def _detect_game(
        self,
        *,
        file_stem: str,
        headers: list[dict[str, Any]],
    ) -> tuple[str | None, str]:
        """Infer 1st/2nd from concrete header layouts, never from the PAC.

        Exact Schema matches are weighted by the amount of record data they
        explain.  A small secondary header therefore cannot outvote the main
        table layout.  Ambiguous/unknown files retain the established Sora1
        compatibility default and are reported as such to the UI.
        """

        scores: dict[str, int] = {}
        for candidate in (GameVersion.SORA1.value, GameVersion.SORA2.value):
            score = 0
            for header in headers:
                _schema_game, _schema, match = self._resolve_schema(
                    file_stem=file_stem,
                    header_name=str(header["name"]),
                    entry_length=int(header["length"]),
                    game=candidate,
                )
                if match == "exact":
                    score += max(1, int(header["length"]) * int(header["count"]))
            scores[candidate] = score

        highest = max(scores.values(), default=0)
        winners = [game_name for game_name, score in scores.items() if score == highest]
        if highest > 0 and len(winners) == 1:
            return winners[0], "layout"
        return None, "fallback"

    @staticmethod
    def _select_game_for_layout(
        requested_game: GameVersion,
        layout_game: str | None,
        layout_detection: str,
    ) -> tuple[str, str]:
        """Prefer a proven per-file layout over the package-level preference.

        Sora2 PACs legitimately bundle Sora1/FC-layout resources such as
        ``t_quest_fc.tbl`` and ``t_books.tbl``. Treating a manual Sora2
        selection as a hard schema boundary hides valid fields in those files.
        The selected game therefore remains a fallback only when the concrete
        record layout is ambiguous or unknown.
        """

        if layout_game is not None:
            if requested_game is GameVersion.AUTO:
                return layout_game, layout_detection
            if requested_game.value == layout_game:
                return layout_game, "manual-confirmed-by-layout"
            return layout_game, f"{layout_detection}-override-{requested_game.value}"
        if requested_game is GameVersion.AUTO:
            return GameVersion.SORA1.value, layout_detection
        return requested_game.value, "manual-fallback"

    def _resolve_schema(
        self,
        *,
        file_stem: str,
        header_name: str,
        entry_length: int,
        game: str,
    ) -> tuple[str | None, dict[str, Any] | None, str | None]:
        schema_root = KURO_ROOT / "schemas"
        filename = re.sub(r"\d", "%d", file_stem)
        meta_path = schema_root / f"{filename}.json"
        header_path = schema_root / "headers" / f"{header_name}.json"
        if not header_path.exists():
            return None, None, None

        allow_header = True
        if meta_path.exists():
            meta = _read_schema_json(str(meta_path))
            allow_header = header_name in meta.get("headers", [])

        schemas = _read_schema_json(str(header_path))
        if allow_header:
            compatible: list[tuple[str, dict[str, Any]]] = []
            for schema_name, schema in schemas.items():
                if self._get_size_from_schema(schema) != entry_length:
                    continue
                if schema.get("game") == game:
                    return schema.get("game", schema_name), schema, "exact"
                compatible.append((schema_name, schema))
            if compatible:
                layouts = {
                    json.dumps(schema.get("schema", {}), sort_keys=True)
                    for _schema_name, schema in compatible
                }
                if len(compatible) == 1 or len(layouts) == 1:
                    schema_name, schema = compatible[0]
                    return schema.get("game", schema_name), schema, "compatible"
        return None, None, None

    @staticmethod
    def _extract_fallback_pool_texts(
        payload: bytes,
        headers: list[HeaderState],
        *,
        inferred_text_targets: set[int] | None = None,
    ) -> list[PoolTextState]:
        """Supplement text omitted by schemas from the validated tail pool.

        KuroTools schemas remain authoritative.  Entries already referenced by
        a structured field are excluded by offset; only otherwise invisible
        pool entries are exposed.  They can therefore be previewed and safely
        patched in place without pretending that their row layout is known.
        """

        if not headers:
            return []
        records_end = max(
            (header.start + header.length * header.count for header in headers),
            default=len(payload),
        )
        if records_end >= len(payload):
            return []
        structured_offsets = {
            offset
            for header in headers
            for offset, _encoding in header.text_storage.values()
        }
        external_ranges = [
            bounds
            for header in headers
            for bounds in header.external_data_ranges
        ]
        output_by_offset: dict[int, PoolTextState] = {}
        physical_targets = scan_printable_cstring_targets(
            payload,
            records_end,
            excluded_ranges=external_ranges,
        )
        canonical_targets = canonical_string_targets(
            payload,
            {*physical_targets, *(inferred_text_targets or set())},
        )
        for offset in canonical_targets:
            if offset in structured_offsets or any(
                start <= offset < end for start, end in external_ranges
            ):
                continue
            end = payload.find(b"\0", offset)
            if end < offset:
                continue
            raw = payload[offset:end]
            if not raw:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            output_by_offset[offset] = PoolTextState(
                offset=offset,
                encoding="utf-8",
                text=text,
                byte_length=len(raw),
            )
        return [output_by_offset[offset] for offset in sorted(output_by_offset)]

    def _extract_units(self, state: KuroTblState) -> list[TextUnit]:
        units: list[TextUnit] = []
        index = 0
        for header_index, header in enumerate(state.headers):
            if header.schema_content is None:
                continue
            schema = header.schema_content["schema"]
            for row_index, row in enumerate(header.data_rows):
                for field_path, value in self._iter_text_fields(schema, row):
                    storage = header.text_storage.get((row_index, tuple(field_path)))
                    metadata: dict[str, Any] = {
                        "header_index": header_index,
                        "row_index": row_index,
                        "field_path": field_path,
                        "schema_match": header.schema_match,
                    }
                    if storage is not None:
                        offset, encoding = storage
                        metadata.update(
                            {
                                "text_offset": offset,
                                "text_encoding": encoding,
                                "text_byte_length": len(value.encode(encoding)),
                            }
                        )
                    units.append(
                        TextUnit(
                            index=index,
                            original_text=value,
                            current_text=value,
                            location=f"{header.name}[{row_index}].{'.'.join(field_path)}",
                            context=header.name,
                            metadata=metadata,
                        )
                    )
                    index += 1
        for pool_text in state.fallback_pool_texts:
            units.append(
                TextUnit(
                    index=index,
                    original_text=pool_text.text,
                    current_text=pool_text.text,
                    location=f"字符串池[0x{pool_text.offset:08X}]",
                    context="Schema 结构外的字符串池补充文本",
                    metadata={
                        "fallback_pool": True,
                        "text_offset": pool_text.offset,
                        "text_encoding": pool_text.encoding,
                        "text_byte_length": pool_text.byte_length,
                    },
                )
            )
            index += 1
        return units

    def _iter_text_fields(
        self,
        schema: dict[str, Any],
        row: dict[str, Any],
        prefix: list[str] | None = None,
    ):
        prefix = prefix or []
        for key, datatype in schema.items():
            value = row[key]
            if isinstance(datatype, dict):
                for index, item in enumerate(value):
                    yield from self._iter_text_fields(
                        datatype["schema"],
                        item,
                        prefix + [f"{key}[{index}]"],
                    )
                continue
            if isinstance(datatype, str) and datatype.startswith("comp:"):
                comp_schema = schema[datatype[5:]]
                if isinstance(comp_schema, dict):
                    for index, item in enumerate(value):
                        yield from self._iter_text_fields(
                            comp_schema["schema"],
                            item,
                            prefix + [f"{key}[{index}]"],
                        )
                continue
            if isinstance(datatype, str) and datatype.startswith("toffset"):
                yield prefix + [key], value

    def _collect_row_text_storage(
        self,
        payload: bytes,
        schema: dict[str, Any],
        row_start: int,
        row_index: int,
        output: dict[tuple[int, tuple[str, ...]], tuple[int, str]],
        external_data_ranges: set[tuple[int, int]] | None = None,
        external_offset_fields: dict[int, int] | None = None,
    ) -> None:
        """Record exact string offsets without changing KuroTools parsing.

        TBL rows only contain 64-bit pointers for ``toffset`` fields.  Keeping
        these offsets lets an equal-byte-length edit preserve unknown headers
        and every other byte in a mixed-schema table.
        """

        def schema_size(value: dict[str, Any]) -> int:
            total = 0
            for nested_datatype in value.values():
                nested_effective = nested_datatype
                if (
                    isinstance(nested_datatype, str)
                    and nested_datatype.startswith("comp:")
                ):
                    nested_effective = value[nested_datatype[5:]]
                if isinstance(nested_effective, dict):
                    total += int(nested_effective["size"]) * schema_size(
                        nested_effective["schema"]
                    )
                else:
                    total += self._get_datatype_size(nested_effective)
            return total

        def walk_schema(
            current_schema: dict[str, Any],
            base: int,
            prefix: list[str],
        ) -> int:
            cursor = base
            for key, datatype in current_schema.items():
                effective = datatype
                if isinstance(datatype, str) and datatype.startswith("comp:"):
                    effective = current_schema[datatype[5:]]
                if isinstance(effective, dict):
                    item_size = schema_size(effective["schema"])
                    for item_index in range(int(effective["size"])):
                        walk_schema(
                            effective["schema"],
                            cursor + item_index * item_size,
                            prefix + [f"{key}[{item_index}]"],
                        )
                    cursor += int(effective["size"]) * item_size
                    continue
                if isinstance(effective, str) and effective.startswith("toffset"):
                    if cursor < 0 or cursor + 8 > len(payload):
                        raise ValueError(
                            "TBL text pointer field falls outside the payload."
                        )
                    text_offset = int.from_bytes(payload[cursor : cursor + 8], "little")
                    if text_offset < 0 or text_offset >= len(payload):
                        raise ValueError("TBL text pointer falls outside the payload.")
                    encoding = "utf-8" if effective == "toffset" else effective[7:]
                    output[(row_index, tuple(prefix + [key]))] = (text_offset, encoding)
                    if external_offset_fields is not None:
                        external_offset_fields[cursor] = text_offset
                elif isinstance(effective, str) and effective == "offset":
                    if cursor < 0 or cursor + 8 > len(payload):
                        raise ValueError(
                            "TBL external pointer field falls outside the payload."
                        )
                    external_offset = int.from_bytes(
                        payload[cursor : cursor + 8],
                        "little",
                    )
                    if not 0 <= external_offset <= len(payload):
                        raise ValueError(
                            "TBL external pointer target falls outside the payload."
                        )
                    if external_offset_fields is not None:
                        external_offset_fields[cursor] = external_offset
                elif (
                    external_data_ranges is not None
                    and isinstance(effective, str)
                    and effective.startswith("u")
                    and effective.endswith("array")
                ):
                    if cursor < 0 or cursor + 12 > len(payload):
                        raise ValueError(
                            "TBL external array field falls outside the payload."
                        )
                    array_offset = int.from_bytes(
                        payload[cursor : cursor + 8],
                        "little",
                    )
                    array_count = int.from_bytes(
                        payload[cursor + 8 : cursor + 12],
                        "little",
                    )
                    item_size = int(effective[1:-5]) // 8
                    array_end = array_offset + array_count * item_size
                    if array_count and 0 <= array_offset <= array_end <= len(payload):
                        external_data_ranges.add((array_offset, array_end))
                    if (
                        external_offset_fields is not None
                        and 0 <= array_offset < len(payload)
                    ):
                        external_offset_fields[cursor] = array_offset
                cursor += self._get_datatype_size(effective)
            return cursor

        walk_schema(schema, row_start, [])

    def _collect_in_place_patches(
        self,
        document: TextDocument,
    ) -> dict[int, bytes] | None:
        state: KuroTblState = document.state
        original_payload = state.original_payload or state.original_bytes
        if original_payload[:4] != b"#TBL":
            return None
        patches: dict[int, bytes] = {}
        for unit in document.changed_units():
            try:
                offset = int(unit.metadata["text_offset"])
                encoding = str(unit.metadata["text_encoding"])
                expected_length = int(unit.metadata["text_byte_length"])
                original = unit.original_text.encode(encoding)
                replacement = unit.current_text.encode(encoding)
            except (KeyError, LookupError, TypeError, ValueError, UnicodeError):
                return None
            if len(original) != expected_length or len(replacement) != expected_length:
                return None
            if offset < 0 or offset + expected_length > len(original_payload):
                return None
            if original_payload[offset : offset + expected_length] != original:
                return None
            existing = patches.get(offset)
            if existing is not None and existing != replacement:
                return None
            patches[offset] = replacement
        if patches:
            for unit in document.units:
                try:
                    offset = int(unit.metadata["text_offset"])
                    encoding = str(unit.metadata["text_encoding"])
                    current = unit.current_text.encode(encoding)
                except (KeyError, LookupError, TypeError, ValueError, UnicodeError):
                    continue
                if offset in patches and patches[offset] != current:
                    return None
        return patches

    def _collect_pool_splice_changes(
        self,
        document: TextDocument,
    ) -> list[tuple[int, bytes, bytes]] | None:
        """Collect variable-length edits that have exact original storage.

        Both Schema-backed and fallback pool TextUnits carry the original
        absolute string offset.  Requiring that storage to match the retained
        byte snapshot prevents a stale or guessed offset from entering the
        layout-preserving pool-splice writer.
        """

        state: KuroTblState = document.state
        original_payload = state.original_payload or state.original_bytes
        if (
            original_payload[:4] != b"#TBL"
            or not state.headers
        ):
            return None
        records_end = max(
            header.start + header.length * header.count
            for header in state.headers
        )
        by_offset: dict[int, tuple[bytes, bytes]] = {}
        known_external_targets = {
            target
            for header in state.headers
            for target in header.external_offset_fields.values()
        }
        for unit in document.changed_units():
            try:
                offset = int(unit.metadata["text_offset"])
                encoding = str(unit.metadata["text_encoding"])
                expected_length = int(unit.metadata["text_byte_length"])
                original = unit.original_text.encode(encoding)
                replacement = unit.current_text.encode(encoding)
            except (KeyError, LookupError, TypeError, ValueError, UnicodeError):
                return None
            if (
                len(original) != expected_length
                or offset < records_end
                or offset + expected_length >= len(original_payload)
                or original_payload[offset:offset + expected_length] != original
                or original_payload[offset + expected_length] != 0
            ):
                return None
            # Referenced strings are preferred, but an unreferenced fallback
            # pool entry can still be resized: every later external offset is
            # relocated from the complete record-column map.
            if offset not in known_external_targets and not unit.metadata.get(
                "fallback_pool"
            ):
                return None
            existing = by_offset.get(offset)
            candidate = (original, replacement)
            if existing is not None and existing != candidate:
                return None
            by_offset[offset] = candidate

        changes = [
            (offset, original, replacement)
            for offset, (original, replacement) in sorted(by_offset.items())
        ]
        previous_end = records_end
        for offset, original, _replacement in changes:
            if offset < previous_end:
                return None
            previous_end = offset + len(original) + 1
        replacements = {
            offset: replacement
            for offset, _original, replacement in changes
        }
        for unit in document.units:
            try:
                offset = int(unit.metadata["text_offset"])
                expected = replacements[offset]
                encoding = str(unit.metadata["text_encoding"])
                current = unit.current_text.encode(encoding)
            except KeyError:
                continue
            except (LookupError, TypeError, ValueError, UnicodeError):
                return None
            # Multiple fields may deliberately share one string-pool entry.
            # Such aliases cannot be edited independently because one splice
            # changes every pointer to that storage.
            if current != expected:
                return None
        return changes or None

    def _build_pool_splice_payload(self, document: TextDocument) -> bytes:
        """Splice selected strings while preserving every unrelated byte.

        Falcom TBL records use 64-bit absolute offsets for text and external
        arrays.  Any such offset after a resized string must move by the same
        delta.  Schema fields and row-column-consistent inferred fields are
        concrete byte positions; unrelated numeric values are never searched
        and replaced by value.
        """

        state: KuroTblState = document.state
        changes = self._collect_pool_splice_changes(document)
        if changes is None:
            raise ValueError(
                "TBL pool rebuild requires exact, non-overlapping string storage."
            )
        records_end = max(
            header.start + header.length * header.count
            for header in state.headers
        )
        original = state.original_payload or state.original_bytes
        pointer_fields = sorted(
            (position, target)
            for header in state.headers
            for position, target in header.external_offset_fields.items()
        )
        for position, target in pointer_fields:
            if not 0 <= position <= records_end - 8:
                raise RuntimeError(
                    "TBL pool rebuild found an external-offset field outside records."
                )
            actual = int.from_bytes(original[position : position + 8], "little")
            if actual != target:
                raise RuntimeError(
                    "TBL pool rebuild found stale external-offset metadata."
                )

        references = [
            StringReference(position, target, 8, False, "tbl-external")
            for position, target in pointer_fields
        ]
        spliced, _relocated = splice_referenced_strings(
            original,
            changes,
            references,
            immutable_prefix_end=records_end,
        )
        return spliced

    def _apply_document_changes(self, document: TextDocument) -> None:
        state: KuroTblState = document.state
        for unit in document.units:
            if unit.metadata.get("fallback_pool"):
                continue
            header = state.headers[unit.metadata["header_index"]]
            row = header.data_rows[unit.metadata["row_index"]]
            self._set_nested_value(row, unit.metadata["field_path"], unit.current_text)

    def _set_nested_value(self, data: dict[str, Any], field_path: list[str], value: str) -> None:
        current: Any = data
        for part in field_path[:-1]:
            current = self._walk_part(current, part)
        last = field_path[-1]
        if "[" in last:
            parent, index = self._split_index(last)
            current[parent][index] = value
        else:
            current[last] = value

    def _walk_part(self, current: Any, part: str) -> Any:
        if "[" not in part:
            return current[part]
        name, index = self._split_index(part)
        return current[name][index]

    @staticmethod
    def _split_index(value: str) -> tuple[str, int]:
        name, suffix = value.split("[", 1)
        return name, int(suffix[:-1])

    def _build_tbl_bytes(self, state: KuroTblState) -> bytes:
        headers = [self._prepare_header(header) for header in state.headers]
        current_addr = 8 + len(headers) * 0x50
        for header in headers:
            header["start"] = current_addr
            current_addr += header["length"] * header["count"]

        stream = io.BytesIO()
        stream.write(b"#TBL")
        self._writeint(stream, len(headers), 4)
        for header in headers:
            self._writetext(stream, header["name"], padding=64)
            self._writeint(stream, self._compute_crc32(header["name"]), 4)
            self._writeint(stream, header["start"], 4)
            self._writeint(stream, header["length"], 4)
            self._writeint(stream, header["count"], 4)

        extra_data_idx = current_addr
        for header in headers:
            for row in header["rows"]:
                for key, datatype in header["schema"].items():
                    extra_data_idx = self._pack_data(stream, datatype, row[key], extra_data_idx)

        if state.trailing_dump:
            self._writehex(stream, state.trailing_dump)
        return stream.getvalue()

    def _prepare_header(self, header: HeaderState) -> dict[str, Any]:
        if header.schema_content is None:
            length = 0
            if header.data_rows:
                length = len(bytes.fromhex(header.data_rows[0]["data"]))
            return {
                "name": header.name,
                "count": len(header.data_rows),
                "length": length,
                "schema": {"data": "data"},
                "rows": header.data_rows,
            }
        return {
            "name": header.name,
            "count": len(header.data_rows),
            "length": header.length,
            "schema": header.schema_content["schema"],
            "rows": header.data_rows,
        }
