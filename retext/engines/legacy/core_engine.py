# core_engine.py
from __future__ import annotations
import os, struct, re
from pathlib import Path
from typing import List, Tuple, Optional, Dict
try:
    from .models import Cluster, Entry, PrecheckReport
except ImportError:  # pragma: no cover - compatibility for the legacy standalone UI
    from models import Cluster, Entry, PrecheckReport
from retext.io_utils import atomic_write_bytes
from retext.engines.relocation import (
    StringReference,
    canonical_string_targets,
    discover_tbl_references,
    parse_dat_references,
    read_cstring,
    splice_referenced_strings,
)

# ------------------------- 编码策略（保留原编码优先） -------------------------
PREF_ENCODINGS = ["utf-8", "cp932", "shift_jis", "gbk", "latin1"]

def _try_decode(b: bytes):
    for enc in PREF_ENCODINGS:
        try:
            return b.decode(enc), enc
        except Exception:
            pass
    return b.decode("latin1", errors="replace"), "latin1"

def _try_encode(s: str, prefer: Optional[str]):
    if prefer:
        try:
            return s.encode(prefer)
        except Exception:
            pass
    for enc in PREF_ENCODINGS:
        try:
            return s.encode(enc)
        except Exception:
            pass
    return s.encode("latin1", errors="replace")

# ------------------------- 文件类型识别 -------------------------
def decide_kind_by_ext(path: str) -> str:
    return "TBL" if os.path.splitext(path)[1].lower() == ".tbl" else "DAT"

# ------------------------- 尾部字符串池探测 -------------------------
def _looks_text(b: bytes) -> bool:
    try:
        s = b.decode("utf-8")
    except Exception:
        return False
    if not s or not all(ch.isprintable() or ch.isspace() for ch in s):
        return False
    if not any(ch.isalnum() for ch in s):
        return False
    return not (len(s) < 3 and s.isascii())

def detect_tail_cluster(data: bytes) -> Optional[Cluster]:
    n = len(data)
    if n < 64:
        return None
    # Older builds only inspected the final 96 KiB.  Large DAT/TBL files can
    # start their valid NUL-terminated text pool much earlier, which made the
    # GUI silently omit searchable dialogue.  This remains a single linear
    # scan and avoids pushing a multi-pass workaround into the business/UI
    # layers.
    window_start = 0
    entries: List[Tuple[int, bytes]] = []
    cursor = window_start
    while cursor < n:
        end = data.find(b"\x00", cursor)
        if end < 0:
            end = n
        raw = data[cursor:end]
        # A window can begin in the middle of a string.  Do not promote that
        # partial fragment to the cluster base.
        is_partial = cursor == window_start and cursor > 0 and data[cursor - 1] != 0
        if raw and not is_partial:
            entries.append((cursor, raw))
        cursor = end + 1

    for index in range(len(entries)):
        sample = entries[index:index + 32]
        if len(sample) < 8:
            break
        required = 8 if len(sample) >= 12 else 6
        if (
            _looks_text(entries[index][1])
            and sum(_looks_text(raw) for _, raw in sample) >= required
        ):
            base = entries[index][0]
            return Cluster(base=base, start=base, end=n)
    return None

def parse_entries(cluster: Cluster, data: bytes) -> List[Entry]:
    out: List[Entry] = []
    i, idx = cluster.start, 0
    while i < cluster.end:
        while i < cluster.end and data[i] == 0x00:
            i += 1
        if i >= cluster.end:
            break
        j = i
        while j < cluster.end and data[j] != 0x00:
            j += 1
        raw = data[i:j]
        if raw and _looks_text(raw):
            txt, _ = _try_decode(raw)
            if txt.strip() != "":
                out.append(Entry(
                    index=idx,
                    offset=i - cluster.start,
                    old_bytes=raw,
                    old_text=txt,
                    new_text=txt
                ))
                idx += 1
        i = j + 1
    return out

