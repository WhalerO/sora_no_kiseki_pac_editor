from __future__ import annotations

import difflib
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Iterable, Literal


MAX_TEXT_BYTES = 16 * 1024 * 1024


@dataclass(slots=True)
class StringReference:
    field_offset: int
    target: int
    width: int
    tagged: bool = False
    category: str = "text"


@dataclass(slots=True)
class BinaryString:
    offset: int
    raw: bytes
    text: str
    encoding: str = "utf-8"
    references: list[StringReference] = field(default_factory=list)


@dataclass(slots=True)
class TblHeaderLayout:
    name: str
    start: int
    length: int
    count: int


@dataclass(slots=True)
class TblReferenceLayout:
    headers: list[TblHeaderLayout]
    records_end: int
    offset_fields: dict[int, int]
    inferred_text_fields: dict[int, int]


@dataclass(slots=True)
class DatReferenceLayout:
    strings_start: int
    pointer_fields: dict[int, StringReference]
    invalid_pointer_fields: dict[int, StringReference]
    strings: dict[int, BinaryString]
    function_starts: tuple[int, ...]


def read_cstring(
    payload: bytes,
    offset: int,
    *,
    encoding: str = "utf-8",
) -> tuple[bytes, str]:
    if not 0 <= offset < len(payload):
        raise ValueError(f"String offset is outside the payload: 0x{offset:X}")
    end = payload.find(b"\0", offset, min(len(payload), offset + MAX_TEXT_BYTES + 1))
    if end < 0:
        raise ValueError(f"Unterminated string at 0x{offset:X}")
    raw = payload[offset:end]
    return raw, raw.decode(encoding)


def canonical_string_targets(payload: bytes, targets: Iterable[int]) -> list[int]:
    """Collapse suffix aliases onto one editable C-string storage region.

    TBL fields may intentionally point into the middle of another referenced
    string.  Presenting both overlapping byte ranges as independent text units
    makes arbitrary edits contradictory.  Grouping by the shared terminator
    preserves every character (search/replace still sees the complete string)
    while all alias pointer fields remain in the relocation graph.
    """

    by_terminator: dict[int, int] = {}
    for target in sorted(set(targets)):
        raw, _text = read_cstring(payload, target)
        if not raw:
            continue
        terminator = target + len(raw)
        by_terminator[terminator] = min(
            target,
            by_terminator.get(terminator, target),
        )
    return sorted(set(by_terminator.values()))


def scan_printable_cstring_targets(
    payload: bytes,
    start: int,
    *,
    excluded_ranges: Iterable[tuple[int, int]] = (),
) -> list[int]:
    """Inventory physical UTF-8 C strings outside known binary ranges.

    Some Falcom tables retain labels that are not referenced by any live row
    (costume/debug names in ``t_name.tbl`` are a real example).  A pointer-only
    inventory silently omits them even though they remain editable string-pool
    content.  Scan NUL-delimited storage from the proven end of fixed records,
    while never interpreting schema-known external arrays as text.
    """

    if not 0 <= start <= len(payload):
        raise ValueError("TBL physical string scan starts outside the payload.")
    ranges = sorted(
        (max(start, left), min(len(payload), right))
        for left, right in excluded_ranges
        if left < right and right > start and left < len(payload)
    )
    output: list[int] = []
    cursor = start
    range_index = 0
    while cursor < len(payload):
        while range_index < len(ranges) and ranges[range_index][1] <= cursor:
            range_index += 1
        if (
            range_index < len(ranges)
            and ranges[range_index][0] <= cursor < ranges[range_index][1]
        ):
            cursor = ranges[range_index][1]
            continue
        limit = (
            ranges[range_index][0]
            if range_index < len(ranges)
            else len(payload)
        )
        terminator = payload.find(b"\0", cursor, limit)
        if terminator < 0:
            cursor = (
                ranges[range_index][1]
                if range_index < len(ranges)
                else len(payload)
            )
            continue
        raw = payload[cursor:terminator]
        if raw:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                if text.strip() and _looks_like_text_value(raw, text):
                    output.append(cursor)
        cursor = terminator + 1
    return output


def parse_tbl_headers(payload: bytes) -> tuple[list[TblHeaderLayout], int]:
    if len(payload) < 8 or payload[:4] != b"#TBL":
        raise ValueError("Payload is not an unwrapped #TBL file.")
    count = int.from_bytes(payload[4:8], "little")
    table_end = 8 + count * 0x50
    if count > 1_000_000 or table_end > len(payload):
        raise ValueError("TBL header table is outside the payload.")
    headers: list[TblHeaderLayout] = []
    for index in range(count):
        position = 8 + index * 0x50
        raw_name = payload[position : position + 64].split(b"\0", 1)[0]
        name = raw_name.decode("utf-8")
        start = int.from_bytes(payload[position + 68 : position + 72], "little")
        length = int.from_bytes(payload[position + 72 : position + 76], "little")
        row_count = int.from_bytes(payload[position + 76 : position + 80], "little")
        end = start + length * row_count
        if start < table_end or end < start or end > len(payload):
            raise ValueError(f"TBL header {name!r} has an invalid record range.")
        headers.append(TblHeaderLayout(name, start, length, row_count))
    records_end = max((item.start + item.length * item.count for item in headers), default=table_end)
    return headers, records_end


def _valid_utf8_target(payload: bytes, target: int, records_end: int) -> tuple[bytes, str] | None:
    if not records_end <= target < len(payload):
        return None
    try:
        return read_cstring(payload, target)
    except (UnicodeDecodeError, ValueError):
        return None


