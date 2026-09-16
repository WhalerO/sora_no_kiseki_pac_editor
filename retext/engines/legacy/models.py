# models.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List

@dataclass
class Entry:
    index: int
    offset: int            # 相对 base 的偏移
    old_bytes: bytes
    old_text: str
    new_text: str

    @property
    def old_len(self) -> int:
        return len(self.old_bytes)

@dataclass
class Cluster:
    base: int             # 字符串池在文件中的基址(=start)
    start: int            # 池起点
    end: int              # 池终点（不含）
    inline: bool = False  # DAT 文本散布在主体中，而不是单一尾池

@dataclass
class PrecheckReport:
    msg: str
    slot_safe: bool
    repack_needed: bool
