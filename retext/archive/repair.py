from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal

from ..engines.kuro.processcle import unwrapCLE, wrapCLE
from ..engines.relocation import (
    parse_dat_references,
    repair_dat_references_from_reference,
)
from ..io_utils import atomic_write_bytes
from ..paths import cleanup_runtime_dir, create_runtime_dir
from .domain import PacBuildReport
from .fpac import FpacArchiveService


DatRepairStrategy = Literal["conservative", "aggressive"]
RepairProgress = Callable[[int, int, str], None]


@dataclass(slots=True, frozen=True)
class PacDatRepairReport:
    reference_path: Path
    source_path: Path
    output_path: Path
    examined_dat_count: int
    repaired_dat_count: int
    corrected_pointer_count: int
    invalid_pointer_count_before: int
    repaired_entries: tuple[str, ...]
    build_report: PacBuildReport
    failed_entries: tuple[tuple[str, str], ...] = ()
    skipped_uncertain_pointer_count: int = 0
    aggressive_pointer_count: int = 0
    strategy: DatRepairStrategy = "conservative"

    @property
    def complete(self) -> bool:
        return not self.failed_entries


@dataclass(slots=True, frozen=True)
class DatIntegrityResult:
    logical_path: str
    text_count: int
    invalid_pointer_count: int
    corrected_pointer_count: int
    silent_pointer_count: int | None
    status: str
    message: str = ""
    comparison_mode: str = "unreferenced"
    skipped_uncertain_pointer_count: int = 0
    aggressive_pointer_count: int = 0

    @property
    def repairable(self) -> bool:
        return self.status == "repairable"


@dataclass(slots=True, frozen=True)
class PacDatScanReport:
    source_path: Path
    reference_path: Path | None
    requested_dat_count: int
    parsed_dat_count: int
    text_count: int
    invalid_pointer_count: int
    corrected_pointer_count: int
    silent_pointer_count: int | None
    complete: bool
    items: tuple[DatIntegrityResult, ...]
    skipped_uncertain_pointer_count: int = 0
    aggressive_pointer_count: int = 0
    strategy: DatRepairStrategy = "conservative"


@dataclass(slots=True, frozen=True)
class DatFileRepairReport:
    reference_path: Path
    source_path: Path
    output_path: Path
    scan: DatIntegrityResult


def _analyze_dat_payload(
    damaged_payload: bytes,
    *,
    logical_path: str,
    reference_payload: bytes | None = None,
    strategy: DatRepairStrategy = "conservative",
) -> DatIntegrityResult:
    return _plan_dat_payload(
        damaged_payload, logical_path=logical_path,
        reference_payload=reference_payload, strategy=strategy,
    )[0]