def _looks_like_text_value(raw: bytes, text: str) -> bool:
    """Accept every decodable C-string value, including short labels.

    Pointer discovery already requires a complete 64-bit record column whose
    targets all live in the external-data area.  Competing overlapping field
    interpretations are resolved at column level, while deliberate suffix
    aliases remain part of the concrete reference graph.  Applying additional
    language heuristics here used to hide legitimate values such as ``HP``,
    ``-`` and punctuation-only UI labels; worse, one such value suppressed the
    *whole* record column.  Text coverage therefore depends only on the actual
    storage contract.
    """

    if not raw:
        return True
    return all(
        character.isprintable() or character in "\t\n\r"
        for character in text
    )


def _text_target_score(payload: bytes, target: int, text: str) -> int:
    """Rank competing target interpretations without language filtering."""

    if not text:
        return 0
    score = 0
    if target == 0 or payload[target - 1] == 0:
        score += 8
    first = text[0]
    if first.isalnum():
        score += 6
    elif ord(first) > 0x7F and first.isprintable():
        score += 5
    elif first in "\t\n\r":
        score += 2
    if all(character.isprintable() or character in "\t\n\r" for character in text):
        score += 2
    return score


def discover_tbl_references(
    payload: bytes,
    *,
    known_fields: dict[int, int] | None = None,
    infer_record_ranges: list[tuple[int, int]] | None = None,
) -> TblReferenceLayout:
    """Discover fixed-record offsets using row-column consistency.

    Falcom TBL external references are 64-bit absolute offsets.  They are not
    guaranteed to be naturally aligned: several real schemas place a pointer
    after 8/16/32-bit scalar fields.  A real column points into the external-
    data area for every row.  Requiring that invariant, plus a valid UTF-8 target
    for inferred text columns, avoids treating pairs such as ``quest_id`` and
    ``chapter`` as offsets merely because their combined numeric value happens
    to fall inside the file.

    Schema-proven fields supplied by the caller always win.  Inference fills
    unknown headers and fields; it never replaces explicit metadata.
    """

    headers, records_end = parse_tbl_headers(payload)
    offset_fields = dict(known_fields or {})
    inferred_text_fields: dict[int, int] = {}
    occupied_field_bytes = {
        byte_offset
        for position in offset_fields
        for byte_offset in range(position, position + 8)
    }
    known_text_ranges = []
    for target in set(offset_fields.values()):
        decoded = _valid_utf8_target(payload, target, records_end)
        if decoded is not None and decoded[0]:
            known_text_ranges.append((target, target + len(decoded[0])))

    # Competing interpretations sometimes point at the same NUL terminator:
    # an unaligned scalar can accidentally address a suffix of a real string,
    # while a real pointer can deliberately skip a short binary prefix.  Keep
    # the complete column invariant and choose the interpretation whose target
    # starts look most like actual text starts; do not reject short or
    # punctuation-only strings when there is no competing interpretation.
    candidate_columns: list[
        tuple[TblHeaderLayout, int, list[int], list[tuple[bytes, str]], int]
    ] = []
    for header in headers:
        header_end = header.start + header.length * header.count
        if infer_record_ranges is not None and not any(
            start <= header.start and header_end <= end
            for start, end in infer_record_ranges
        ):
            continue
        if not header.count or header.length < 8:
            continue
        for relative in range(0, header.length - 7):
            positions = [
                header.start + row * header.length + relative
                for row in range(header.count)
            ]
            if any(
                any(byte_offset in occupied_field_bytes for byte_offset in range(position, position + 8))
                for position in positions
            ):
                continue
            targets = [
                int.from_bytes(payload[position : position + 8], "little")
                for position in positions
            ]
            decoded = [
                _valid_utf8_target(payload, target, records_end)
                for target in targets
            ]
            conflicts_with_known = any(
                target < known_end
                and known_start < target + len(item[0])
                for target, item in zip(targets, decoded)
                if item is not None and item[0]
                for known_start, known_end in known_text_ranges
            )
            if all(item is not None for item in decoded) and all(
                _looks_like_text_value(raw, text)
                for raw, text in (item for item in decoded if item is not None)
            ) and not conflicts_with_known:
                candidate_columns.append(
                    (
                        header,
                        relative,
                        targets,
                        [item for item in decoded if item is not None],
                        sum(
                            _text_target_score(payload, target, item[1])
                            for target, item in zip(targets, decoded)
                            if item is not None
                        ) + (8 * len(targets) if relative % 8 == 0 else 0),
                    )
                )

    rejected_columns: set[int] = set()
    for left_index, left in enumerate(candidate_columns):
        left_header, _left_relative, left_targets, left_decoded, left_score = left
        for right_index in range(left_index + 1, len(candidate_columns)):
            right = candidate_columns[right_index]
            right_header, _right_relative, right_targets, right_decoded, right_score = right
            if left_header is not right_header:
                continue
            conflicts = abs(left[1] - right[1]) < 8
            if not conflicts:
                continue
            if left_score == right_score:
                left_boundaries = sum(
                    target == 0 or payload[target - 1] == 0 for target in left_targets
                )
                right_boundaries = sum(
                    target == 0 or payload[target - 1] == 0 for target in right_targets
                )
                if left_boundaries == right_boundaries:
                    loser = right_index
                else:
                    loser = right_index if left_boundaries > right_boundaries else left_index
            else:
                loser = right_index if left_score > right_score else left_index
            rejected_columns.add(loser)

    for candidate_index, (header, relative, targets, _decoded, _score) in enumerate(candidate_columns):
        if candidate_index in rejected_columns:
            continue
        for row, target in enumerate(targets):
            position = header.start + row * header.length + relative
            if position in offset_fields:
                continue
            offset_fields[position] = target
            inferred_text_fields[position] = target

    # External arrays use the same 64-bit absolute offset followed by a
    # 32-bit element count.  Infer those columns separately so resizing an
    # earlier string also keeps unknown-schema array payloads reachable.
    referenced_ranges: list[tuple[int, int]] = []
    for target in set(offset_fields.values()):
        decoded = _valid_utf8_target(payload, target, records_end)
        if decoded is not None and decoded[0]:
            referenced_ranges.append((target, target + len(decoded[0])))
    occupied_field_bytes = {
        byte_offset
        for position in offset_fields
        for byte_offset in range(position, position + 8)
    }
    for header in headers:
        header_end = header.start + header.length * header.count
        if infer_record_ranges is not None and not any(
            start <= header.start and header_end <= end
            for start, end in infer_record_ranges
        ):
            continue
        if not header.count or header.length < 12:
            continue
        for relative in range(0, header.length - 11):
            positions = [
                header.start + row * header.length + relative
                for row in range(header.count)
            ]
            if any(
                any(byte_offset in occupied_field_bytes for byte_offset in range(position, position + 8))
                for position in positions
            ):
                continue
            targets = [
                int.from_bytes(payload[position : position + 8], "little")
                for position in positions
            ]
            counts = [
                int.from_bytes(payload[position + 8 : position + 12], "little")
                for position in positions
            ]
            if not all(records_end <= target < len(payload) for target in targets):
                continue
            if not any(count > 0 for count in counts):
                continue
            candidate_ends: list[list[int]] = []
            valid_arrays = True
            for target, count in zip(targets, counts):
                if count > 1_000_000:
                    valid_arrays = False
                    break
                ends = [
                    target + count * width
                    for width in (1, 2, 4, 8)
                    if target + count * width <= len(payload)
                ]
                if not ends:
                    valid_arrays = False
                    break
                candidate_ends.append(ends)
            if not valid_arrays:
                continue
            if any(
                any(
                    target != start
                    and target < end
                    and start < candidate_end
                    for start, end in referenced_ranges
                    for candidate_end in ends
                )
                for target, ends in zip(targets, candidate_ends)
            ):
                continue
            for position, target in zip(positions, targets):
                offset_fields[position] = target
                occupied_field_bytes.update(range(position, position + 8))

    # Some unknown layouts contain external objects whose element type/count is
    # stored elsewhere in the record.  They are not text, but their absolute
    # offsets must still follow a resized string pool.  Accept complete
    # external-offset columns after text/array fields have claimed their bytes;
    # natural alignment wins when two byte interpretations overlap.
    external_candidates: list[tuple[int, TblHeaderLayout, list[int], list[int]]] = []
    for header in headers:
        header_end = header.start + header.length * header.count
        if infer_record_ranges is not None and not any(
            start <= header.start and header_end <= end
            for start, end in infer_record_ranges
        ):
            continue
        if not header.count or header.length < 8:
            continue
        for relative in range(header.length - 7):
            positions = [
                header.start + row * header.length + relative
                for row in range(header.count)
            ]
            if any(
                any(
                    byte_offset in occupied_field_bytes
                    for byte_offset in range(position, position + 8)
                )
                for position in positions
            ):
                continue
            targets = [
                int.from_bytes(payload[position : position + 8], "little")
                for position in positions
            ]
            if all(records_end <= target <= len(payload) for target in targets):
                external_candidates.append(
                    (relative % 8 != 0, header, positions, targets)
                )
    for _unaligned, _header, positions, targets in sorted(
        external_candidates,
        key=lambda item: (item[0], item[1].start, item[1].length, item[2][0]),
    ):
        if any(
            any(
                byte_offset in occupied_field_bytes
                for byte_offset in range(position, position + 8)
            )
            for position in positions
        ):
            continue
        for position, target in zip(positions, targets):
            offset_fields[position] = target
            occupied_field_bytes.update(range(position, position + 8))

    return TblReferenceLayout(
        headers=headers,
        records_end=records_end,
        offset_fields=offset_fields,
        inferred_text_fields=inferred_text_fields,
    )


