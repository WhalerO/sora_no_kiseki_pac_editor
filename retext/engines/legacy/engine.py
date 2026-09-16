from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ...domain import CapabilityLevel, DocumentKind, EngineCapability, SavePlan, TextDocument, TextUnit
from ...io_utils import atomic_write_bytes
from ..base import EngineBase
from ..kuro.processcle import unwrapCLE, wrapCLE
from . import core_engine


@dataclass(slots=True)
class LegacyDocumentState:
    kind: str
    data: bytes
    cluster: object | None
    entries: list[object]
    source_bytes: bytes = b""
    cle_layers: tuple[bytes, ...] = ()
    tbl_offset_fields: dict[int, int] = field(default_factory=dict)
    tbl_infer_ranges: list[tuple[int, int]] = field(default_factory=list)
    schema_hint: str = ""
    resolved_game: str = ""


def _legacy_modules():
    """Return the bundled engine through its package namespace.

    Older revisions temporarily inserted the vendor directory into ``sys.path``
    and imported a top-level module named ``core_engine``.  That was vulnerable
    to unrelated modules with the same name already being present in
    ``sys.modules`` and was not thread-safe.
    """

    return core_engine


class LegacyEngine(EngineBase):
    name = "legacy"

    def capabilities(self) -> list[EngineCapability]:
        return [
            EngineCapability(
                engine=self.name,
                kind=DocumentKind.TBL,
                operation="load-edit-save",
                level=CapabilityLevel.STABLE,
                notes="基于旧版字符串池/指针重写逻辑，适合快速敏捷处理。",
            ),
            EngineCapability(
                engine=self.name,
                kind=DocumentKind.DAT,
                operation="load-edit-save",
                level=CapabilityLevel.STABLE,
                notes="基于旧版 DAT 内嵌字符串与尾池识别逻辑。",
            ),
        ]

    def load(self, path: str | Path, **kwargs) -> TextDocument:
        core = _legacy_modules()
        source = Path(path).resolve()
        source_bytes = source.read_bytes()
        data, cle_layers = unwrapCLE(source_bytes)
        kind = str(kwargs.get("kind") or core.decide_kind_by_ext(str(source))).upper()
        tbl_text_targets: list[int] = []
        tbl_offset_fields: dict[int, int] = {}
        tbl_infer_ranges: list[tuple[int, int]] = []
        schema_hint = str(kwargs.get("schema_hint", source.stem))
        resolved_game = str(kwargs.get("game", "auto"))
        if kind == "TBL" and data.startswith(b"#TBL"):
            # Legacy keeps its own slot/repack writer, but consumes the same
            # complete schema-assisted reference inventory as Kuro.  Without
            # this bridge, unaligned fields and punctuation-only columns were
            # invisible in Legacy even though their on-disk pointers were exact.
            try:
                from ..kuro.tbl import KuroTblEngine

                structured = KuroTblEngine().load(
                    source,
                    game=resolved_game,
                    schema_hint=schema_hint,
                )
                structured_state = structured.state
                tbl_offset_fields = {
                    position: target
                    for header in structured_state.headers
                    for position, target in header.external_offset_fields.items()
                }
                tbl_infer_ranges = [
                    (
                        header.start,
                        header.start + header.length * header.count,
                    )
                    for header in structured_state.headers
                    if header.schema_content is None
                ]
                tbl_text_targets = sorted(
                    {
                        int(unit.metadata["text_offset"])
                        for unit in structured.units
                        if unit.current_text and "text_offset" in unit.metadata
                    }
                )
                resolved_game = str(
                    structured.metadata.get("resolved_game", resolved_game)
                )
            except (EOFError, LookupError, TypeError, UnicodeError, ValueError):
                # The generic reference-column parser remains available for
                # valid #TBL layouts not yet represented by a bundled schema.
                tbl_text_targets = []
                tbl_offset_fields = {}
                tbl_infer_ranges = []
        data, cluster, entries, kind = core.load_and_detect_bytes(
            data,
            kind,
            tbl_text_targets=tbl_text_targets,
            tbl_offset_fields=tbl_offset_fields,
            tbl_infer_ranges=tbl_infer_ranges,
        )
        base = cluster.base if cluster else 0
        dat_layout = getattr(cluster, "dat_layout", None) if cluster else None
        invalid_pointer_count = (
            len(dat_layout.invalid_pointer_fields)
            if dat_layout is not None
            else 0
        )
        units: list[TextUnit] = []
        for entry in entries:
            units.append(
                TextUnit(
                    index=entry.index,
                    original_text=entry.old_text,
                    current_text=entry.new_text,
                    location=f"0x{base + entry.offset:08X}",
                    context=f"{kind} legacy entry",
                    metadata={"offset": entry.offset, "byte_length": entry.old_len},
                )
            )
        return TextDocument(
            source_path=source,
            kind=DocumentKind(kind.lower()),
            engine=self.name,
            units=units,
            metadata={
                "cluster_base": base,
                "entry_count": len(entries),
                "cle_layers": len(cle_layers),
                "schema_hint": schema_hint,
                "resolved_game": resolved_game,
                "invalid_pointer_count": invalid_pointer_count,
                "damaged_reference_layout": bool(invalid_pointer_count),
                "full_pool_scan": dat_layout is not None,
                "unreferenced_text_count": (
                    sum(not item.references for item in dat_layout.strings.values())
                    if dat_layout is not None
                    else 0
                ),
            },
            state=LegacyDocumentState(
                kind=kind,
                data=data,
                cluster=cluster,
                entries=entries,
                source_bytes=source_bytes,
                cle_layers=cle_layers,
                tbl_offset_fields=tbl_offset_fields,
                tbl_infer_ranges=tbl_infer_ranges,
                schema_hint=schema_hint,
                resolved_game=resolved_game,
            ),
        )

    def preview_save(self, document: TextDocument, **kwargs) -> SavePlan:
        state: LegacyDocumentState = document.state
        if state.cluster is None:
            return SavePlan(
                engine=self.name,
                mode="noop",
                safe=False,
                requires_rebuild=False,
                notes=["文件未识别出可编辑字符串池。"],
            )

        core = _legacy_modules()
        self._apply_document_changes(document)
        requested_mode = kwargs.get("mode", "auto")
        if requested_mode == "auto":
            slot_report = core.precheck(state.kind, state.data, state.cluster, state.entries, "slot")
            if slot_report.slot_safe:
                return SavePlan(
                    engine=self.name,
                    mode="slot",
                    safe=True,
                    requires_rebuild=False,
                    notes=[slot_report.msg],
                )
            if state.kind == "TBL":
                repack_report = core.precheck(
                    state.kind,
                    state.data,
                    state.cluster,
                    state.entries,
                    "repack",
                )
                return SavePlan(
                    engine=self.name,
                    mode="repack",
                    safe=True,
                    requires_rebuild=True,
                    notes=[slot_report.msg, repack_report.msg],
                )
            repack_report = core.precheck(state.kind, state.data, state.cluster, state.entries, "repack")
            return SavePlan(
                engine=self.name,
                mode="repack",
                safe=repack_report.slot_safe,
                requires_rebuild=True,
                notes=[repack_report.msg],
            )

        report = core.precheck(state.kind, state.data, state.cluster, state.entries, requested_mode)
        return SavePlan(
            engine=self.name,
            mode=requested_mode,
            safe=report.slot_safe,
            requires_rebuild=report.repack_needed,
            notes=[report.msg],
        )

    def save(
        self,
        document: TextDocument,
        *,
        output_path: str | Path | None = None,
        **kwargs,
    ) -> Path:
        state: LegacyDocumentState = document.state
        if state.cluster is None:
            raise ValueError("Legacy engine could not find a writable string cluster in this file.")

        core = _legacy_modules()
        self._apply_document_changes(document)
        plan = self.preview_save(document, mode=kwargs.get("mode", "auto"))
        allow_unsafe_repack = bool(kwargs.get("allow_unsafe_repack", False))
        if not plan.safe and not allow_unsafe_repack:
            raise ValueError(
                "Legacy heuristic REPACK is unsafe and disabled by default; "
                "allow_unsafe_repack=True is required for an explicit experimental run."
            )
        effective_mode = plan.mode
        target = Path(output_path).resolve() if output_path else document.source_path
        check_stale = target == document.source_path.resolve()
        expected_texts = [unit.current_text for unit in document.units]
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=target.suffix,
            dir=target.parent,
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            if effective_mode == "slot":
                core.save_slot_write(
                    str(temporary),
                    state.data,
                    state.cluster,
                    state.entries,
                    do_backup=False,
                    check_stale=False,
                )
            else:
                core.save_repack_generic(
                    state.kind,
                    str(temporary),
                    state.data,
                    state.cluster,
                    state.entries,
                    do_backup=False,
                    check_stale=False,
                    tbl_offset_fields=state.tbl_offset_fields,
                    tbl_infer_ranges=state.tbl_infer_ranges,
                )
            candidate_payload = temporary.read_bytes()
            if (
                state.kind == "DAT"
                and state.data.startswith(b"#scp")
            ):
                try:
                    from ..kuro.dat import KuroDatEngine

                    KuroDatEngine().verify_exact_binary_compatibility(
                        target.name,
                        state.data,
                        candidate_payload,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"Legacy DAT {effective_mode} output failed exact binary verification; "
                        "the rebuild was discarded."
                    ) from exc
            verified = self.load(
                temporary,
                schema_hint=state.schema_hint,
                game=state.resolved_game,
            )
            if [unit.current_text for unit in verified.units] != expected_texts:
                raise RuntimeError("Legacy staged output failed the full text roundtrip verification.")
            candidate_bytes = wrapCLE(candidate_payload, state.cle_layers)
            atomic_write_bytes(
                target,
                candidate_bytes,
                do_backup=kwargs.get("do_backup", False),
                expected_bytes=state.source_bytes if check_stale else None,
            )
            refreshed = self.load(
                target,
                schema_hint=state.schema_hint,
                game=state.resolved_game,
            )
            if [unit.current_text for unit in refreshed.units] != expected_texts:
                raise RuntimeError("Legacy wrapped output failed the final text verification.")
            document.source_path = refreshed.source_path
            document.units = refreshed.units
            document.metadata = refreshed.metadata
            document.state = refreshed.state
            document.rebuild_index()
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def _apply_document_changes(self, document: TextDocument) -> None:
        state: LegacyDocumentState = document.state
        if len(document.units) != len(state.entries):
            raise ValueError(
                "Legacy document units no longer match the parsed entry table."
            )
        for unit, entry in zip(document.units, state.entries):
            if (
                unit.index != entry.index
                or int(unit.metadata.get("offset", -1)) != entry.offset
            ):
                raise ValueError(
                    "Legacy document unit order or offsets changed after parsing."
                )
            entry.new_text = unit.current_text