def _plan_dat_payload(
    damaged_payload: bytes,
    *,
    logical_path: str,
    reference_payload: bytes | None = None,
    strategy: DatRepairStrategy = "conservative",
) -> tuple[DatIntegrityResult, bytes]:
    """Analyze and prepare a verified repair once, shared by scan and repair."""
    if strategy not in {"conservative", "aggressive"}:
        raise ValueError(f"Unknown DAT reference-repair strategy: {strategy}")
    if not damaged_payload.startswith(b"#scp"):
        raise ValueError("目标不是可结构化扫描的 #scp DAT。")
    if reference_payload is None or reference_payload == damaged_payload:
        layout = parse_dat_references(
            damaged_payload, tolerate_invalid_text=True, include_unreferenced_text=True,
        )
        invalid = len(layout.invalid_pointer_fields)
        text_count = sum(bool(item.raw) for item in layout.strings.values())
        if reference_payload is None:
            return DatIntegrityResult(
                logical_path, text_count, invalid, 0, None,
                "damaged" if invalid else "clean-uncompared",
                "检测到显式无效指针；未提供参考文件，无法判定静默错链。" if invalid
                else "未发现显式无效指针；未提供参考文件，无法排除静默错链。",
            ), damaged_payload
        if not invalid:
            # Exact byte equality plus an actual structural parse is evidence;
            # historical hashes and "same size" are never used as shortcuts.
            return DatIntegrityResult(
                logical_path, text_count, 0, 0, 0, "clean",
                "与参考关系一致，无需修复。", comparison_mode="full",
            ), damaged_payload
    if not reference_payload.startswith(b"#scp"):
        raise ValueError("参考文件不是 #scp DAT。")
    diagnostics: dict[str, object] = {}
    repaired, corrected = repair_dat_references_from_reference(
        reference_payload, damaged_payload, strategy=strategy, diagnostics=diagnostics,
    )
    invalid = int(diagnostics["invalid_pointer_count_before"])
    comparison_mode = str(diagnostics.get("comparison_mode", "full"))
    skipped_uncertain = int(diagnostics.get("skipped_uncertain_pointer_count", 0) or 0)
    aggressive = int(diagnostics.get("aggressive_pointer_count", 0) or 0)
    return DatIntegrityResult(
        logical_path=logical_path,
        text_count=int(diagnostics["text_count"]),
        invalid_pointer_count=invalid,
        corrected_pointer_count=corrected,
        silent_pointer_count=max(0, corrected - invalid),
        status="repairable" if corrected else "uncertain" if skipped_uncertain else "clean",
        message=(
            "参考差异较大，已进入保守对照；仅认定显式损坏及具有同函数、"
            "同偏移证据的相邻静默错链。"
            if comparison_mode == "conservative" else
            "已启用激进对照：参考关系差异较大，证据不足但能够按字段和字符串池"
            "对齐的有效指针也会被改写；必须另存并进行游戏验证。"
            if comparison_mode == "aggressive" else
            "参考关系可以安全完整对齐。" if corrected else "与参考关系一致，无需修复。"
        ),
        comparison_mode=comparison_mode,
        skipped_uncertain_pointer_count=skipped_uncertain,
        aggressive_pointer_count=aggressive,
    ), repaired

class DatReferenceRepairService:
    """Scan or repair one wrapped/unwrapped #scp DAT without a PAC workspace."""

    def scan(
        self,
        damaged_dat: str | Path,
        reference_dat: str | Path | None = None,
        *,
        strategy: DatRepairStrategy = "conservative",
    ) -> DatIntegrityResult:
        source = Path(damaged_dat).resolve()
        damaged_payload, _damaged_layers = unwrapCLE(source.read_bytes())
        reference_payload = None
        if reference_dat is not None:
            reference = Path(reference_dat).resolve()
            reference_payload, _reference_layers = unwrapCLE(reference.read_bytes())
        return _analyze_dat_payload(
            damaged_payload,
            logical_path=source.name,
            reference_payload=reference_payload,
            strategy=strategy,
        )

    def repair(
        self,
        reference_dat: str | Path,
        damaged_dat: str | Path,
        output_dat: str | Path,
        *,
        do_backup: bool = True,
        strategy: DatRepairStrategy = "conservative",
    ) -> DatFileRepairReport:
        reference = Path(reference_dat).resolve()
        source = Path(damaged_dat).resolve()
        output = Path(output_dat).resolve()
        if output in {reference, source}:
            raise ValueError("DAT 指针修复必须另存为新文件，不能覆盖参考或待修复文件。")
        reference_payload, _reference_layers = unwrapCLE(reference.read_bytes())
        damaged_payload, damaged_layers = unwrapCLE(source.read_bytes())
        scan, repaired_payload = _plan_dat_payload(
            damaged_payload,
            logical_path=source.name,
            reference_payload=reference_payload,
            strategy=strategy,
        )
        atomic_write_bytes(
            output,
            wrapCLE(repaired_payload, damaged_layers),
            do_backup=do_backup,
        )
        verified_payload, _verified_layers = unwrapCLE(output.read_bytes())
        parse_dat_references(verified_payload)
        return DatFileRepairReport(reference, source, output, scan)