def _tagged_string_target(value: int, payload_size: int) -> int | None:
    if value & 0xC0000000 != 0xC0000000:
        return None
    target = value & 0x3FFFFFFF
    return target if 0 <= target < payload_size else None


def parse_dat_references(
    payload: bytes,
    *,
    tolerate_invalid_text: bool = False,
    include_unreferenced_text: bool = False,
) -> DatReferenceLayout:
    """Parse every structurally typed string pointer in an unwrapped #scp DAT.

    The parser deliberately records byte positions rather than re-emitting
    instructions.  Variable-length text edits can therefore resize only the
    terminal string pool while function bodies, instruction widths, jumps and
    all non-text operands remain byte-for-byte unchanged.
    """

    if len(payload) < 24 or payload[:4] != b"#scp":
        raise ValueError("Payload is not an unwrapped #scp DAT file.")
    functions_offset = int.from_bytes(payload[4:8], "little")
    function_count = int.from_bytes(payload[8:12], "little")
    script_vars_offset = int.from_bytes(payload[12:16], "little")
    script_vars_in = int.from_bytes(payload[16:20], "little")
    script_vars_out = int.from_bytes(payload[20:24], "little")
    function_headers_end = functions_offset + function_count * 0x20
    if (
        functions_offset < 24
        or function_count > 1_000_000
        or function_headers_end > len(payload)
        or script_vars_offset > len(payload)
    ):
        raise ValueError("DAT header contains an invalid section range.")

    references: dict[int, StringReference] = {}
    invalid_references: dict[int, StringReference] = {}
    physical_starts: dict[int, int] = {}

    def decode_target(target: int) -> tuple[bytes, str, int]:
        raw, text = read_cstring(payload, target)
        terminator = target + len(raw)
        storage_start = payload.rfind(b"\0", 0, target) + 1
        storage_raw = payload[storage_start:terminator]
        try:
            storage_text = storage_raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw, text, target
        if not _looks_like_text_value(storage_raw, storage_text):
            return raw, text, target
        return storage_raw, storage_text, storage_start

    def add_tagged(position: int, category: str) -> int | None:
        if not 0 <= position <= len(payload) - 4:
            raise ValueError("DAT string field is outside the payload.")
        value = int.from_bytes(payload[position : position + 4], "little")
        target = _tagged_string_target(value, len(payload))
        if target is None:
            return None
        try:
            _raw, _text, physical_start = decode_target(target)
        except (UnicodeDecodeError, ValueError) as exc:
            candidate = StringReference(position, target, 4, True, category)
            if tolerate_invalid_text:
                invalid_references[position] = candidate
                return None
            raise ValueError(
                f"DAT {category} string pointer at 0x{position:X} "
                f"targets invalid text at 0x{target:X}."
            ) from exc
        existing = references.get(position)
        candidate = StringReference(position, target, 4, True, category)
        if existing is not None and existing.target != target:
            raise ValueError("DAT string-field metadata is inconsistent.")
        references[position] = candidate
        physical_starts[position] = physical_start
        return target

    function_starts: list[int] = []
    for index in range(function_count):
        header = functions_offset + index * 0x20
        start = int.from_bytes(payload[header : header + 4], "little")
        packed_counts = payload[header + 4 : header + 8]
        input_count = packed_counts[0]
        output_count = packed_counts[3]
        output_offset = int.from_bytes(payload[header + 8 : header + 12], "little")
        input_offset = int.from_bytes(payload[header + 12 : header + 16], "little")
        struct_count = int.from_bytes(payload[header + 16 : header + 20], "little")
        structs_offset = int.from_bytes(payload[header + 20 : header + 24], "little")
        if not 0 <= start < len(payload):
            raise ValueError("DAT function start is outside the payload.")
        function_starts.append(start)
        add_tagged(header + 28, "function-name")
        if input_offset + input_count * 4 > len(payload):
            raise ValueError("DAT function input array is outside the payload.")
        if output_offset + output_count * 4 > len(payload):
            raise ValueError("DAT function output array is outside the payload.")
        for item in range(input_count):
            add_tagged(input_offset + item * 4, "function-input")
        for item in range(output_count):
            add_tagged(output_offset + item * 4, "function-output")
        if structs_offset + struct_count * 12 > len(payload):
            raise ValueError("DAT struct table is outside the payload.")
        for struct_index in range(struct_count):
            struct_offset = structs_offset + struct_index * 12
            pair_count = int.from_bytes(
                payload[struct_offset + 6 : struct_offset + 8], "little"
            )
            params_offset = int.from_bytes(
                payload[struct_offset + 8 : struct_offset + 12], "little"
            )
            value_count = pair_count * 2
            if params_offset + value_count * 4 > len(payload):
                raise ValueError("DAT struct parameter array is outside the payload.")
            for item in range(value_count):
                add_tagged(params_offset + item * 4, "struct")

    script_value_count = script_vars_in + script_vars_out
    if script_vars_offset + script_value_count * 8 > len(payload):
        raise ValueError("DAT script-variable table is outside the payload.")
    for item in range(script_value_count * 2):
        add_tagged(script_vars_offset + item * 4, "script-variable")

    if not references:
        raise ValueError("DAT contains no structurally typed string pointers.")
    strings_start = min(physical_starts.values())

    # Parse bytecode with its exact on-disk operand widths.  The final function
    # ends at the earliest string target, which can move earlier as PUSHSTRING
    # operands are encountered.
    ordered_starts = sorted(set(function_starts))
    for index, start in enumerate(ordered_starts):
        end = ordered_starts[index + 1] if index + 1 < len(ordered_starts) else strings_start
        position = start
        while position < end:
            opcode = payload[position]
            if opcode == 0:
                if position + 2 > len(payload):
                    raise ValueError("Truncated DAT PUSH instruction.")
                width = payload[position + 1]
                size = 2 + width
                if width == 4:
                    target = add_tagged(position + 2, "code")
                    if target is not None:
                        strings_start = min(
                            strings_start,
                            physical_starts[position + 2],
                        )
                        end = min(end, strings_start)
            elif opcode == 1:
                size = 2
            elif 2 <= opcode <= 8:
                size = 5
            elif opcode in {9, 10}:
                size = 2
            elif opcode in {11, 14, 15, 37}:
                size = 5
            elif opcode == 12:
                size = 3
            elif opcode == 13 or 16 <= opcode <= 33:
                size = 1
            elif opcode in {34, 35}:
                size = 10
                first = add_tagged(position + 1, "external-script")
                second = add_tagged(position + 5, "external-script")
                for field, target in (
                    (position + 1, first),
                    (position + 5, second),
                ):
                    if target is not None:
                        strings_start = min(strings_start, physical_starts[field])
                        end = min(end, strings_start)
            elif opcode == 36:
                size = 4
            elif opcode == 38:
                size = 3
            elif opcode == 39:
                size = 2
            elif opcode == 40:
                size = 5
            else:
                raise ValueError(f"Unsupported DAT opcode 0x{opcode:02X} at 0x{position:X}.")
            if size <= 0 or position + size > end:
                raise ValueError(f"DAT instruction at 0x{position:X} crosses its function boundary.")
            position += size
        if position != end:
            raise ValueError("DAT function does not end on an instruction boundary.")

    strings: dict[int, BinaryString] = {}
    if include_unreferenced_text:
        cursor = strings_start
        while cursor < len(payload):
            terminator = payload.find(b"\0", cursor)
            if terminator < 0:
                break
            raw = payload[cursor:terminator]
            if raw:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    pass
                else:
                    if _looks_like_text_value(raw, text):
                        strings[cursor] = BinaryString(cursor, raw, text)
            cursor = terminator + 1

    for position, reference in references.items():
        raw, text, physical_start = decode_target(reference.target)
        item = strings.setdefault(
            physical_start,
            BinaryString(physical_start, raw, text),
        )
        item.references.append(reference)
    return DatReferenceLayout(
        strings_start=strings_start,
        pointer_fields=references,
        invalid_pointer_fields=invalid_references,
        strings=strings,
        function_starts=tuple(sorted(function_starts)),
    )


