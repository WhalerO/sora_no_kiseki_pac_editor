from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from types import SimpleNamespace

from .core import RetextService
from .domain import GameVersion, WorkflowMode, is_text_document_path
from .io_utils import atomic_write_bytes
from .engines.legacy import diff_core
from .session import SessionOptions


@dataclass(slots=True)
class ServiceBatchHit:
    checked: bool
    file: str
    kind: str
    unit_index: int
    location: str
    original_text: str
    new_text: str
    pair_old: str
    pair_new: str
    logical_file: str = ""
    source_id: str = ""
    match_start: int = -1
    match_end: int = -1
    occurrence_index: int = 0
    writable: bool = True
    write_mode: str = ""
    write_note: str = ""
    full_match: bool = False
    rule_index: int = -1


@dataclass(slots=True, frozen=True)
class BatchMapping:
    """One batch mapping with its own search-boundary policy."""

    old: str
    new: str
    full_match: bool = False


BatchMappingLike = (
    BatchMapping
    | tuple[str, str]
    | tuple[str, str, bool]
)


@dataclass(slots=True, frozen=True)
class BusinessFileTarget:
    path: str
    logical_path: str
    source_id: str = ""


@dataclass(slots=True, frozen=True)
class DiffFileRow:
    rel: str
    old_path: str | None
    new_path: str | None
    old_size: int
    new_size: int
    status: str


@dataclass(slots=True)
class BusinessBatchScanResult:
    hits: list[ServiceBatchHit]
    errors: list[str]
    fast_hit_count: int
    target_count: int = 0
    parsed_file_count: int = 0
    text_unit_count: int = 0
    complete: bool = True


@dataclass(slots=True)
class DiffEntryRow:
    index_old: int | None
    index_new: int | None
    text_old: str
    text_new: str
    status: str


