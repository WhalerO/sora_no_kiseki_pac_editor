"""Original synthetic fixtures; no game payload, artwork or dialogue."""
from __future__ import annotations

import struct
import zlib

DEMO_TEXTS = (
    "演示文本：欢迎使用文本编辑器。",
    "演示角色甲与演示角色乙一起出发。",
    "演示角色甲，请先备份，再修改文本。",
    "这是一条用于预览、查找和变长保存的合成记录。",
)


def make_tbl(texts: tuple[str, ...] = DEMO_TEXTS) -> bytes:
    """#TBL with 24-byte BooksTitle records and exact text pointers."""
    payload = bytearray(88 + 24 * len(texts))
    payload[:4] = b"#TBL"
    struct.pack_into("<I", payload, 4, 1)
    payload[8:18] = b"BooksTitle"
    struct.pack_into("<III", payload, 76, 88, 24, len(texts))
    for index, text in enumerate(texts):
        struct.pack_into("<HIHQHHI", payload, 88 + 24 * index,
                         index + 1, 0, 0, len(payload), 0, 0, 0)
        payload.extend(text.encode("utf-8") + b"\0")
    return bytes(payload)


def make_dat(texts: tuple[str, ...] = DEMO_TEXTS) -> bytes:
    """#scp function with PUSHSTRING operands and RETURN."""
    function_start = 56
    strings_start = function_start + 6 * len(texts) + 1
    payload = bytearray(strings_start)
    payload[:4] = b"#scp"
    struct.pack_into("<III", payload, 4, 24, 1, function_start)
    for offset in (24, 32, 36, 44):
        struct.pack_into("<I", payload, offset, function_start)
    struct.pack_into("<I", payload, 52, 0xC0000000 | strings_start)
    payload.extend(b"demo_function\0")
    for index, text in enumerate(texts):
        position = function_start + index * 6
        payload[position:position + 2] = b"\0\x04"
        struct.pack_into("<I", payload, position + 2, 0xC0000000 | len(payload))
        payload.extend(text.encode("utf-8") + b"\0")
    payload[strings_start - 1] = 13
    return bytes(payload)


def make_pac(items: list[tuple[str, bytes]]) -> bytes:
    """Tiny FPAC v1 for demonstration, ordered by path CRC."""
    table_size = 16 + len(items) * 32
    names = bytearray()
    records = []
    for name, data in items:
        raw = name.encode("utf-8")
        records.append([zlib.crc32(raw) ^ 0xFFFFFFFF, 0,
                        table_size + len(names), len(data), 0])
        names.extend(raw + b"\0")
    header_size = table_size + len(names)
    offset = header_size
    for record in records:
        record[4] = offset
        offset += record[3]
    return (struct.pack("<4sIII", b"FPAC", len(items), header_size, 1)
            + b"".join(struct.pack("<IIQQQ", *r) for r in sorted(records))
            + names + b"".join(data for _, data in items))