def relocate_offset(offset: int, deltas: Iterable[tuple[int, int]]) -> int:
    return offset + sum(delta for start, delta in deltas if offset > start)


def _map_changed_string_boundary(old_raw: bytes, new_raw: bytes, index: int) -> int:
    """Map a byte boundary inside an edited string onto the new bytes."""

    if not 0 <= index <= len(old_raw):
        raise ValueError("String-alias boundary is outside the original text.")
    # Most typed references point to a slot's first byte; unchanged slots also
    # need no alignment. Do not run SequenceMatcher for these exact cases.
    if index == 0 or old_raw == new_raw:
        return index

    def map_index(old_value, new_value, old_index: int) -> int:
        matcher = difflib.SequenceMatcher(
            None,
            old_value,
            new_value,
            autojunk=False,
        )
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if old_index == old_start:
                return new_start
            if old_start < old_index < old_end:
                if tag == "equal":
                    return new_start + (old_index - old_start)
                return new_start + min(old_index - old_start, new_end - new_start)
            if old_index == old_end:
                return new_end
        return len(new_value)

    # UTF-8 is the native encoding for the modern TBL/DAT pools.  Mapping on
    # characters guarantees that a suffix alias can never be relocated into a
    # multibyte continuation byte.  The byte-level fallback retains support
    # for explicitly encoded legacy fields.
    try:
        old_text = old_raw.decode("utf-8")
        new_text = new_raw.decode("utf-8")
        old_character_index = len(old_raw[:index].decode("utf-8"))
    except UnicodeDecodeError:
        return map_index(old_raw, new_raw, index)
    new_character_index = map_index(
        old_text,
        new_text,
        old_character_index,
    )
    return len(new_text[:new_character_index].encode("utf-8"))