def parse_mapping_lines(raw_text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in re.split(r"[\r\n;；]+", raw_text):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = re.split(r"\s*(?:=>|->|→|\s-\s)\s*", stripped, maxsplit=1)
        if len(parts) != 2:
            continue
        old, new = parts
        old = old.strip()
        if old:
            pairs.append((old, new.strip()))
    return pairs


def normalize_batch_mappings(
    pairs: list[BatchMappingLike],
) -> list[BatchMapping]:
    """Normalize new per-row mappings and the historical tuple API."""

    normalized: list[BatchMapping] = []
    for index, pair in enumerate(pairs):
        if isinstance(pair, BatchMapping):
            mapping = pair
        elif isinstance(pair, (tuple, list)) and len(pair) in {2, 3}:
            mapping = BatchMapping(
                old=str(pair[0]),
                new=str(pair[1]),
                full_match=bool(pair[2]) if len(pair) == 3 else False,
            )
        else:
            raise TypeError(
                f"批量映射第 {index + 1} 行必须是 BatchMapping、"
                "(旧文本, 新文本) 或 (旧文本, 新文本, 完全匹配)。"
            )
        normalized.append(mapping)
    return normalized


def serialize_batch_behavior(hits: list[ServiceBatchHit]) -> dict[str, object]:
    """Create a portable per-occurrence check-state document."""

    return {
        "format": "tis-retext-batch-behavior",
        "version": 1,
        "operations": [
            {
                "source_id": hit.source_id,
                "logical_file": hit.logical_file or Path(hit.file).name,
                "unit_index": hit.unit_index,
                "location": hit.location,
                "old": hit.pair_old,
                "new": hit.pair_new,
                "full_match": bool(hit.full_match),
                "rule_index": hit.rule_index,
                "occurrence_index": hit.occurrence_index,
                "match_start": hit.match_start,
                "original_sha256": hashlib.sha256(
                    hit.original_text.encode("utf-8")
                ).hexdigest(),
                "checked": bool(hit.checked),
            }
            for hit in hits
        ],
    }


def load_batch_behavior(raw: str | bytes | dict[str, object]) -> list[dict[str, object]]:
    payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    if not isinstance(payload, dict) or payload.get("format") != "tis-retext-batch-behavior":
        raise ValueError("不是受支持的批量处理行为文件。")
    if int(payload.get("version", 0)) != 1:
        raise ValueError("不支持该批量处理行为文件版本。")
    operations = payload.get("operations")
    if not isinstance(operations, list):
        raise ValueError("批量处理行为文件缺少 operations 数组。")
    return [item for item in operations if isinstance(item, dict)]


def apply_batch_behavior(
    hits: list[ServiceBatchHit],
    operations: list[dict[str, object]],
) -> int:
    """Apply exact-source rules first, then portable unambiguous rules."""

    exact: dict[tuple[object, ...], bool] = {}
    portable_states: dict[tuple[object, ...], set[bool]] = defaultdict(set)
    for item in operations:
        portable = _batch_behavior_key(item)
        source = str(item.get("source_id", ""))
        checked = bool(item.get("checked", True))
        portable_states[portable].add(checked)
        if source:
            exact[(source, *portable)] = checked

    applied = 0
    for hit in hits:
        record = {
            "source_id": hit.source_id,
            "logical_file": hit.logical_file or Path(hit.file).name,
            "unit_index": hit.unit_index,
            "location": hit.location,
            "old": hit.pair_old,
            "new": hit.pair_new,
            "full_match": bool(hit.full_match),
            "rule_index": hit.rule_index,
            "occurrence_index": hit.occurrence_index,
            "match_start": hit.match_start,
            "original_sha256": hashlib.sha256(
                hit.original_text.encode("utf-8")
            ).hexdigest(),
        }
        portable = _batch_behavior_key(record)
        exact_key = (hit.source_id, *portable)
        if hit.source_id and exact_key in exact:
            hit.checked = bool(hit.writable and exact[exact_key])
            applied += 1
        elif len(portable_states.get(portable, ())) == 1:
            hit.checked = bool(hit.writable and next(iter(portable_states[portable])))
            applied += 1
    return applied


def _batch_behavior_key(item: dict[str, object]) -> tuple[object, ...]:
    return (
        str(item.get("logical_file", "")).replace("\\", "/"),
        int(item.get("unit_index", -1)),
        str(item.get("location", "")),
        str(item.get("old", "")),
        str(item.get("new", "")),
        bool(item.get("full_match", False)),
        int(item.get("rule_index", -1)),
        int(item.get("occurrence_index", 0)),
        int(item.get("match_start", -1)),
        str(item.get("original_sha256", "")),
    )


class WorkspaceBusiness:
    def __init__(self) -> None:
        self.diff_core = diff_core
        self.service = RetextService()
        self.last_scan_errors: list[str] = []
        self.last_scan_target_count = 0
        self.last_scan_parsed_file_count = 0
        self.last_scan_text_unit_count = 0
        self.last_scan_complete = True

    def scan_batch_hits(self, roots: list[str], globs: str, pairs: list[BatchMappingLike]):
        """Compatibility alias for the safe, structure-aware batch scanner."""

        return self.scan_service_batch_hits(roots, globs, pairs)

    def execute_equal_batch(self, selected_hits, pairs: list[BatchMappingLike], *, do_backup: bool = True):
        """Compatibility entry point that refuses legacy raw-byte hits.

        Equal-length replacements no longer receive a separate binary path;
        they use the same parsed TextUnit transaction as every other mapping.
        """

        structural = [
            hit for hit in selected_hits if isinstance(hit, ServiceBatchHit)
        ]
        rejected = [
            hit for hit in selected_hits if not isinstance(hit, ServiceBatchHit)
        ]
        ok = fail = 0
        logs: list[str] = []
        if structural:
            ok, fail, logs = self.execute_service_batch(
                structural,
                do_backup=do_backup,
            )
        if rejected:
            fail += len(rejected)
            logs.extend(
                f"[SKIP] {getattr(hit, 'file', '<未知文件>')}："
                "旧版任意字节等长替换已禁用；请重新扫描为结构化文本命中。"
                for hit in rejected
            )
        return ok, fail, logs

    def scan_mixed_batch_targets(
        self,
        targets: list[BusinessFileTarget],
        globs: str,
        pairs: list[BatchMappingLike],
        *,
        use_equal_fast_path: bool,
        options: SessionOptions | None = None,
        ordered: bool = False,
    ) -> BusinessBatchScanResult:
        rejected_targets = [
            target
            for target in targets
            if not self._is_editable_target(target)
        ]
        targets = [
            target
            for target in targets
            if self._is_editable_target(target)
        ]
        boundary_errors = [
            f"[SKIP] {target.logical_path or target.path}：批量修改仅支持 TBL/DAT。"
            for target in rejected_targets
        ]
        # ``use_equal_fast_path`` remains in the call signature so older GUI
        # adapters keep working, but every mapping now follows one structural
        # scan and staged roundtrip-verified save path.
        service_hits = self.scan_service_batch_targets(targets, pairs, options=options, ordered=True) if ordered else (
            self.scan_service_batch_targets(targets, pairs)
            if options is None
            else self.scan_service_batch_targets(targets, pairs, options=options)
        )
        return BusinessBatchScanResult(
            hits=service_hits,
            errors=[*boundary_errors, *self.last_scan_errors],
            fast_hit_count=0,
            target_count=self.last_scan_target_count,
            parsed_file_count=self.last_scan_parsed_file_count,
            text_unit_count=self.last_scan_text_unit_count,
            complete=self.last_scan_complete,
        )

    def execute_mixed_batch(
        self,
        selected_hits: list[object],
        pairs: list[BatchMappingLike],
        *,
        do_backup: bool = True,
        options: SessionOptions | None = None,
    ) -> tuple[int, int, list[str]]:
        structural = [hit for hit in selected_hits if isinstance(hit, ServiceBatchHit)]
        mappings = normalize_batch_mappings(pairs)
        if any(hit.rule_index >= 0 and (
            hit.rule_index >= len(mappings) or mappings[hit.rule_index] != BatchMapping(hit.pair_old, hit.pair_new, hit.full_match)
        ) for hit in structural):
            return 0, len(structural), ["[SKIP] 映射内容或顺序已改变，请重新查找匹配后再执行。"]
        rejected = [hit for hit in selected_hits if not isinstance(hit, ServiceBatchHit)]
        if not self.last_scan_complete:
            checked = [hit for hit in structural if hit.checked]
            return 0, len(checked) + len(rejected), [
                "[SKIP] 最近一次批量搜索未完整解析全部 TBL/DAT；"
                "为避免基于遗漏清单写入，必须修复错误并重新扫描。"
            ]
        ok, fail, logs = self.execute_service_batch(
            structural,
            do_backup=do_backup,
            options=options,
        ) if structural else (0, 0, [])
        if rejected:
            fail += len(rejected)
            logs.extend(
                f"[SKIP] {getattr(hit, 'file', '<未知文件>')}：不是结构化文本命中。"
                for hit in rejected
            )
        return ok, fail, logs

    def scan_service_batch_hits(
        self,
        roots: list[str],
        globs: str,
        pairs: list[BatchMappingLike],
        *,
        options: SessionOptions | None = None,
    ) -> list[ServiceBatchHit]:
        targets = self.collect_file_targets(roots, globs)
        if options is None:
            return self.scan_service_batch_targets(targets, pairs)
        return self.scan_service_batch_targets(targets, pairs, options=options)

    def collect_file_targets(
        self,
        roots: list[str],
        globs: str,
    ) -> list[BusinessFileTarget]:
        patterns = [
            item.strip()
            for item in (globs or "*.tbl,*.dat").split(",")
            if item.strip()
        ]
        targets: list[BusinessFileTarget] = []
        seen_paths: set[Path] = set()
        for raw_root in roots:
            root = Path(raw_root).resolve()
            if not root.exists():
                continue
            if root.is_file():
                if (
                    is_text_document_path(root)
                    and root not in seen_paths
                    and any(fnmatch.fnmatch(root.name, pattern) for pattern in patterns)
                ):
                    seen_paths.add(root)
                    targets.append(
                        BusinessFileTarget(
                            path=str(root),
                            logical_path=root.name,
                            source_id=str(root),
                        )
                    )
                continue
            for pattern in patterns:
                for file_path in root.rglob(pattern):
                    resolved = file_path.resolve()
                    if (
                        not resolved.is_file()
                        or not is_text_document_path(resolved)
                        or resolved in seen_paths
                    ):
                        continue
                    seen_paths.add(resolved)
                    targets.append(
                        BusinessFileTarget(
                            path=str(resolved),
                            logical_path=resolved.relative_to(root).as_posix(),
                            source_id=str(root),
                        )
                    )
        return sorted(targets, key=lambda item: (item.logical_path, item.path))

    def scan_service_batch_targets(
        self,
        targets: list[BusinessFileTarget],
        pairs: list[BatchMappingLike],
        *,
        options: SessionOptions | None = None,
        preflight: bool = True,
        ordered: bool = False,
    ) -> list[ServiceBatchHit]:
        mappings = normalize_batch_mappings(pairs)
        hits: list[ServiceBatchHit] = []
        self.last_scan_errors = []
        self.last_scan_target_count = len(targets)
        self.last_scan_parsed_file_count = 0
        self.last_scan_text_unit_count = 0
        self.last_scan_complete = True
        parsed: list[tuple[BusinessFileTarget, object, list[ServiceBatchHit]]] = []

        if not targets:
            self.last_scan_complete = False
            self.last_scan_errors.append(
                "[INCOMPLETE] 所选范围没有展开出任何 TBL/DAT；"
                "请重新添加 PAC/目录范围，并检查文件筛选条件。"
            )
            return hits

        # Phase 1 is deliberately read-only: parse and search the complete
        # selected scope before any save preflight mutates an in-memory
        # document.  A failure in one file keeps the partial hits visible for
        # diagnosis, but marks the inventory incomplete so callers can forbid
        # execution.
        for target in targets:
            if not self._is_editable_target(target):
                self.last_scan_errors.append(
                    f"[INCOMPLETE] {target.logical_path or target.path}："
                    "批量搜索仅支持 TBL/DAT，所选范围未完整解析。"
                )
                self.last_scan_complete = False
                continue
            file_path = Path(target.path)
            document = self._load_service_document(
                file_path,
                target.logical_path,
                options=options,
            )
            if document is None:
                self.last_scan_complete = False
                continue
            self.last_scan_parsed_file_count += 1
            self.last_scan_text_unit_count += len(document.units)
            invalid_pointer_count = int(
                getattr(document, "metadata", {}).get("invalid_pointer_count", 0)
                or 0
            )
            if invalid_pointer_count:
                self.last_scan_errors.append(
                    f"[DAMAGED] {target.logical_path or target.path}：检测到 "
                    f"{invalid_pointer_count} 个显式无效 DAT 字符串指针；"
                    "物理字符串池已完整纳入搜索/编辑，但源文件的引用关系已损坏，"
                    "回包前请在“缓存管理”中使用可信参考 PAC 修复 DAT 指针。"
                )
            scanner = self._scan_ordered_document_hits if ordered else self._scan_document_hits
            file_hits = scanner(
                file_path,
                document,
                mappings,
                logical_file=target.logical_path,
                source_id=target.source_id,
            )
            if preflight and file_hits:
                parsed.append((target, document, file_hits))
            hits.extend(file_hits)

        if self.last_scan_parsed_file_count != self.last_scan_target_count:
            self.last_scan_complete = False

        # Phase 2 only evaluates whether the already-frozen hit inventory can
        # be written.  Read-only/preflight errors do not retroactively hide
        # search results or change the definition of scan completeness.
        if preflight:
            for target, document, file_hits in parsed:
                self._preflight_service_hits(
                    document,
                    file_hits,
                    target.logical_path,
                    options=options,
                )
        return sorted(hits, key=lambda hit: (hit.rule_index, hit.logical_file, hit.file, hit.unit_index, hit.match_start)) if ordered else hits

    def preflight_service_hits(
        self,
        selected_hits: list[ServiceBatchHit],
        *,
        options: SessionOptions | None = None,
    ) -> None:
        """Evaluate writeability only after the caller freezes its selection.

        Patch manifests can intentionally select an outer phrase while leaving
        an overlapping inner-word candidate unchecked.  Preflighting the whole
        search inventory would incorrectly mark the valid selected transaction
        read-only.  Reload each selected file and assess exactly those rows.
        """

        for file_path, hits in self._group_hits_by_file(selected_hits).items():
            first = hits[0]
            logical_file = first.logical_file
            document = self._load_service_document(
                Path(file_path),
                logical_file,
                options=options,
            )
            if document is None:
                for hit in hits:
                    hit.writable = False
                    hit.checked = False
                    hit.write_mode = "blocked"
                    hit.write_note = "无法按扫描时的引擎重新载入文件。"
                continue
            self._preflight_service_hits(
                document,
                hits,
                logical_file,
                options=options,
            )

    def execute_service_batch(
        self,
        selected_hits: list[ServiceBatchHit],
        *,
        do_backup: bool = True,
        options: SessionOptions | None = None,
    ):
        by_file = self._group_hits_by_file(selected_hits)

        ok = 0
        fail = 0
        logs: list[str] = []
        for file_path, hits in by_file.items():
            try:
                first = hits[0]
                logical_file = getattr(first, "logical_file", "")
                if not all(self._is_editable_hit(hit) for hit in hits):
                    fail += len(hits)
                    logs.append(
                        f"[SKIP] {logical_file or file_path}：批量修改仅支持 TBL/DAT。"
                    )
                    continue
                if not all(hit.writable for hit in hits):
                    fail += len(hits)
                    logs.append(
                        f"[SKIP] {logical_file or file_path}：包含扫描阶段判定为不可写的操作。"
                    )
                    continue
                document = self._load_service_document(
                    Path(file_path),
                    logical_file,
                    options=options,
                )
                if document is None:
                    raise RuntimeError("无法按扫描时的前端策略重新载入文件。")
                self._apply_service_hits(document, hits)
                plan = self.service.preview_save(document)
                self._save_and_verify_document(
                    document,
                    Path(file_path),
                    do_backup=do_backup,
                    options=options,
                )
                ok += len(hits)
                display_name = getattr(first, "logical_file", "") or os.path.basename(file_path)
                display_mode = (
                    "repack-risk"
                    if any(hit.write_mode == "repack-risk" for hit in hits)
                    else plan.mode
                )
                logs.append(
                    f"[SAVE] {display_name}：{plan.engine}/{display_mode}，命中 {len(hits)}"
                )
            except Exception as exc:
                fail += len(hits)
                first = hits[0]
                display_name = getattr(first, "logical_file", "") or os.path.basename(file_path)
                logs.append(f"[ERR] {display_name}：{exc}")
        return ok, fail, logs

    def execute_repack_batch(self, selected_hits, *, do_backup: bool = True):
        checked = [
            hit for hit in selected_hits if getattr(hit, "checked", True)
        ]
        return 0, len(checked), [
            "[SKIP] 旧版启发式批量 REPACK 已禁用；请使用统一结构化批量页。"
        ] if checked else []

    def build_diff_index(self, roots_old: list[str], roots_new: list[str], globs: str):
        return self.diff_core.build_file_diff_index(roots_old, roots_new, globs)

    def build_target_diff_index(
        self,
        old_targets: list[BusinessFileTarget],
        new_targets: list[BusinessFileTarget],
    ) -> list[DiffFileRow]:
        old_by_name = self._index_targets(old_targets, "旧版本")
        new_by_name = self._index_targets(new_targets, "新版本")
        rows: list[DiffFileRow] = []
        for logical_path in sorted(set(old_by_name) | set(new_by_name)):
            old = old_by_name.get(logical_path)
            new = new_by_name.get(logical_path)
            old_path = old.path if old else None
            new_path = new.path if new else None
            old_size = Path(old_path).stat().st_size if old_path else 0
            new_size = Path(new_path).stat().st_size if new_path else 0
            if old_path and new_path:
                status = "same" if self._file_sha256(old_path) == self._file_sha256(new_path) else "modified"
            elif old_path:
                status = "only_old"
            elif new_path:
                status = "only_new"
            else:
                status = "unknown"
            rows.append(
                DiffFileRow(
                    rel=logical_path,
                    old_path=old_path,
                    new_path=new_path,
                    old_size=old_size,
                    new_size=new_size,
                    status=status,
                )
            )
        return rows

    def compute_entry_diff_auto(
        self,
        old_path: str | None,
        new_path: str | None,
        *,
        logical_path: str = "",
        options: SessionOptions | None = None,
    ) -> list[DiffEntryRow]:
        display_name = logical_path or os.path.basename(old_path or new_path or "")
        if old_path and not new_path:
            return [DiffEntryRow(None, None, f"[仅旧版本存在] {display_name}", "", "deleted")]
        if new_path and not old_path:
            return [DiffEntryRow(None, None, "", f"[仅新版本存在] {display_name}", "added")]
        if not old_path and not new_path:
            return []

        selected = options or SessionOptions(mode=WorkflowMode.SAFE)
        route_path = logical_path or old_path or new_path or ""
        load_kwargs: dict[str, object] = {
            "mode": selected.mode,
            "engine": selected.engine_for_path(route_path),
            "game": GameVersion.normalize(selected.game).value,
            "keep_artifacts": selected.keep_artifacts,
        }
        if logical_path.lower().endswith(".tbl"):
            load_kwargs["schema_hint"] = Path(logical_path).stem
        old_doc = self.service.load(old_path, **load_kwargs)
        new_doc = self.service.load(new_path, **load_kwargs)
        rows: list[DiffEntryRow] = []
        old_texts = [unit.current_text for unit in old_doc.units]
        new_texts = [unit.current_text for unit in new_doc.units]
        matcher = difflib.SequenceMatcher(a=old_texts, b=new_texts, autojunk=False)
        for opcode, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            old_slice = old_doc.units[old_start:old_end]
            new_slice = new_doc.units[new_start:new_end]
            if opcode == "equal":
                pairs = zip(old_slice, new_slice)
            elif opcode == "replace":
                pairs = zip_longest(old_slice, new_slice)
            elif opcode == "delete":
                pairs = ((unit, None) for unit in old_slice)
            else:
                pairs = ((None, unit) for unit in new_slice)
            for old_unit, new_unit in pairs:
                if old_unit and new_unit:
                    status = "same" if old_unit.current_text == new_unit.current_text else "modified"
                elif old_unit:
                    status = "deleted"
                else:
                    status = "added"
                rows.append(
                    DiffEntryRow(
                        index_old=old_unit.index if old_unit else None,
                        index_new=new_unit.index if new_unit else None,
                        text_old=old_unit.current_text if old_unit else "",
                        text_new=new_unit.current_text if new_unit else "",
                        status=status,
                    )
                )
        return rows

    def _load_service_document(
        self,
        file_path: Path,
        logical_path: str = "",
        *,
        options: SessionOptions | None = None,
    ):
        try:
            selected = options or SessionOptions(mode=WorkflowMode.SAFE)
            route_path = logical_path or file_path
            load_kwargs: dict[str, object] = {
                "mode": selected.mode,
                "engine": selected.engine_for_path(route_path),
                "keep_artifacts": selected.keep_artifacts,
                "game": GameVersion.normalize(selected.game).value,
            }
            schema_hint = selected.schema_hint.strip()
            if not schema_hint and logical_path.lower().endswith(".tbl"):
                schema_hint = Path(logical_path).stem
            if schema_hint:
                load_kwargs["schema_hint"] = schema_hint
            return self.service.load(file_path, **load_kwargs)
        except Exception as exc:
            self.last_scan_errors.append(
                f"[INCOMPLETE] {logical_path or file_path.name}：解析/搜索失败：{exc}"
            )
            return None

    def _preflight_service_hits(
        self,
        document,
        hits: list[ServiceBatchHit],
        logical_path: str,
        *,
        options: SessionOptions | None = None,
    ) -> None:
        if not hits:
            return
        actionable_hits = [hit for hit in hits if hit.pair_old != hit.pair_new]
        for hit in hits:
            if hit.pair_old == hit.pair_new:
                hit.writable = False
                hit.checked = False
                hit.write_mode = "search-only"
                hit.write_note = "新旧文本相同；该命中仅用于搜索和定位，不会参与写入。"
        if not actionable_hits:
            return
        try:
            self._apply_service_hits(document, actionable_hits)
            plan = self.service.preview_save(document)
            selected = options or SessionOptions(mode=WorkflowMode.SAFE)
            experimental_modes = {
                "legacy": {"repack"},
                "kuro_dat": {"script-roundtrip"},
            }
            risky_allowed = bool(
                selected.allow_risky_repack
                and plan.requires_rebuild
                and plan.mode in experimental_modes.get(document.engine, set())
            )
            writable = bool(plan.safe or risky_allowed)
            write_mode = "repack-risk" if risky_allowed and not plan.safe else plan.mode
            note = " ".join(plan.notes).strip()
            if risky_allowed and not plan.safe:
                note = (
                    "已由用户显式允许启发式回退写入；暂存产物仍需通过完整文本回读，"
                    "#scp DAT 还需通过非文本脚本结构校验；未知语义仍不保证。 " + note
                ).strip()
            for hit in actionable_hits:
                hit.writable = writable
                hit.checked = writable
                hit.write_mode = write_mode
                hit.write_note = note
            if not writable:
                self.last_scan_errors.append(
                    f"[READ-ONLY] {logical_path or document.source_path.name}：{note}"
                )
            elif risky_allowed and not plan.safe:
                self.last_scan_errors.append(
                    f"[RISK] {logical_path or document.source_path.name}：{note}"
                )
        except Exception as exc:
            for hit in actionable_hits:
                hit.writable = False
                hit.checked = False
                hit.write_mode = "blocked"
                hit.write_note = str(exc)
            self.last_scan_errors.append(
                f"[READ-ONLY] {logical_path or document.source_path.name}：{exc}"
            )

    def _scan_document_hits(
        self,
        file_path: Path,
        document,
        pairs: list[BatchMapping],
        *,
        logical_file: str = "",
        source_id: str = "",
    ) -> list[ServiceBatchHit]:
        hits: list[ServiceBatchHit] = []
        seen: set[tuple[int, int, int, str, str]] = set()
        for unit in document.units:
            current_text = unit.current_text
            for mapping in pairs:
                old = mapping.old
                new = mapping.new
                if not old:
                    continue
                if mapping.full_match:
                    spans = [(0, len(old))] if current_text == old else []
                else:
                    spans = [
                        match.span()
                        for match in re.finditer(re.escape(old), current_text)
                    ]
                for occurrence_index, (start, end) in enumerate(spans):
                    identity = (unit.index, start, end, old, new)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    search_only = old == new
                    hits.append(
                        ServiceBatchHit(
                            checked=not search_only,
                            file=str(file_path),
                            kind=document.kind.value.upper(),
                            unit_index=unit.index,
                            location=unit.location,
                            original_text=current_text,
                            new_text=current_text[:start] + new + current_text[end:],
                            pair_old=old,
                            pair_new=new,
                            logical_file=logical_file,
                            source_id=source_id,
                            match_start=start,
                            match_end=end,
                            occurrence_index=occurrence_index,
                            writable=not search_only,
                            write_mode="search-only" if search_only else "",
                            write_note=(
                                "新旧文本相同；该命中仅用于搜索和定位，不会参与写入。"
                                if search_only
                                else ""
                            ),
                            full_match=mapping.full_match,
                        )
                    )
        return hits

    def _scan_ordered_document_hits(self, file_path, document, pairs, *, logical_file="", source_id=""):
        """Preview each rule against the output of earlier rules, without touching the source document."""
        hits = []
        for unit in document.units:
            current = unit.current_text
            for rule_index, mapping in enumerate(pairs):
                if mapping.old not in current or (mapping.full_match and current != mapping.old):
                    continue
                view = SimpleNamespace(kind=document.kind, units=[SimpleNamespace(
                    index=unit.index, location=unit.location, current_text=current)])
                stage = self._scan_document_hits(file_path, view, [mapping], logical_file=logical_file, source_id=source_id)
                for hit in stage:
                    hit.rule_index = rule_index
                hits.extend(stage)
                if stage and mapping.old != mapping.new:
                    # All occurrences of one rule use the same input. Replacing
                    # once prevents a self-containing replacement from looping.
                    current = mapping.new if mapping.full_match else current.replace(mapping.old, mapping.new)
        return hits

    def _group_hits_by_file(self, hits) -> dict[str, list]:
        grouped = defaultdict(list)
        for hit in hits:
            if getattr(hit, "checked", True):
                grouped[hit.file].append(hit)
        return grouped

    def _apply_service_hits(self, document, hits: list[ServiceBatchHit]) -> None:
        if any(hit.rule_index >= 0 for hit in hits):
            if any(hit.rule_index < 0 for hit in hits):
                raise RuntimeError("不能混用顺序替换和旧版同时替换清单，请重新扫描。")
            stages = defaultdict(list)
            for hit in hits:
                stages[hit.rule_index].append(hit)
            for stage in sorted(stages):
                try:
                    self._apply_simultaneous_hits(document, stages[stage])
                except RuntimeError as exc:
                    raise RuntimeError(f"第 {stage + 1} 条映射的输入已变化（可能取消了前序依赖）。"
                                       "该文件未写入，请调整映射并重新扫描。 " + str(exc)) from exc
            return
        self._apply_simultaneous_hits(document, hits)

    def _apply_simultaneous_hits(self, document, hits: list[ServiceBatchHit]) -> None:
        by_unit: dict[int, list[ServiceBatchHit]] = defaultdict(list)
        for hit in hits:
            by_unit[hit.unit_index].append(hit)
        for unit_index, unit_hits in by_unit.items():
            unit = document.get_unit(unit_index)
            first = unit_hits[0]
            if unit.location != first.location or any(
                unit.current_text != hit.original_text or unit.location != hit.location
                for hit in unit_hits
            ):
                raise RuntimeError(
                    f"Stale batch hit at {first.location}; rescan before applying changes."
                )
            operations: list[tuple[int, int, str, ServiceBatchHit]] = []
            for hit in unit_hits:
                start = hit.match_start
                end = hit.match_end
                if not (
                    0 <= start <= end <= len(unit.current_text)
                    and unit.current_text[start:end] == hit.pair_old
                ):
                    matches = list(
                        re.finditer(re.escape(hit.pair_old), unit.current_text)
                    )
                    if hit.occurrence_index < 0 or hit.occurrence_index >= len(matches):
                        raise RuntimeError(
                            f"Stale batch occurrence at {hit.location}; "
                            "rescan before applying changes."
                        )
                    start, end = matches[hit.occurrence_index].span()
                operations.append((start, end, hit.pair_new, hit))

            operations.sort(key=lambda item: (item[0], item[1]))
            previous_end = -1
            for start, end, _replacement, hit in operations:
                if start < previous_end:
                    raise RuntimeError(
                        f"Overlapping batch operations at {hit.location}; adjust the checked rows."
                    )
                previous_end = end

            output: list[str] = []
            cursor = 0
            for start, end, replacement, _hit in operations:
                output.append(unit.current_text[cursor:start])
                output.append(replacement)
                cursor = end
            output.append(unit.current_text[cursor:])
            unit.current_text = "".join(output)

    def _save_and_verify_document(
        self,
        document,
        target: Path,
        *,
        do_backup: bool,
        options: SessionOptions | None = None,
    ) -> None:
        expected_texts = [unit.current_text for unit in document.units]
        original_bytes = target.read_bytes()
        loaded = getattr(document.state, "source_bytes", None)
        if loaded is None:
            loaded = getattr(document.state, "original_bytes", None)
        if loaded is not None and loaded != original_bytes:
            raise RuntimeError(f"Refusing to overwrite a file that changed on disk: {target}")
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=target.suffix,
            dir=target.parent,
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            selected = options or SessionOptions(mode=WorkflowMode.SAFE)
            self.service.save(
                document,
                output_path=temporary,
                do_backup=False,
                keep_artifacts=selected.keep_artifacts,
                allow_unsafe_repack=selected.allow_risky_repack,
            )
            load_kwargs: dict[str, object] = {"engine": document.engine}
            schema_hint = document.metadata.get("schema_hint")
            if schema_hint:
                load_kwargs["schema_hint"] = schema_hint
            resolved_game = document.metadata.get("resolved_game")
            if resolved_game:
                load_kwargs["game"] = resolved_game
            verified = self.service.load(temporary, **load_kwargs)
            actual_texts = [unit.current_text for unit in verified.units]
            if actual_texts != expected_texts:
                raise RuntimeError("Staged batch output failed the text roundtrip verification.")
            atomic_write_bytes(
                target,
                temporary.read_bytes(),
                do_backup=do_backup,
                expected_bytes=original_bytes,
            )
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _is_editable_target(target: BusinessFileTarget) -> bool:
        return (
            is_text_document_path(target.path)
            and is_text_document_path(target.logical_path)
        )

    @staticmethod
    def _is_editable_hit(hit) -> bool:
        file_path = str(getattr(hit, "file", ""))
        logical_path = str(getattr(hit, "logical_file", ""))
        return (
            is_text_document_path(file_path)
            and (not logical_path or is_text_document_path(logical_path))
        )

    @staticmethod
    def _index_targets(
        targets: list[BusinessFileTarget],
        side_label: str,
    ) -> dict[str, BusinessFileTarget]:
        indexed: dict[str, BusinessFileTarget] = {}
        duplicates: set[str] = set()
        for target in targets:
            logical_path = target.logical_path.replace("\\", "/").lstrip("/")
            if logical_path in indexed:
                duplicates.add(logical_path)
            indexed[logical_path] = target
        if duplicates:
            names = "、".join(sorted(duplicates)[:5])
            raise ValueError(f"{side_label}选择中存在重复 PAC 内路径：{names}。请缩小节点选择范围。")
        return indexed

    @staticmethod
    def _file_sha256(path: str) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