class PacDatReferenceRepairService:
    """Repair a damaged script PAC against a trusted same-version PAC."""

    def __init__(self, archive_service: FpacArchiveService | None = None) -> None:
        self.archive_service = archive_service or FpacArchiveService()

    def scan(
        self,
        damaged_pac: str | Path,
        reference_pac: str | Path | None = None,
        *,
        entry_names: Iterable[str] | None = None,
        strategy: DatRepairStrategy = "conservative",
        progress: RepairProgress | None = None,
    ) -> PacDatScanReport:
        damaged = self.archive_service.inspect(damaged_pac)
        reference = (
            self.archive_service.inspect(reference_pac)
            if reference_pac is not None
            else None
        )
        selected = set(entry_names) if entry_names is not None else None
        damaged_entries = [
            entry
            for entry in damaged.entries
            if entry.suffix == ".dat"
            and (selected is None or entry.name in selected)
        ]
        if selected is not None:
            missing = sorted(
                selected - {entry.name for entry in damaged_entries}
            )
            if missing:
                raise ValueError(
                    "待扫描 PAC 中不存在所选 DAT：" + ", ".join(missing[:8])
                )
        reference_names = (
            {entry.name for entry in reference.entries}
            if reference is not None
            else set()
        )

        items: list[DatIntegrityResult] = []
        complete = True
        for index, damaged_entry in enumerate(damaged_entries):
            try:
                damaged_payload, _layers = unwrapCLE(
                    self.archive_service.read_entry_bytes(damaged, damaged_entry.name)
                )
                if not damaged_payload.startswith(b"#scp"):
                    items.append(DatIntegrityResult(
                        damaged_entry.name, 0, 0, 0, None if reference is None else 0,
                        "not-scp", "不是 #scp DAT，不适用指针关系扫描。",
                    ))
                    continue
                reference_payload = None
                if reference is not None:
                    if damaged_entry.name not in reference_names:
                        raise ValueError("参考 PAC 缺少同路径 DAT。")
                    reference_payload, _layers = unwrapCLE(
                        self.archive_service.read_entry_bytes(reference, damaged_entry.name)
                    )
                items.append(_analyze_dat_payload(
                    damaged_payload, logical_path=damaged_entry.name,
                    reference_payload=reference_payload, strategy=strategy,
                ))
            except Exception as exc:
                items.append(DatIntegrityResult(
                    damaged_entry.name, 0, 0, 0, 0 if reference is not None else None,
                    "incompatible", str(exc),
                ))
                complete = False
            finally:
                if progress is not None:
                    progress(index + 1, len(damaged_entries), damaged_entry.name)

        silent_values = [
            item.silent_pointer_count
            for item in items
            if item.silent_pointer_count is not None
        ]
        return PacDatScanReport(
            source_path=damaged.source_path,
            reference_path=reference.source_path if reference is not None else None,
            requested_dat_count=len(damaged_entries),
            parsed_dat_count=sum(
                item.status not in {"incompatible", "not-scp"}
                for item in items
            ),
            text_count=sum(item.text_count for item in items),
            invalid_pointer_count=sum(item.invalid_pointer_count for item in items),
            corrected_pointer_count=sum(item.corrected_pointer_count for item in items),
            silent_pointer_count=(
                sum(silent_values) if reference is not None else None
            ),
            complete=complete,
            items=tuple(items),
            skipped_uncertain_pointer_count=sum(
                item.skipped_uncertain_pointer_count for item in items
            ),
            aggressive_pointer_count=sum(
                item.aggressive_pointer_count for item in items
            ),
            strategy=strategy,
        )

    def repair(
        self,
        reference_pac: str | Path,
        damaged_pac: str | Path,
        output_pac: str | Path,
        *,
        do_backup: bool = True,
        strategy: DatRepairStrategy = "conservative",
        progress: RepairProgress | None = None,
    ) -> PacDatRepairReport:
        reference = self.archive_service.inspect(reference_pac)
        damaged = self.archive_service.inspect(damaged_pac)
        output = Path(output_pac).resolve()
        if output in {reference.source_path.resolve(), damaged.source_path.resolve()}:
            raise ValueError("DAT 指针修复必须另存为新的 PAC，不能覆盖参考包或待修复包。")

        reference_names = {entry.name for entry in reference.entries}
        damaged_dat_names = {
            entry.name
            for entry in damaged.entries
            if entry.suffix == ".dat"
        }
        runtime = create_runtime_dir("pac_dat_reference_repair")
        replacements: dict[str, Path] = {}
        repaired_entries: list[str] = []
        failed_entries: list[tuple[str, str]] = []
        examined = 0
        corrected_total = 0
        invalid_before = 0
        skipped_uncertain_total = 0
        aggressive_total = 0
        try:
            for index, entry_name in enumerate(sorted(damaged_dat_names)):
                try:
                    damaged_entry = damaged.get_entry(entry_name)
                    damaged_payload, damaged_layers = unwrapCLE(
                        self.archive_service.read_entry_bytes(damaged, damaged_entry.name)
                    )
                    if not damaged_payload.startswith(b"#scp"):
                        continue

                    examined += 1
                    damaged_layout = parse_dat_references(
                        damaged_payload,
                        tolerate_invalid_text=True,
                    )
                    invalid_before += len(damaged_layout.invalid_pointer_fields)
                    if entry_name not in reference_names:
                        raise ValueError("参考 PAC 缺少同路径 DAT。")
                    reference_entry = reference.get_entry(entry_name)
                    reference_payload, _reference_layers = unwrapCLE(
                        self.archive_service.read_entry_bytes(reference, reference_entry.name)
                    )
                    if not reference_payload.startswith(b"#scp"):
                        raise ValueError("参考条目不是 #scp DAT。")

                    if reference_payload == damaged_payload and not damaged_layout.invalid_pointer_fields:
                        continue
                    diagnostics: dict[str, object] = {}
                    repaired_payload, corrected = repair_dat_references_from_reference(
                        reference_payload,
                        damaged_payload,
                        strategy=strategy,
                        diagnostics=diagnostics,
                    )
                    skipped_uncertain_total += int(
                        diagnostics.get("skipped_uncertain_pointer_count", 0) or 0
                    )
                    aggressive_total += int(
                        diagnostics.get("aggressive_pointer_count", 0) or 0
                    )
                except Exception as exc:
                    failed_entries.append((entry_name, str(exc)))
                    continue
                finally:
                    if progress is not None:
                        progress(index + 1, len(damaged_dat_names), entry_name)
                if not corrected:
                    continue
                repaired_file = runtime / f"repaired-{index:05d}.dat"
                atomic_write_bytes(
                    repaired_file,
                    wrapCLE(repaired_payload, damaged_layers),
                )
                replacements[entry_name] = repaired_file
                repaired_entries.append(entry_name)
                corrected_total += corrected

            build_report = self.archive_service.build(
                damaged,
                replacements,
                output,
                do_backup=do_backup,
            )
            return PacDatRepairReport(
                reference_path=reference.source_path,
                source_path=damaged.source_path,
                output_path=build_report.output_path,
                examined_dat_count=examined,
                repaired_dat_count=len(repaired_entries),
                corrected_pointer_count=corrected_total,
                invalid_pointer_count_before=invalid_before,
                repaired_entries=tuple(repaired_entries),
                build_report=build_report,
                failed_entries=tuple(failed_entries),
                skipped_uncertain_pointer_count=skipped_uncertain_total,
                aggressive_pointer_count=aggressive_total,
                strategy=strategy,
            )
        finally:
            cleanup_runtime_dir(runtime)