def _relocate_reference_target(
    target: int,
    changes: list[tuple[int, bytes, bytes]],
) -> int:
    delta_before = 0
    for start, old_raw, new_raw in changes:
        old_end = start + len(old_raw)
        if target < start:
            break
        if target <= old_end:
            return (
                start
                + delta_before
                + _map_changed_string_boundary(old_raw, new_raw, target - start)
            )
        delta_before += len(new_raw) - len(old_raw)
    return target + delta_before


def _nul_terminated_slots(payload: bytes, start: int) -> list[tuple[int, int, bytes]]:
    slots: list[tuple[int, int, bytes]] = []
    cursor = start
    while cursor < len(payload):
        terminator = payload.find(b"\0", cursor)
        if terminator < 0:
            raise ValueError("DAT string pool has an unterminated trailing slot.")
        slots.append((cursor, terminator, payload[cursor:terminator]))
        cursor = terminator + 1
    return slots


def _align_dat_reference_fields(
    reference_payload: bytes,
    reference: DatReferenceLayout,
    damaged_payload: bytes,
    damaged: DatReferenceLayout,
    stats: dict[str, int] | None = None,
) -> dict[int, int]:
    """Align typed fields across inserted/removed instruction bytes."""

    damaged_fields = {
        **damaged.pointer_fields,
        **damaged.invalid_pointer_fields,
    }
    damaged_strings_start = damaged.strings_start

    reference_prefix = bytearray(reference_payload[: reference.strings_start])
    damaged_prefix = bytearray(damaged_payload[:damaged_strings_start])
    for field, item in reference.pointer_fields.items():
        reference_prefix[field : field + item.width] = b"\0" * item.width
    for field, item in damaged_fields.items():
        damaged_prefix[field : field + item.width] = b"\0" * item.width
    left = bytes(reference_prefix)
    right = bytes(damaged_prefix)

    equal_blocks: list[tuple[int, int, int]] = []
    left_cursor = 0
    right_cursor = 0
    anchor_size = 24
    while left_cursor < len(left) and right_cursor < len(right):
        left_start = left_cursor
        right_start = right_cursor
        while (
            left_cursor < len(left)
            and right_cursor < len(right)
            and left[left_cursor] == right[right_cursor]
        ):
            left_cursor += 1
            right_cursor += 1
        if left_cursor > left_start:
            equal_blocks.append(
                (left_start, right_start, left_cursor - left_start)
            )
        if left_cursor >= len(left) or right_cursor >= len(right):
            break

        found: tuple[tuple[int, int], int, int] | None = None
        for window in (8192, 65536):
            left_stop = min(
                len(left) - anchor_size + 1,
                left_cursor + window,
            )
            anchors = {
                left[position : position + anchor_size]: position
                for position in range(left_cursor, max(left_cursor, left_stop))
            }
            right_stop = min(
                len(right) - anchor_size + 1,
                right_cursor + window,
            )
            for position in range(right_cursor, max(right_cursor, right_stop)):
                matched = anchors.get(right[position : position + anchor_size])
                if matched is None:
                    continue
                score = (
                    max(matched - left_cursor, position - right_cursor),
                    matched - left_cursor + position - right_cursor,
                )
                if found is None or score < found[0]:
                    found = (score, matched, position)
                if score[0] <= 32:
                    break
            if found is not None:
                break
        if found is None:
            break
        _score, left_cursor, right_cursor = found

    damaged_positions = sorted(damaged_fields)
    aligned: dict[int, int] = {}
    for left_start, right_start, length in equal_blocks:
        index = bisect_left(damaged_positions, right_start)
        while index < len(damaged_positions):
            damaged_field = damaged_positions[index]
            item = damaged_fields[damaged_field]
            if damaged_field + item.width > right_start + length:
                break
            reference_field = left_start + damaged_field - right_start
            reference_item = reference.pointer_fields.get(reference_field)
            if (
                reference_item is not None
                and reference_item.width == item.width
                and reference_item.category == item.category
            ):
                aligned[damaged_field] = reference_field
            index += 1
    byte_aligned_count = len(aligned)

    # A voice/script patch can replace a sizeable byte range inside one
    # function while retaining the same pointer-bearing instruction sequence.
    # Global byte anchors then stop on both sides of an otherwise unambiguous
    # field (the real mp2010_07.dat case left 0x46990 unresolved).  Recover such
    # interior gaps by function ordinal, but only when:
    #
    # * both files expose the same function count;
    # * the complete category sequence in this function is identical; and
    # * already byte-aligned fields on both sides prove the same ordinal delta.
    #
    # This is deliberately not a blind "Nth pointer maps to Nth pointer"
    # fallback.  Inserted/removed pointer-bearing instructions change the
    # category sequence or the surrounding ordinal anchors and remain
    # unresolved for the caller to reject safely.
    if len(reference.function_starts) == len(damaged.function_starts):
        reference_starts = list(reference.function_starts)
        damaged_starts = list(damaged.function_starts)
        for function_index in range(len(reference_starts)):
            reference_start = reference_starts[function_index]
            damaged_start = damaged_starts[function_index]
            reference_end = (
                reference_starts[function_index + 1]
                if function_index + 1 < len(reference_starts)
                else reference.strings_start
            )
            damaged_end = (
                damaged_starts[function_index + 1]
                if function_index + 1 < len(damaged_starts)
                else damaged_strings_start
            )
            reference_sequence = [
                (field, item)
                for field, item in sorted(reference.pointer_fields.items())
                if reference_start <= field < reference_end
            ]
            damaged_sequence = [
                (field, item)
                for field, item in sorted(damaged_fields.items())
                if damaged_start <= field < damaged_end
            ]
            if (
                len(reference_sequence) != len(damaged_sequence)
                or [item.category for _field, item in reference_sequence]
                != [item.category for _field, item in damaged_sequence]
            ):
                continue
            reference_ordinals = {
                field: ordinal
                for ordinal, (field, _item) in enumerate(reference_sequence)
            }
            anchors = sorted(
                (
                    damaged_ordinal,
                    reference_ordinals[aligned[damaged_field]],
                )
                for damaged_ordinal, (damaged_field, _item) in enumerate(
                    damaged_sequence
                )
                if damaged_field in aligned
                and aligned[damaged_field] in reference_ordinals
            )
            if len(anchors) < 2:
                continue
            anchor_ordinals = [item[0] for item in anchors]
            for damaged_ordinal, (damaged_field, damaged_item) in enumerate(
                damaged_sequence
            ):
                if damaged_field in aligned:
                    continue
                insertion = bisect_left(anchor_ordinals, damaged_ordinal)
                if insertion == 0 or insertion == len(anchors):
                    continue
                previous_damaged, previous_reference = anchors[insertion - 1]
                next_damaged, next_reference = anchors[insertion]
                previous_delta = previous_reference - previous_damaged
                next_delta = next_reference - next_damaged
                if previous_delta != next_delta:
                    continue
                reference_ordinal = damaged_ordinal + previous_delta
                if not 0 <= reference_ordinal < len(reference_sequence):
                    continue
                reference_field, reference_item = reference_sequence[
                    reference_ordinal
                ]
                if reference_item.category != damaged_item.category:
                    continue
                aligned[damaged_field] = reference_field
    if stats is not None:
        stats["byte_aligned_count"] = byte_aligned_count
        stats["function_recovered_count"] = len(aligned) - byte_aligned_count
        stats["total_field_count"] = len(damaged_fields)
    return aligned