# ------------------------- DAT(内嵌) 识别与解析 -------------------------
def _count_virt_ptrs(data: bytes) -> Tuple[int, List[int]]:
    cnt, offs = 0, []
    for i in range(0, len(data) - 3):
        v = struct.unpack_from("<I", data, i)[0]
        if (v & 0xC0000000) == 0xC0000000:
            off = v & 0x3FFFFFFF
            if 0 <= off < len(data):
                cnt += 1
                offs.append(off)
    return cnt, offs

def is_inline_dat(data: bytes, cluster: Optional[Cluster]) -> bool:
    cnt, offs = _count_virt_ptrs(data)
    if cnt == 0:
        return False
    if cluster is None:
        return True
    before = sum(1 for off in offs if off < cluster.base)
    return (before / cnt) > 0.40 or (len(data) < 64 * 1024 and cnt >= 8)

def inline_dat_entries(data: bytes) -> List[Entry]:
    targets = set()
    for i in range(0, len(data) - 3):
        v = struct.unpack_from("<I", data, i)[0]
        if (v & 0xC0000000) == 0xC0000000:
            off = v & 0x3FFFFFFF
            if 0 <= off < len(data):
                targets.add(off)
    out = []
    for idx, off in enumerate(sorted(targets)):
        j = off
        while j < len(data) and data[j] != 0x00:
            j += 1
        raw = data[off:j]
        if not raw or not _looks_text(raw):
            continue
        txt, _ = _try_decode(raw)
        if txt.strip():
            out.append(Entry(index=idx, offset=off, old_bytes=raw, old_text=txt, new_text=txt))
    return out

# ------------------------- 装载统一接口 -------------------------
def load_and_detect(path: str, kind: Optional[str] = None):
    data = Path(path).read_bytes()
    k = (kind or decide_kind_by_ext(path))
    return load_and_detect_bytes(data, k)


def load_and_detect_bytes(
    data: bytes,
    kind: str,
    *,
    tbl_text_targets: Optional[List[int]] = None,
    tbl_offset_fields: Optional[Dict[int, int]] = None,
    tbl_infer_ranges: Optional[List[Tuple[int, int]]] = None,
):
    k = kind.upper()
    if k == "TBL" and data.startswith(b"#TBL"):
        layout = discover_tbl_references(
            data,
            known_fields=tbl_offset_fields,
            infer_record_ranges=tbl_infer_ranges,
        )
        entries: List[Entry] = []
        text_targets = set(layout.inferred_text_fields.values())
        text_targets.update(tbl_text_targets or [])
        for target in canonical_string_targets(data, text_targets):
            raw, text = read_cstring(data, target)
            if not raw:
                continue
            entries.append(
                Entry(
                    index=len(entries),
                    offset=target,
                    old_bytes=raw,
                    old_text=text,
                    new_text=text,
                )
            )
        cluster = Cluster(
            base=0,
            start=layout.records_end,
            end=len(data),
            inline=True,
        )
        return data, cluster, entries, k
    if k == "DAT" and data.startswith(b"#scp"):
        layout = parse_dat_references(
            data,
            tolerate_invalid_text=True,
            include_unreferenced_text=True,
        )
        entries = []
        for item in sorted(layout.strings.values(), key=lambda value: value.offset):
            if not item.raw:
                continue
            entries.append(
                Entry(
                    index=len(entries),
                    offset=item.offset,
                    old_bytes=item.raw,
                    old_text=item.text,
                    new_text=item.text,
                )
            )
        cluster = Cluster(0, 0, len(data), inline=True)
        cluster.dat_layout = layout
        return data, cluster, entries, k
    cluster = detect_tail_cluster(data) if k == "DAT" else detect_tail_cluster(data)
    if k == "DAT" and is_inline_dat(data, cluster):
        fake = Cluster(base=0, start=0, end=len(data), inline=True)
        entries = inline_dat_entries(data)
        if cluster is not None:
            by_offset = {entry.offset: entry for entry in entries}
            for pool_entry in parse_entries(cluster, data):
                absolute_offset = cluster.base + pool_entry.offset
                if absolute_offset in by_offset:
                    continue
                by_offset[absolute_offset] = Entry(
                    index=0,
                    offset=absolute_offset,
                    old_bytes=pool_entry.old_bytes,
                    old_text=pool_entry.old_text,
                    new_text=pool_entry.new_text,
                )
            entries = sorted(by_offset.values(), key=lambda entry: entry.offset)
            for index, entry in enumerate(entries):
                entry.index = index
        return data, fake, entries, k
    if not cluster:
        return data, None, [], k
    entries = parse_entries(cluster, data)
    return data, cluster, entries, k

