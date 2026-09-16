"""Read-only content search, independent of batch scan/write state."""
from dataclasses import dataclass, field, replace
from pathlib import Path

from .business import ServiceBatchHit
from .session import DocumentSession, SessionOptions
from .text_presentation import find_text_spans


@dataclass
class TextSearchResult:
    hits: list[ServiceBatchHit] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    parsed: int = 0
    cancelled: bool = False


def search_text_targets(targets, query: str, *, options: SessionOptions, case_sensitive=False,
                        on_hits=None, on_progress=None, cancelled=None, prepare_target=None):
    """Emit the first hit immediately, then bounded batches, without batch-write state."""
    result = TextSearchResult()
    session = DocumentSession()
    pending = []
    stopped = cancelled or (lambda: False)
    def flush():
        if pending:
            if on_hits is not None:
                on_hits(tuple(pending))
            pending.clear()
    for target in targets:
        if stopped():
            result.cancelled = True
            break
        try:
            selected = replace(options)
            if not selected.schema_hint and target.logical_path.lower().endswith(".tbl"):
                selected.schema_hint = Path(target.logical_path).stem
            path = prepare_target(target) if prepare_target is not None else target.path
            document = session.open_document(path, options=selected)
            for unit in document.units:
                if stopped():
                    result.cancelled = True
                    break
                for start, end in find_text_spans(unit.current_text, query, case_sensitive=case_sensitive):
                    if stopped():
                        result.cancelled = True
                        break
                    hit = ServiceBatchHit(
                        checked=False, file=target.path, kind=document.kind.value,
                        unit_index=unit.index, location=unit.location,
                        original_text=unit.current_text, new_text=unit.current_text,
                        pair_old=unit.current_text[start:end], pair_new=unit.current_text[start:end],
                        logical_file=target.logical_path, source_id=target.source_id,
                        match_start=start, match_end=end, writable=False, write_mode="search-only",
                    )
                    result.hits.append(hit)
                    pending.append(hit)
                    if len(result.hits) == 1 or len(pending) >= 64:
                        flush()
            if not result.cancelled:
                result.parsed += 1
        except Exception as exc:
            result.errors.append(f"{target.logical_path}: {exc}")
        flush()
        if on_progress is not None:
            on_progress(result.parsed, len(result.errors), target.logical_path)
    result.cancelled = result.cancelled or stopped()
    return result


def search_pac_targets(sources, query, **kwargs):
    """Read private snapshots, never materialize/mutate a live GUI workspace.

    sources contains (BusinessFileTarget, PacArchive, requires_cache) records captured on the UI
    thread. Hits retain the real workspace path and logical identity; temporary
    snapshot paths never escape this function.
    """
    from .archive.fpac import FpacArchiveService
    from .paths import create_runtime_dir, cleanup_runtime_dir

    root = create_runtime_dir("text_search")
    service = FpacArchiveService()
    archives = {target.path: (archive, requires_cache) for target, archive, requires_cache in sources}
    serial = 0
    def prepare(target):
        nonlocal serial
        archive, requires_cache = archives[target.path]
        if not service.source_is_current(archive):
            raise RuntimeError("源 PAC 已变化，请重新打开资源后搜索。")
        current = Path(target.path)
        if current.is_file():
            before = current.stat()
            payload = current.read_bytes()
            after = current.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError("工作区文本在读取期间发生变化，请重新搜索。")
        else:
            if requires_cache:
                raise FileNotFoundError("已修改的工作区文本丢失，不能用原版内容代替搜索。")
            payload = service.read_entry_bytes(archive, target.logical_path)
        serial += 1
        snapshot = root / f"{serial:06d}{current.suffix}"
        snapshot.write_bytes(payload)
        return snapshot
    try:
        return search_text_targets(
            [target for target, _archive, _requires_cache in sources], query,
            prepare_target=prepare, **kwargs,
        )
    finally:
        cleanup_runtime_dir(root)