def repair_dat_references_from_reference(
    reference_payload: bytes,
    damaged_payload: bytes,
    *,
    strategy: Literal["conservative", "aggressive"] = "conservative",
    diagnostics: dict[str, object] | None = None,
) -> tuple[bytes, int]:
    """Restore every DAT string field using a trusted pre-edit payload.

    A historical Legacy writer could relocate most tagged fields while leaving
    a small subset at their old offsets.  Merely accepting pointers that still
    decode is insufficient: a stale address can land on a different valid
    string and silently swap quest names or dialogue.  A trusted payload with
    the same instruction topology supplies the original field-to-string
    relation.  String slots are then mapped by pool order, so the damaged file's
    *current text* is preserved while all typed pointers are rebuilt.
    """

    if strategy not in {"conservative", "aggressive"}:
        raise ValueError(f"Unknown DAT reference-repair strategy: {strategy}")

    reference = parse_dat_references(reference_payload)
    damaged = parse_dat_references(
        damaged_payload,
        tolerate_invalid_text=True,
        include_unreferenced_text=diagnostics is not None,
    )
    damaged_fields = {
        **damaged.pointer_fields,
        **damaged.invalid_pointer_fields,
    }
    reference_slots = _nul_terminated_slots(
        reference_payload,
        reference.strings_start,
    )
    damaged_slots = _nul_terminated_slots(
        damaged_payload,
        damaged.strings_start,
    )
    if len(reference_slots) == len(damaged_slots):
        damaged_slot_by_reference = {
            index: index for index in range(len(reference_slots))
        }
    else:
        matcher = difflib.SequenceMatcher(
            None,
            [raw for _start, _end, raw in reference_slots],
            [raw for _start, _end, raw in damaged_slots],
            autojunk=False,
        )
        damaged_slot_by_reference: dict[int, int] = {}
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag == "equal" or (
                tag == "replace"
                and old_end - old_start == new_end - new_start
            ):
                for relative in range(old_end - old_start):
                    damaged_slot_by_reference[old_start + relative] = (
                        new_start + relative
                    )

    slot_starts = [start for start, _end, _raw in reference_slots]
    slot_index_by_target: dict[int, int] = {}
    for target in {item.target for item in reference.pointer_fields.values()}:
        index = bisect_right(slot_starts, target) - 1
        if index < 0 or target > reference_slots[index][1]:
            raise ValueError("Reference DAT pointer is outside its string pool.")
        slot_index_by_target[target] = index

    mapped_by_target: dict[int, int] = {}

    def map_reference_target(target: int) -> int:
        if target in mapped_by_target:
            return mapped_by_target[target]
        slot_index = slot_index_by_target.get(target)
        if slot_index is None:
            slot_index = bisect_right(slot_starts, target) - 1
            if slot_index < 0 or target > reference_slots[slot_index][1]:
                raise ValueError(
                    "Reference DAT target is outside its string pool."
                )
        damaged_slot_index = damaged_slot_by_reference.get(slot_index)
        if damaged_slot_index is None:
            raise ValueError("Reference DAT string slot was removed or reordered.")
        old_start, old_end, old_raw = reference_slots[slot_index]
        new_start, _new_end, new_raw = damaged_slots[damaged_slot_index]
        relative = target - old_start
        mapped_relative = _map_changed_string_boundary(
            old_raw,
            new_raw,
            relative,
        )
        mapped_by_target[target] = new_start + mapped_relative
        return mapped_by_target[target]

    reference_targets = set(slot_index_by_target)
    mapped_targets = {
        map_reference_target(target)
        for target in reference_targets
    }
    same_field_topology = (
        reference.function_starts == damaged.function_starts
        and set(reference.pointer_fields) == set(damaged_fields)
    )

    output = bytearray(damaged_payload)
    corrected = 0
    alignment_stats: dict[str, int] = {}
    ambiguous_fallback_fields: list[int] = []
    if same_field_topology:
        alignment_stats.update(
            byte_aligned_count=len(damaged_fields),
            function_recovered_count=0,
            total_field_count=len(damaged_fields),
        )
        candidates = [
            (field, item.target, map_reference_target(item.target))
            for field, item in reference.pointer_fields.items()
        ]
    else:
        aligned_fields = _align_dat_reference_fields(
            reference_payload,
            reference,
            damaged_payload,
            damaged,
            alignment_stats,
        )
        candidates = [
            (
                damaged_field,
                damaged_fields[damaged_field].target,
                map_reference_target(
                    reference.pointer_fields[reference_field].target
                ),
            )
            for damaged_field, reference_field in aligned_fields.items()
        ]
        unresolved_invalid: list[int] = []
        for field, item in damaged_fields.items():
            if field in aligned_fields:
                continue
            target = item.target
            if target in reference_targets and target in mapped_targets:
                # With a divergent string pool, a relocated address can
                # numerically equal the old address of a different string.
                # An unaligned field at that address has no structural
                # evidence telling us whether it is stale or already fixed.
                # Rewriting it is not idempotent: every repair pass can walk
                # it one more slot through the overlapping address sets.
                ambiguous_fallback_fields.append(field)
                continue
            if target in reference_targets:
                candidates.append(
                    (field, target, map_reference_target(target))
                )
            elif target in mapped_targets:
                continue
            elif field in damaged.invalid_pointer_fields:
                try:
                    mapped = map_reference_target(target)
                    read_cstring(damaged_payload, mapped)
                except (UnicodeDecodeError, ValueError):
                    unresolved_invalid.append(field)
                else:
                    candidates.append((field, target, mapped))
        if unresolved_invalid:
            fields = ", ".join(f"0x{field:X}" for field in unresolved_invalid[:8])
            raise ValueError(
                "Reference DAT cannot resolve invalid fields: " + fields
            )

    # A distant reference (for example the unmodified game script versus a
    # voice-injected script) can legitimately change the semantic target of a
    # still-valid pointer.  Treating every such difference as a silent error
    # would undo intentional additions.  Byte-level alignment coverage tells
    # us whether the reference is close enough for full semantic comparison.
    # For a divergent reference, repair every explicit invalid field and only
    # those neighbouring silent fields corroborated by the same relocation
    # delta inside the same function.  Uncorroborated valid differences are
    # reported but left untouched.
    total_fields = max(1, alignment_stats.get("total_field_count", 0))
    byte_aligned_count = alignment_stats.get("byte_aligned_count", 0)
    full_comparison = byte_aligned_count / total_fields >= 0.995

    unique_candidates: dict[int, tuple[int, int]] = {}
    for field, old_target, expected_target in candidates:
        existing = unique_candidates.get(field)
        if existing is not None and existing[1] != expected_target:
            raise ValueError(
                f"Reference DAT produced conflicting mappings for field 0x{field:X}."
            )
        unique_candidates[field] = (old_target, expected_target)

    def function_index(field: int) -> int:
        index = bisect_right(damaged.function_starts, field) - 1
        if index < 0:
            return -1
        end = (
            damaged.function_starts[index + 1]
            if index + 1 < len(damaged.function_starts)
            else damaged.strings_start
        )
        return index if field < end else -1

    invalid_evidence = [
        (
            function_index(field),
            expected_target - old_target,
            field,
        )
        for field, (old_target, expected_target) in unique_candidates.items()
        if field in damaged.invalid_pointer_fields
        and old_target != expected_target
    ]
    safe_candidates: dict[int, tuple[int, int]] = {}
    uncertain_mismatches = len(ambiguous_fallback_fields)
    aggressive_mismatches = 0
    for field, pair in unique_candidates.items():
        old_target, expected_target = pair
        if old_target == expected_target:
            safe_candidates[field] = pair
            continue
        if full_comparison or field in damaged.invalid_pointer_fields:
            safe_candidates[field] = pair
            continue
        field_function = function_index(field)
        delta = expected_target - old_target
        corroborated = any(
            evidence_function == field_function
            and evidence_delta == delta
            and abs(evidence_field - field) <= 0x80
            for evidence_function, evidence_delta, evidence_field in invalid_evidence
        )
        if corroborated:
            safe_candidates[field] = pair
        elif strategy == "aggressive":
            safe_candidates[field] = pair
            aggressive_mismatches += 1
        else:
            uncertain_mismatches += 1

    for field, (_old_target, expected_target) in safe_candidates.items():
        reference_item = damaged_fields[field]
        expected_value = expected_target | 0xC0000000
        current_value = int.from_bytes(
            damaged_payload[field : field + reference_item.width],
            "little",
        )
        if current_value == expected_value:
            continue
        output[field : field + reference_item.width] = expected_value.to_bytes(
            reference_item.width,
            "little",
        )
        corrected += 1

    repaired = bytes(output)
    if corrected or damaged.invalid_pointer_fields:
        parse_dat_references(repaired)
    if repaired[damaged.strings_start:] != damaged_payload[damaged.strings_start:]:
        raise RuntimeError("DAT reference repair changed current string-pool bytes.")
    if diagnostics is not None:
        diagnostics.update(
            alignment_stats,
            invalid_pointer_count_before=len(damaged.invalid_pointer_fields),
            text_count=sum(bool(item.raw) for item in damaged.strings.values()),
            comparison_mode=(
                "full"
                if full_comparison
                else "aggressive"
                if strategy == "aggressive"
                else "conservative"
            ),
            strategy=strategy,
            skipped_uncertain_pointer_count=uncertain_mismatches,
            ambiguous_pointer_count=len(ambiguous_fallback_fields),
            aggressive_pointer_count=aggressive_mismatches,
            corrected_pointer_count=corrected,
        )
    return repaired, corrected