# ------------------------- 工具：槽后连续 0x00 空隙 -------------------------
def _contiguous_zeros_after(cluster: Cluster, data: bytes, abs_off: int, old_len: int) -> int:
    k = abs_off + old_len
    limit = cluster.end
    z = 0
    while k + z < limit and data[k + z] == 0x00:
        z += 1
    return z

def _slot_capacity(cluster: Cluster, data: bytes, abs_off: int, old_len: int) -> int:
    # At least one trailing zero must remain as the C-string terminator.
    trailing_zeros = _contiguous_zeros_after(cluster, data, abs_off, old_len)
    return old_len + max(0, trailing_zeros - 1)

# ------------------------- 预检 -------------------------
def precheck(kind: str, data: bytes, cluster: Cluster, entries: List[Entry], mode: str) -> PrecheckReport:
    if kind == "DAT" and (cluster.inline or is_inline_dat(data, cluster)):
        return precheck_inline_dat(data, entries, mode)

    slot_ok = True
    grow_bad = 0
    for e in entries:
        if e.new_text == e.old_text:
            continue
        _, enc = _try_decode(e.old_bytes)
        newb = _try_encode(e.new_text, enc)
        abs_off = cluster.base + e.offset
        room = _slot_capacity(cluster, data, abs_off, e.old_len)
        if len(newb) > room:
            slot_ok = False
            grow_bad += 1

    if mode == "slot":
        if slot_ok:
            return PrecheckReport(f"[VERIFY] SLOT 可写：所有增长均在紧邻 0x00 空隙内。", True, False)
        else:
            return PrecheckReport(f"[VERIFY] SLOT 不安全：有 {grow_bad} 处长度增长超过 0x00 空隙（建议 REPACK）。", False, True)
    else:
        rule = "记录列确认的绝对偏移(8-byte LE)" if kind == "TBL" else "0xC0000000|abs(4-byte LE)"
        return PrecheckReport(
            f"[VERIFY] REPACK：保留字符串池中未识别的间隙与尾部数据，"
            f"仅重排文本并重写前段指针（{rule}）；写入前后执行完整文本回读。",
            kind == "TBL",
            True,
        )

def precheck_inline_dat(data: bytes, entries: List[Entry], mode: str) -> PrecheckReport:
    if mode == "slot":
        bad = []
        for e in entries:
            if e.new_text == e.old_text:
                continue
            enc = _try_decode(e.old_bytes)[1]
            if len(_try_encode(e.new_text, enc)) > len(e.old_bytes):
                bad.append(e)
        if bad:
            return PrecheckReport(msg=f"[INLINE] SLOT 不安全：{len(bad)} 处增长，建议 REPACK。", slot_safe=False, repack_needed=True)
        else:
            return PrecheckReport(msg=f"[INLINE] SLOT 可写：全部修改不增长。", slot_safe=True, repack_needed=False)
    else:
        structured = data.startswith(b"#scp")
        return PrecheckReport(
            msg=(
                "[INLINE] REPACK：剪接尾部字符串池，并按 #scp 结构化字段重写 0xC000 指针；"
                "函数与指令区保持原字节。"
                if structured
                else "[INLINE] REPACK：按修改点右移后续内容，重写 0xC000 指针并执行完整文本回读。"
            ),
            slot_safe=structured,
            repack_needed=True,
        )

