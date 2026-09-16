from __future__ import annotations

import ast
import gc
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ...domain import CapabilityLevel, DocumentKind, EngineCapability, SavePlan, TextDocument, TextUnit
from ...io_utils import atomic_write_bytes
from ...paths import cleanup_runtime_dir, create_runtime_dir
from ..base import EngineBase
from ..relocation import (
    DatReferenceLayout,
    parse_dat_references,
    splice_referenced_strings,
)
from .processcle import unwrapCLE, wrapCLE
from .support import clear_disasm_modules, import_kuro_module


@dataclass(slots=True)
class KuroDatState:
    script_source: str
    source_filename: str
    original_bytes: bytes
    original_payload: bytes = b""
    cle_layers: tuple[bytes, ...] = ()
    layout: DatReferenceLayout | None = None


_CWD_LOCK = threading.RLock()
_LOCATION_NAME = re.compile(r"^Loc_\d+$")


def _script_structure_fingerprint(script_source: str) -> tuple[object, ...]:
    """Return the non-text structure of a generated DAT assembler script.

    A successful PUSHSTRING reread is not enough to validate a script rebuild:
    instruction widths, function boundaries, jumps, command operands and
    struct values can change while the visible text list stays identical.  The
    fingerprint ignores only structurally typed string-pool payloads (including
    function names), plus generated location-label numbering.
    """

    tree = ast.parse(script_source)
    function_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "script"
        ),
        None,
    )
    if function_node is None:
        raise ValueError("DAT assembler script function was not found.")

    location_names: dict[str, str] = {}

    def normalize(node: ast.AST, *, wildcard_strings: bool = False):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                if wildcard_strings:
                    return ("text",)
                if _LOCATION_NAME.fullmatch(node.value):
                    canonical = location_names.setdefault(
                        node.value,
                        f"location_{len(location_names)}",
                    )
                    return ("location", canonical)
            return ("constant", type(node.value).__name__, node.value)
        if isinstance(node, ast.Name):
            return ("name", node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return ("negative", normalize(node.operand, wildcard_strings=wildcard_strings))
        if isinstance(node, (ast.List, ast.Tuple)):
            return (
                type(node).__name__,
                tuple(
                    normalize(item, wildcard_strings=wildcard_strings)
                    for item in node.elts
                ),
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return (
                "call",
                node.func.id,
                tuple(
                    normalize(item, wildcard_strings=wildcard_strings)
                    for item in node.args
                ),
                tuple(
                    (
                        item.arg,
                        normalize(item.value, wildcard_strings=wildcard_strings),
                    )
                    for item in node.keywords
                ),
            )
        raise ValueError(f"Unsupported DAT fingerprint node: {type(node).__name__}")

    calls: list[object] = []
    for statement in function_node.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            raise ValueError("DAT assembler script contains unsupported statements.")
        call = statement.value
        if not isinstance(call.func, ast.Name):
            raise ValueError("DAT assembler calls must use simple function names.")
        if call.func.id == "PUSHSTRING":
            calls.append(normalize(call, wildcard_strings=True))
            continue
        if call.func.id == "add_struct":
            normalized_keywords = []
            for keyword in call.keywords:
                normalized_keywords.append(
                    (
                        keyword.arg,
                        normalize(
                            keyword.value,
                            wildcard_strings=keyword.arg == "array2",
                        ),
                    )
                )
            calls.append(
                (
                    "call",
                    call.func.id,
                    tuple(normalize(item) for item in call.args),
                    tuple(normalized_keywords),
                )
            )
            continue
        if call.func.id in {"create_script_header", "add_function"}:
            editable_keywords = {
                "varin",
                "varout",
                "input_args",
                "output_args",
            }
            if call.func.id == "add_function":
                editable_keywords.add("name")
            calls.append(
                (
                    "call",
                    call.func.id,
                    tuple(normalize(item) for item in call.args),
                    tuple(
                        (
                            keyword.arg,
                            normalize(
                                keyword.value,
                                wildcard_strings=keyword.arg in editable_keywords,
                            ),
                        )
                        for keyword in call.keywords
                    ),
                )
            )
            continue
        if call.func.id in {"CALLFROMANOTHERSCRIPT", "CALLFROMANOTHERSCRIPT2"}:
            calls.append(normalize(call, wildcard_strings=True))
            continue
        calls.append(normalize(call))
    return tuple(calls)


@contextmanager
def pushd(path: Path):
    with _CWD_LOCK:
        previous = Path.cwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(previous)


class _PushStringCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.items: list[ast.Constant] = []
        self._stack: list[ast.AST] = []

    def visit(self, node: ast.AST):
        self._stack.append(node)
        try:
            return super().visit(node)
        finally:
            self._stack.pop()

    def visit_Constant(self, node: ast.Constant):
        if not isinstance(node.value, str) or len(self._stack) < 2:
            return
        parent = self._stack[-2]
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id == "PUSHSTRING":
            self.items.append(node)


class KuroDatEngine(EngineBase):
    name = "kuro_dat"

    def capabilities(self) -> list[EngineCapability]:
        return [
            EngineCapability(
                engine=self.name,
                kind=DocumentKind.DAT,
                operation="binary-string-pool-relocation",
                level=CapabilityLevel.STABLE,
                notes="精确解析 #scp 全部字符串引用，保留脚本字节并重定位尾部字符串池。",
            ),
            EngineCapability(
                engine=self.name,
                kind=DocumentKind.DAT,
                operation="script-roundtrip-fallback",
                level=CapabilityLevel.EXPERIMENTAL,
                notes="仅在无法建立 #scp 二进制布局时使用反汇编/重编回退。",
            ),
        ]

    def load(self, path: str | Path, **kwargs) -> TextDocument:
        source = Path(path).resolve()
        original_bytes = source.read_bytes()
        original_payload, cle_layers = unwrapCLE(original_bytes)
        force_script_view = bool(kwargs.get("force_script_view", False))
        try:
            layout = (
                None
                if force_script_view
                else parse_dat_references(
                    original_payload,
                    tolerate_invalid_text=True,
                    include_unreferenced_text=True,
                )
            )
        except (EOFError, UnicodeError, ValueError):
            layout = None

        if layout is None:
            # Compatibility for partially supported KuroTools inputs: retain
            # the script view, but only the exact binary layout path is marked
            # as generally writable.
            script_source = self._disassemble_snapshot(
                source.name,
                original_bytes,
                decompile=kwargs.get("decompile", False),
                keep_artifacts=kwargs.get("keep_artifacts", False),
            )
            units = self._extract_units(script_source)
            unknown_command_count = script_source.count("Cmd_unknown_")
        else:
            # Exact #scp parsing is the complete text/search path.  Do not run
            # the experimental disassembler first: it can reject a valid
            # binary dialect, is much slower, and used to make search coverage
            # depend on an unrelated script-roundtrip implementation.
            script_source = ""
            units = self._extract_binary_units(layout)
            unknown_command_count = 0
        return TextDocument(
            source_path=source,
            kind=DocumentKind.DAT,
            engine=self.name,
            units=units,
            metadata={
                "entry_count": len(units),
                "decompile": kwargs.get("decompile", False),
                "unknown_command_count": unknown_command_count,
                "binary_layout": layout is not None,
                "invalid_pointer_count": (
                    len(layout.invalid_pointer_fields)
                    if layout is not None
                    else 0
                ),
                "damaged_reference_layout": bool(
                    layout is not None and layout.invalid_pointer_fields
                ),
                "full_pool_scan": layout is not None,
                "unreferenced_text_count": (
                    sum(not item.references for item in layout.strings.values())
                    if layout is not None
                    else 0
                ),
                "cle_layers": len(cle_layers),
            },
            state=KuroDatState(
                script_source=script_source,
                source_filename=source.name,
                original_bytes=original_bytes,
                original_payload=original_payload,
                cle_layers=cle_layers,
                layout=layout,
            ),
        )

    def preview_save(self, document: TextDocument, **kwargs) -> SavePlan:
        changed = len(document.changed_units())
        state: KuroDatState = document.state
        binary_layout = state.layout is not None
        invalid_pointer_count = (
            len(state.layout.invalid_pointer_fields)
            if state.layout is not None
            else 0
        )
        return SavePlan(
            engine=self.name,
            mode=(
                "copy"
                if changed == 0
                else "pool-splice"
                if binary_layout
                else "script-roundtrip"
            ),
            safe=changed == 0 or binary_layout,
            requires_rebuild=changed > 0,
            notes=[
                (
                    "DAT 使用结构化指针表剪接尾部字符串池；函数、指令、跳转和非文本操作数保持原字节。"
                    if binary_layout
                    else "DAT 无法建立完整二进制布局，将回退到实验性脚本重编。"
                ),
                *(
                    [
                        f"源 DAT 含 {invalid_pointer_count} 个既有损坏字符串指针；"
                        "文本仍可完整读取和编辑，但应先使用可信参考 PAC 执行指针修复再回包。"
                    ]
                    if invalid_pointer_count
                    else []
                ),
                f"Changed text entries: {changed}.",
            ] + (
                [f"保存后将恢复 {len(state.cle_layers)} 层 CLE 封装。"]
                if changed and state.cle_layers
                else []
            ),
        )

    def save(
        self,
        document: TextDocument,
        *,
        output_path: str | Path | None = None,
        **kwargs,
    ) -> Path:
        target = Path(output_path).resolve() if output_path else document.source_path
        state: KuroDatState = document.state
        changed = len(document.changed_units())
        script_source = state.script_source
        if changed == 0:
            payload = state.original_bytes
        elif state.layout is not None:
            original_payload = state.original_payload or state.original_bytes
            changes: list[tuple[int, bytes, bytes]] = []
            for unit in document.changed_units():
                try:
                    offset = int(unit.metadata["text_offset"])
                    expected_length = int(unit.metadata["text_byte_length"])
                    encoding = str(unit.metadata.get("text_encoding", "utf-8"))
                    old_raw = unit.original_text.encode(encoding)
                    new_raw = unit.current_text.encode(encoding)
                except (KeyError, LookupError, TypeError, ValueError, UnicodeError) as exc:
                    raise ValueError("DAT text metadata is incomplete or stale.") from exc
                if len(old_raw) != expected_length:
                    raise ValueError("DAT text byte length changed outside the document model.")
                changes.append((offset, old_raw, new_raw))
            rebuilt_payload, _mapping = splice_referenced_strings(
                original_payload,
                changes,
                [
                    *state.layout.pointer_fields.values(),
                    *state.layout.invalid_pointer_fields.values(),
                ],
                immutable_prefix_end=state.layout.strings_start,
            )
            payload = wrapCLE(rebuilt_payload, state.cle_layers)
            self._verify_binary_payload(
                target.name,
                state,
                payload,
                [unit.current_text for unit in document.units],
            )
        else:
            if not kwargs.get("allow_unsafe_repack", False):
                raise ValueError(
                    "Kuro DAT script roundtrip is experimental and disabled by default; "
                    "explicitly enable experimental REPACK to continue."
                )
            script_source = self._apply_changes(state.script_source, document.units)
            script_source = self._replace_script_name(script_source, target.stem)
            payload = self._assemble(
                target.stem,
                script_source,
                keep_artifacts=kwargs.get("keep_artifacts", False),
            )
            self._verify_payload(
                target.name,
                payload,
                [unit.current_text for unit in document.units],
                script_source,
            )
        expected = state.original_bytes if target == document.source_path.resolve() else None
        atomic_write_bytes(
            target,
            payload,
            do_backup=kwargs.get("do_backup", False),
            expected_bytes=expected,
        )
        refreshed = self.load(
            target,
            keep_artifacts=False,
            force_script_view=state.layout is None,
        )
        if [unit.current_text for unit in refreshed.units] != [
            unit.current_text for unit in document.units
        ]:
            raise RuntimeError("Saved DAT failed the final text verification.")
        document.source_path = refreshed.source_path
        document.units = refreshed.units
        document.metadata = refreshed.metadata
        document.state = refreshed.state
        document.rebuild_index()
        return target

    def _verify_payload(
        self,
        filename: str,
        payload: bytes,
        expected_texts: list[str],
        expected_script_source: str,
    ) -> None:
        verify_dir = create_runtime_dir("kuro_dat_verify")
        verify_path = verify_dir / filename
        try:
            atomic_write_bytes(verify_path, payload)
            verified = self.load(
                verify_path,
                keep_artifacts=False,
                force_script_view=True,
            )
            actual_texts = [unit.current_text for unit in verified.units]
            if actual_texts != expected_texts:
                raise RuntimeError("Kuro DAT staged output failed the PUSHSTRING roundtrip verification.")
            if _script_structure_fingerprint(verified.state.script_source) != _script_structure_fingerprint(
                expected_script_source
            ):
                raise RuntimeError(
                    "Kuro DAT staged output changed non-text script structure."
                )
        finally:
            cleanup_runtime_dir(verify_dir)

    def _verify_binary_payload(
        self,
        filename: str,
        original_state: KuroDatState,
        candidate_bytes: bytes,
        expected_texts: list[str],
    ) -> None:
        candidate_payload, candidate_layers = unwrapCLE(candidate_bytes)
        if candidate_layers != original_state.cle_layers:
            raise RuntimeError("DAT staged output changed its CLE wrapper stack.")
        candidate_layout = parse_dat_references(
            candidate_payload,
            tolerate_invalid_text=True,
            include_unreferenced_text=True,
        )
        actual_texts = [
            unit.current_text for unit in self._extract_binary_units(candidate_layout)
        ]
        if actual_texts != expected_texts:
            raise RuntimeError("DAT staged output failed the complete text roundtrip verification.")
        self.verify_exact_binary_compatibility(
            filename,
            original_state.original_bytes,
            candidate_bytes,
        )

    @staticmethod
    def _exact_structure_fingerprint(
        payload: bytes,
        layout: DatReferenceLayout,
    ) -> tuple[object, ...]:
        """Fingerprint every fixed DAT byte except typed string pointers."""

        if any(
            reference.field_offset + reference.width > layout.strings_start
            for reference in layout.pointer_fields.values()
        ):
            raise RuntimeError(
                "DAT string-pointer fields are not wholly contained in the fixed prefix."
            )
        fixed_prefix = bytearray(payload[: layout.strings_start])
        topology: list[tuple[object, ...]] = []
        for reference in sorted(
            [
                *layout.pointer_fields.values(),
                *layout.invalid_pointer_fields.values(),
            ],
            key=lambda item: item.field_offset,
        ):
            fixed_prefix[
                reference.field_offset : reference.field_offset + reference.width
            ] = b"\0" * reference.width
            topology.append(
                (
                    reference.field_offset,
                    reference.width,
                    reference.tagged,
                    reference.category,
                )
            )
        return (
            layout.strings_start,
            layout.function_starts,
            tuple(topology),
            bytes(fixed_prefix),
        )

    def verify_exact_binary_compatibility(
        self,
        filename: str,
        original_bytes: bytes,
        candidate_bytes: bytes,
    ) -> None:
        """Reject exact relocation if any non-text DAT byte/topology changed."""

        original_payload, original_layers = unwrapCLE(original_bytes)
        candidate_payload, candidate_layers = unwrapCLE(candidate_bytes)
        if candidate_layers != original_layers:
            raise RuntimeError(
                f"DAT staged output changed its CLE wrapper stack: {filename}"
            )
        original_layout = parse_dat_references(
            original_payload,
            tolerate_invalid_text=True,
            include_unreferenced_text=True,
        )
        candidate_layout = parse_dat_references(
            candidate_payload,
            tolerate_invalid_text=True,
            include_unreferenced_text=True,
        )
        if self._exact_structure_fingerprint(
            original_payload,
            original_layout,
        ) != self._exact_structure_fingerprint(candidate_payload, candidate_layout):
            raise RuntimeError(
                "DAT staged output changed a non-text byte or string-reference topology."
            )

    def verify_structural_compatibility(
        self,
        filename: str,
        original_bytes: bytes,
        candidate_bytes: bytes,
    ) -> None:
        """Reject a DAT rewrite whose non-text script topology changed."""

        original_source = self._disassemble_snapshot(
            filename,
            original_bytes,
            decompile=False,
            keep_artifacts=False,
        )
        candidate_source = self._disassemble_snapshot(
            filename,
            candidate_bytes,
            decompile=False,
            keep_artifacts=False,
        )
        if _script_structure_fingerprint(candidate_source) != _script_structure_fingerprint(
            original_source
        ):
            raise RuntimeError(
                "DAT staged output changed non-text script structure."
            )

    def _disassemble_snapshot(
        self,
        filename: str,
        source_bytes: bytes,
        *,
        decompile: bool,
        keep_artifacts: bool,
    ) -> str:
        """Disassemble exactly the byte snapshot retained by the document."""

        with _CWD_LOCK:
            clear_disasm_modules()
            disassembler_module = import_kuro_module("disasm.ED9Disassembler")
            temp_dir = create_runtime_dir("kuro_dat_work")
            try:
                local_copy = temp_dir / filename
                atomic_write_bytes(local_copy, source_bytes)
                with pushd(temp_dir):
                    disassembler = disassembler_module.ED9Disassembler(
                        False,
                        decompile,
                    )
                    disassembler.parse(str(local_copy))
                    script_source = (
                        temp_dir / f"{Path(filename).stem}.py"
                    ).read_text(encoding="utf-8")
                    if (
                        getattr(disassembler, "stream", None)
                        and not disassembler.stream.closed
                    ):
                        disassembler.stream.close()
                    del disassembler
                    return script_source
            finally:
                clear_disasm_modules()
                gc.collect()
                if not keep_artifacts:
                    cleanup_runtime_dir(temp_dir)

    def _assemble(self, stem: str, script_source: str, *, keep_artifacts: bool) -> bytes:
        with _CWD_LOCK:
            clear_disasm_modules()
            assembler_module = import_kuro_module("disasm.ED9Assembler")
            temp_dir = create_runtime_dir("kuro_dat_build")
            try:
                script_path = temp_dir / f"{stem}.py"
                script_path.write_text(script_source, encoding="utf-8")
                with pushd(temp_dir):
                    self._execute_assembler_script(
                        script_source,
                        assembler_module,
                        str(script_path),
                    )
                    return (temp_dir / f"{stem}.dat").read_bytes()
            finally:
                clear_disasm_modules()
                gc.collect()
                if not keep_artifacts:
                    cleanup_runtime_dir(temp_dir)

    def _extract_units(self, script_source: str) -> list[TextUnit]:
        tree = ast.parse(script_source)
        collector = _PushStringCollector()
        collector.visit(tree)
        lines = script_source.splitlines(keepends=True)
        line_offsets = self._line_offsets(lines)
        units: list[TextUnit] = []
        for index, node in enumerate(collector.items):
            start_col = self._byte_col_to_char(lines[node.lineno - 1], node.col_offset)
            end_col = self._byte_col_to_char(lines[node.end_lineno - 1], node.end_col_offset)
            start = line_offsets[node.lineno - 1] + start_col
            end = line_offsets[node.end_lineno - 1] + end_col
            units.append(
                TextUnit(
                    index=index,
                    original_text=node.value,
                    current_text=node.value,
                    location=f"{node.lineno}:{start_col + 1}",
                    context="PUSHSTRING",
                    metadata={"span": (start, end)},
                )
            )
        return units

    @staticmethod
    def _extract_binary_units(layout: DatReferenceLayout) -> list[TextUnit]:
        units: list[TextUnit] = []
        for item in sorted(layout.strings.values(), key=lambda value: value.offset):
            if not item.raw:
                continue
            categories = sorted(
                {
                    reference.category
                    for reference in item.references
                }
            )
            units.append(
                TextUnit(
                    index=len(units),
                    original_text=item.text,
                    current_text=item.text,
                    location=f"字符串池[0x{item.offset:08X}]",
                    context=(
                        "DAT " + "/".join(categories)
                        if categories
                        else "DAT unreferenced-pool"
                    ),
                    metadata={
                        "binary_layout": True,
                        "text_offset": item.offset,
                        "text_encoding": item.encoding,
                        "text_byte_length": len(item.raw),
                        "reference_count": len(item.references),
                        "reference_categories": categories,
                    },
                )
            )
        return units

    def _apply_changes(self, script_source: str, units: list[TextUnit]) -> str:
        updated = script_source
        for unit in sorted(units, key=lambda item: item.metadata["span"][0], reverse=True):
            start, end = unit.metadata["span"]
            updated = updated[:start] + json.dumps(unit.current_text, ensure_ascii=False) + updated[end:]
        return updated

    def _replace_script_name(self, script_source: str, stem: str) -> str:
        tree = ast.parse(script_source)
        lines = script_source.splitlines(keepends=True)
        line_offsets = self._line_offsets(lines)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "create_script_header":
                continue
            keyword = next((item for item in node.keywords if item.arg == "name"), None)
            if keyword is None or not isinstance(keyword.value, ast.Constant):
                raise ValueError("DAT script header does not contain a literal name.")
            value = keyword.value
            start_col = self._byte_col_to_char(lines[value.lineno - 1], value.col_offset)
            end_col = self._byte_col_to_char(lines[value.end_lineno - 1], value.end_col_offset)
            start = line_offsets[value.lineno - 1] + start_col
            end = line_offsets[value.end_lineno - 1] + end_col
            return script_source[:start] + json.dumps(stem, ensure_ascii=False) + script_source[end:]
        raise ValueError("DAT script header was not found.")

    def _execute_assembler_script(self, source: str, assembler_module, filename: str) -> None:
        tree = ast.parse(source, filename=filename)
        if len(tree.body) != 3:
            raise ValueError("Unsafe DAT assembler script layout.")
        import_node, function_node, call_node = tree.body
        if not (
            isinstance(import_node, ast.ImportFrom)
            and import_node.module == "disasm.ED9Assembler"
            and len(import_node.names) == 1
            and import_node.names[0].name == "*"
        ):
            raise ValueError("Unsafe DAT assembler import.")
        if not (
            isinstance(function_node, ast.FunctionDef)
            and function_node.name == "script"
            and not function_node.decorator_list
            and not function_node.args.args
        ):
            raise ValueError("Unsafe DAT assembler function definition.")
        if not all(isinstance(item, ast.Expr) and isinstance(item.value, ast.Call) for item in function_node.body):
            raise ValueError("DAT assembler script contains unsupported statements.")
        if not (
            isinstance(call_node, ast.Expr)
            and isinstance(call_node.value, ast.Call)
            and isinstance(call_node.value.func, ast.Name)
            and call_node.value.func.id == "script"
            and not call_node.value.args
            and not call_node.value.keywords
        ):
            raise ValueError("Unsafe DAT assembler entry point.")

        allowed_nodes = (
            ast.Module,
            ast.FunctionDef,
            ast.arguments,
            ast.Expr,
            ast.Call,
            ast.Name,
            ast.Load,
            ast.keyword,
            ast.Constant,
            ast.List,
            ast.Tuple,
            ast.UnaryOp,
            ast.USub,
        )
        call_names: set[str] = set()
        safe_tree = ast.Module(body=[function_node, call_node], type_ignores=[])
        for node in ast.walk(safe_tree):
            if not isinstance(node, allowed_nodes):
                raise ValueError(f"Unsupported DAT assembler syntax: {type(node).__name__}")
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name):
                    raise ValueError("DAT assembler calls must use simple function names.")
                call_names.add(node.func.id)
            if isinstance(node, ast.keyword) and node.arg is None:
                raise ValueError("DAT assembler does not allow expanded keyword arguments.")

        namespace = {"__builtins__": {}}
        for name in call_names - {"script"}:
            value = getattr(assembler_module, name, None)
            if not callable(value) or getattr(value, "__module__", None) != assembler_module.__name__:
                raise ValueError(f"DAT assembler call is not allowed: {name}")
            namespace[name] = value
        ast.fix_missing_locations(safe_tree)
        exec(compile(safe_tree, filename, "exec"), namespace, namespace)

    @staticmethod
    def _line_offsets(lines: list[str]) -> list[int]:
        offsets: list[int] = []
        running = 0
        for line in lines:
            offsets.append(running)
            running += len(line)
        if not offsets:
            offsets.append(0)
        return offsets

    @staticmethod
    def _byte_col_to_char(line: str, byte_col: int) -> int:
        return len(line.encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))