def splice_referenced_strings(
    payload: bytes,
    changes: Iterable[tuple[int, bytes, bytes]],
    references: Iterable[StringReference],
    *,
    immutable_prefix_end: int,
) -> tuple[bytes, dict[int, int]]:
    """Resize C strings and relocate only the supplied concrete fields."""

    normalized = sorted(changes, key=lambda item: item[0])
    concrete_references = list(references)
    previous_end = immutable_prefix_end
    deltas: list[tuple[int, int]] = []
    output = bytearray()
    cursor = 0
    for offset, old_raw, new_raw in normalized:
        if offset < previous_end or offset + len(old_raw) >= len(payload):
            raise ValueError("Text changes overlap or fall outside the external string pool.")
        if payload[offset : offset + len(old_raw)] != old_raw:
            raise ValueError("Text change metadata is stale.")
        if payload[offset + len(old_raw)] != 0:
            raise ValueError("Text change does not end at a C-string terminator.")
        output += payload[cursor:offset]
        output += new_raw + b"\0"
        cursor = offset + len(old_raw) + 1
        previous_end = cursor
        deltas.append((offset, len(new_raw) - len(old_raw)))
    output += payload[cursor:]

    relocated_targets: dict[int, int] = {}
    last_written_end = -1
    for reference in sorted(concrete_references, key=lambda item: item.field_offset):
        new_field = relocate_offset(reference.field_offset, deltas)
        new_target = _relocate_reference_target(reference.target, normalized)
        relocated_targets[reference.target] = new_target
        if new_target == reference.target and new_field == reference.field_offset:
            continue
        if not 0 <= new_field <= len(output) - reference.width:
            raise ValueError("Relocated reference field is outside the payload.")
        if new_field < last_written_end:
            raise ValueError("Relocated reference fields overlap.")
        value = new_target | (0xC0000000 if reference.tagged else 0)
        output[new_field : new_field + reference.width] = value.to_bytes(
            reference.width, "little"
        )
        last_written_end = new_field + reference.width

    allowed = bytearray(immutable_prefix_end)
    for reference in concrete_references:
        if reference.field_offset < immutable_prefix_end:
            end = min(immutable_prefix_end, reference.field_offset + reference.width)
            allowed[reference.field_offset:end] = b"\x01" * (end - reference.field_offset)
    if any(
        payload[index] != output[index] and not allowed[index]
        for index in range(min(immutable_prefix_end, len(payload), len(output)))
    ):
        raise RuntimeError("String relocation changed a non-reference byte in the fixed prefix.")
    return bytes(output), relocated_targets