# ------------------------- 保存：槽内写入 -------------------------
def save_slot_write(
    path: str,
    data: bytes,
    cluster: Cluster,
    entries: List[Entry],
    *,
    do_backup: bool = False,
    check_stale: bool = True,
) -> None:
    buf = bytearray(data)
    for e in entries:
        if e.new_text == e.old_text:
            continue
        _, enc = _try_decode(e.old_bytes)
        newb = _try_encode(e.new_text, enc)
        abs_off = cluster.base + e.offset
        room = _slot_capacity(cluster, data, abs_off, e.old_len)
        if len(newb) > room:
            raise RuntimeError(
                f"SLOT 写入失败：#{e.index} 需要 {len(newb)}B > 可用 {room}B（offset=0x{abs_off:08X}），请使用 REPACK。"
            )

        old_len = e.old_len
        new_len = len(newb)

        end_pos = abs_off + new_len
        if end_pos >= cluster.end:
            raise RuntimeError(f"SLOT 写入失败：#{e.index} 没有可保留的字符串终止符。")
        # Slice assignment must use an equally sized slice.  Assigning a
        # longer value to the old-length slice inserts bytes into the bytearray
        # and shifts the whole file, which is the opposite of SLOT semantics.
        buf[abs_off:end_pos] = newb
        clear_end = max(end_pos + 1, abs_off + old_len + 1)
        buf[end_pos:clear_end] = b"\x00" * (clear_end - end_pos)

    target = Path(path).resolve()
    expected = data if check_stale and target.exists() else None
    atomic_write_bytes(target, bytes(buf), do_backup=do_backup, expected_bytes=expected)

# ------------------------- 常规：REPACK（尾池 + 指针重映射） -------------------------
def _rewrite_by_patterns(front: bytearray, patterns: List[Tuple[bytes, bytes]]) -> Tuple[int, int]:
    snap = bytes(front)
    hits = rew = 0
    for oldb, newb in patterns:
        i = 0
        while True:
            k = snap.find(oldb, i)
            if k < 0:
                break
            front[k:k+4] = newb
            hits += 1
            rew += 1
            i = k + 1
    return hits, rew


def _discover_tagged_pointer_fields(
    data: bytes,
    entries: List[Entry],
    *,
    entry_base: int,
    field_end: int,
    target_start: int,
    require_string_boundary: bool,
) -> List[Tuple[int, int]]:
    """Record likely tagged-pointer *field positions* in the original bytes.

    Legacy cannot prove the complete DAT instruction schema.  This therefore
    remains a heuristic, but it avoids the most damaging older behaviour:
    searching the rebuilt payload for a four-byte value also rewrote matching
    bytes inside strings and could cascade through already modified fields.
    """

    text_mask = bytearray(len(data))
    for entry in entries:
        start = entry_base + entry.offset
        end = start + entry.old_len + 1
        if 0 <= start < end <= len(data):
            text_mask[start:end] = b"\x01" * (end - start)

    fields: List[Tuple[int, int]] = []
    scan_end = min(max(0, field_end), len(data))
    for position in range(max(0, scan_end - 3)):
        if any(text_mask[position:position + 4]):
            continue
        value = struct.unpack_from("<I", data, position)[0]
        if (value & 0xC0000000) != 0xC0000000:
            continue
        target = value & 0x3FFFFFFF
        if not target_start <= target < len(data):
            continue
        if require_string_boundary and target and data[target - 1] != 0:
            continue
        fields.append((position, target))
    return fields


def _relocated_offset(offset: int, deltas: List[Tuple[int, int]]) -> int:
    """Relocate an original byte offset after variable-length string edits."""

    return offset + sum(change for start, change in deltas if offset > start)

def save_repack_generic(
    kind: str,
    path: str,
    data: bytes,
    cluster: Cluster,
    entries: List[Entry],
    *,
    do_backup: bool = False,
    check_stale: bool = True,
    tbl_offset_fields: Optional[Dict[int, int]] = None,
    tbl_infer_ranges: Optional[List[Tuple[int, int]]] = None,
) -> Tuple[int, int]:
    if kind == "TBL" and data.startswith(b"#TBL"):
        layout = discover_tbl_references(
            data,
            known_fields=tbl_offset_fields,
            infer_record_ranges=tbl_infer_ranges,
        )
        changes: List[Tuple[int, bytes, bytes]] = []
        for entry in entries:
            if entry.new_text == entry.old_text:
                continue
            encoding = _try_decode(entry.old_bytes)[1]
            changes.append(
                (
                    entry.offset,
                    entry.old_bytes,
                    _try_encode(entry.new_text, encoding),
                )
            )
        references = [
            StringReference(position, target, 8, False, "tbl-external")
            for position, target in layout.offset_fields.items()
        ]
        rebuilt, _mapping = splice_referenced_strings(
            data,
            changes,
            references,
            immutable_prefix_end=layout.records_end,
        )
        target = Path(path).resolve()
        expected = data if check_stale and target.exists() else None
        atomic_write_bytes(
            target,
            rebuilt,
            do_backup=do_backup,
            expected_bytes=expected,
        )
        deltas = [
            (offset, len(new) - len(old))
            for offset, old, new in changes
            if len(new) != len(old)
        ]
        moved = sum(
            1
            for reference in references
            if any(reference.target > start for start, _delta in deltas)
        )
        return moved, moved
    if kind == "DAT" and (cluster.inline or is_inline_dat(data, cluster)):
        return save_repack_inline_dat(
            path,
            data,
            entries,
            do_backup=do_backup,
            check_stale=check_stale,
        )

    base = cluster.base
    front = bytearray(data[:base])

    # Rebuild by splicing changed strings into the original pool.  The older
    # implementation concatenated only parsed entries, which collapsed extra
    # NUL padding and discarded any unrecognised tail bytes.  Keeping every
    # byte between entries makes variable-length DAT edits substantially more
    # conservative and leaves unrelated data intact.
    new_pool = bytearray()
    mapping: Dict[int, int] = {}
    deltas: List[Tuple[int, int]] = []
    pool_cursor = base
    for e in entries:
        old_abs = base + e.offset
        if old_abs < pool_cursor:
            raise RuntimeError("REPACK failed: overlapping or unsorted text entries.")
        new_pool += data[pool_cursor:old_abs]
        _, enc = _try_decode(e.old_bytes)
        nb = e.old_bytes if e.new_text == e.old_text else _try_encode(e.new_text, enc)
        new_abs = base + len(new_pool)
        mapping[old_abs] = new_abs
        new_pool += nb + b"\x00"
        if len(nb) != e.old_len:
            deltas.append((old_abs, len(nb) - e.old_len))
        pool_cursor = old_abs + e.old_len + 1
    new_pool += data[pool_cursor:]

    patterns: List[Tuple[bytes, bytes]] = []
    if kind == "TBL":
        for old_abs, new_abs in mapping.items():
            if old_abs != new_abs:
                patterns.append((struct.pack("<I", old_abs), struct.pack("<I", new_abs)))
    if kind == "TBL":
        ptr_hits, ptr_rew = _rewrite_by_patterns(front, patterns)
    else:
        # Record concrete fields in the original front section and rewrite only
        # those positions.  Targets may point at intentionally hidden tail data,
        # so target relocation is based on every preceding string-size delta.
        ptr_hits = ptr_rew = 0
        pointer_fields = _discover_tagged_pointer_fields(
            data,
            entries,
            entry_base=base,
            field_end=base,
            target_start=base,
            require_string_boundary=True,
        )
        for position, old_abs in pointer_fields:
            new_abs = _relocated_offset(old_abs, deltas)
            if new_abs == old_abs:
                continue
            front[position:position + 4] = struct.pack("<I", 0xC0000000 | new_abs)
            ptr_hits += 1
            ptr_rew += 1

    out = front + new_pool
    target = Path(path).resolve()
    expected = data if check_stale and target.exists() else None
    atomic_write_bytes(target, bytes(out), do_backup=do_backup, expected_bytes=expected)
    return ptr_hits, ptr_rew

# ------------------------- DAT(内嵌)：REPACK -------------------------
def save_repack_inline_dat(
    path: str,
    data: bytes,
    entries: List[Entry],
    *,
    do_backup: bool = False,
    check_stale: bool = True,
) -> Tuple[int, int]:
    changes: List[Tuple[int, bytes, bytes]] = []
    seen = set()
    for e in entries:
        if e.new_text == e.old_text:
            continue
        off = e.offset
        if off in seen:
            continue
        seen.add(off)
        prefer = _try_decode(e.old_bytes)[1]
        nb = _try_encode(e.new_text, prefer)
        changes.append((off, e.old_bytes, nb))
    changes.sort(key=lambda x: x[0])

    if not changes:
        target = Path(path).resolve()
        expected = data if check_stale and target.exists() else None
        atomic_write_bytes(target, data, do_backup=do_backup, expected_bytes=expected)
        return (0, 0)

    if data.startswith(b"#scp"):
        layout = parse_dat_references(
            data,
            tolerate_invalid_text=True,
            include_unreferenced_text=True,
        )
        rebuilt, _mapping = splice_referenced_strings(
            data,
            changes,
            [
                *layout.pointer_fields.values(),
                *layout.invalid_pointer_fields.values(),
            ],
            immutable_prefix_end=layout.strings_start,
        )
        target = Path(path).resolve()
        expected = data if check_stale and target.exists() else None
        atomic_write_bytes(
            target,
            rebuilt,
            do_backup=do_backup,
            expected_bytes=expected,
        )
        moved = sum(
            1
            for reference in layout.pointer_fields.values()
            if any(
                reference.target > offset and len(new) != len(old)
                for offset, old, new in changes
            )
        )
        return moved, moved

    out = bytearray()
    pos = 0
    deltas: List[Tuple[int, int]] = []
    for off, old_raw, new_bytes in changes:
        out += data[pos:off]
        out += new_bytes
        out.append(0)
        pos = off + len(old_raw) + 1
        deltas.append((off, len(new_bytes) - len(old_raw)))
    out += data[pos:]

    deltas.sort()
    pointer_fields = _discover_tagged_pointer_fields(
        data,
        entries,
        entry_base=0,
        field_end=len(data),
        target_start=0,
        require_string_boundary=True,
    )

    buf = bytearray(out)
    hits = rew = 0
    written_ranges: List[Tuple[int, int]] = []
    for old_position, old_target in pointer_fields:
        new_position = _relocated_offset(old_position, deltas)
        new_target = _relocated_offset(old_target, deltas)
        if new_target == old_target:
            continue
        if not 0 <= new_position <= len(buf) - 4:
            raise RuntimeError("REPACK failed: relocated tagged-pointer field is out of range.")
        if any(new_position < end and start < new_position + 4 for start, end in written_ranges):
            raise RuntimeError("REPACK failed: overlapping tagged-pointer candidates are ambiguous.")
        buf[new_position:new_position + 4] = struct.pack(
            "<I", 0xC0000000 | new_target
        )
        written_ranges.append((new_position, new_position + 4))
        hits += 1
        rew += 1

    target = Path(path).resolve()
    expected = data if check_stale and target.exists() else None
    atomic_write_bytes(target, bytes(buf), do_backup=do_backup, expected_bytes=expected)
    return hits, rew
