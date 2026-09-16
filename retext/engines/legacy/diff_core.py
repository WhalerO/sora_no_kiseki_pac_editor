# diff_core.py
from __future__ import annotations
import os, glob, hashlib
from dataclasses import dataclass
from typing import List, Dict, Optional, Callable

try:
    from .core_engine import load_and_detect
except ImportError:  # pragma: no cover - compatibility for the legacy standalone UI
    from core_engine import load_and_detect

@dataclass
class DiffFile:
    """两组目录之间的单个文件对比结果（基于相对路径匹配）。"""
    rel: str
    old_path: Optional[str]
    new_path: Optional[str]
    old_size: int
    new_size: int
    status: str   # "same", "modified", "only_old", "only_new", "unknown"

@dataclass
class EntryDiff:
    """
    单文件内部的条目级 diff：
    - 对于 TBL/DAT：一条 Entry 视为一行；
    - 退化为文本：一行文本视为一行。
    """
    index_old: Optional[int]
    index_new: Optional[int]
    text_old: str
    text_new: str
    status: str   # "same","modified","added","deleted"

# -------------------- 文件层：构建索引 --------------------

def _split_globs(globs_str: str) -> List[str]:
    parts = [g.strip() for g in (globs_str or "").split(",") if g.strip()]
    return parts or ["*.tbl", "*.dat"]

def _gather_files_dict(roots: List[str], globs_str: str) -> Dict[str, str]:
    """
    将若干根目录下的文件收集为:  相对路径 -> 绝对路径  的字典。
    若出现重复相对路径，后者覆盖前者（你可以视为“最后一次选择生效”）。
    """
    result: Dict[str, str] = {}
    patterns = _split_globs(globs_str)
    for root in roots:
        root = root.strip()
        if not root:
            continue
        root_abs = os.path.abspath(root)
        for pat in patterns:
            # 递归匹配
            for fp in glob.glob(os.path.join(root_abs, "**", pat), recursive=True):
                if not os.path.isfile(fp):
                    continue
                rel = os.path.relpath(fp, root_abs).replace("\\", "/")
                result[rel] = fp
            # 当前目录一层匹配
            for fp in glob.glob(os.path.join(root_abs, pat)):
                if not os.path.isfile(fp):
                    continue
                rel = os.path.relpath(fp, root_abs).replace("\\", "/")
                result[rel] = fp
    return result

def _file_md5(path: str) -> Optional[str]:
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None

def build_file_diff_index(
    roots_old: List[str],
    roots_new: List[str],
    globs_str: str,
    progress_cb: Optional[Callable[[int, str], None]] = None
) -> List[DiffFile]:
    """
    核心：构建“文件级 diff 列表”。

    - roots_old / roots_new: 两组版本的根目录列表（各自允许有多个）
    - globs_str: 通配符，形如 "*.tbl,*.dat"
    - progress_cb: 可选进度回调 (pct, rel_path)
    """
    d_old = _gather_files_dict(roots_old, globs_str)
    d_new = _gather_files_dict(roots_new, globs_str)

    all_rels = sorted(set(d_old.keys()) | set(d_new.keys()))
    total = max(len(all_rels), 1)
    out: List[DiffFile] = []

    for i, rel in enumerate(all_rels):
        old_path = d_old.get(rel)
        new_path = d_new.get(rel)
        old_size = os.path.getsize(old_path) if old_path and os.path.exists(old_path) else 0
        new_size = os.path.getsize(new_path) if new_path and os.path.exists(new_path) else 0

        if old_path and new_path:
            # 两边都有：比较 MD5 判断是否修改
            md5_old = _file_md5(old_path)
            md5_new = _file_md5(new_path)
            if md5_old is not None and md5_new is not None and md5_old == md5_new:
                status = "same"
            else:
                status = "modified"
        elif old_path and not new_path:
            status = "only_old"
        elif new_path and not old_path:
            status = "only_new"
        else:
            status = "unknown"  # 理论上不会出现

        out.append(DiffFile(
            rel=rel,
            old_path=old_path,
            new_path=new_path,
            old_size=old_size,
            new_size=new_size,
            status=status,
        ))

        if progress_cb:
            pct = int((i + 1) / total * 100)
            progress_cb(pct, rel)

    return out

# -------------------- 文件内部：条目级 diff --------------------

def compute_entry_diff(
    old_path: Optional[str],
    new_path: Optional[str],
) -> List[EntryDiff]:
    """
    基于 core_engine.load_and_detect 的字符串池比对。
    若解析失败，则退化为整文件按行文本比对。

    注意：只读、不写文件。
    """
    # 只有旧版 / 新版存在时，生成一条“虚拟 diff”说明
    if old_path and not new_path:
        return [EntryDiff(
            index_old=None,
            index_new=None,
            text_old=f"[仅旧版本存在] {os.path.basename(old_path)}",
            text_new="",
            status="deleted",
        )]
    if new_path and not old_path:
        return [EntryDiff(
            index_old=None,
            index_new=None,
            text_old="",
            text_new=f"[仅新版本存在] {os.path.basename(new_path)}",
            status="added",
        )]
    if not old_path and not new_path:
        return []

    assert old_path and new_path

    # 优先：TBL / DAT 用字符串池解析
    ext = os.path.splitext(old_path)[1].lower()
    use_core = ext in (".tbl", ".dat")

    try:
        if use_core:
            data_old, cluster_old, entries_old, kind_old = load_and_detect(old_path, None)
            data_new, cluster_new, entries_new, kind_new = load_and_detect(new_path, None)
            if cluster_old and entries_old and cluster_new and entries_new:
                n = max(len(entries_old), len(entries_new))
                rows: List[EntryDiff] = []
                for i in range(n):
                    e_old = entries_old[i] if i < len(entries_old) else None
                    e_new = entries_new[i] if i < len(entries_new) else None
                    txt_old = e_old.old_text if e_old else ""
                    txt_new = e_new.old_text if e_new else ""
                    if e_old and e_new:
                        status = "same" if txt_old == txt_new else "modified"
                    elif e_old and not e_new:
                        status = "deleted"
                    else:
                        status = "added"
                    rows.append(EntryDiff(
                        index_old=e_old.index if e_old else None,
                        index_new=e_new.index if e_new else None,
                        text_old=txt_old,
                        text_new=txt_new,
                        status=status,
                    ))
                return rows
        # 走到这里：要么不是 tbl/dat，要么核心解析失败，退化为文本行 diff
    except Exception:
        # 把异常吞掉，退化到文本 diff
        pass

    # 退化方案：按行文本 diff（用于非 TBL/DAT 或解析失败的情况）
    def read_lines(p: str) -> List[str]:
        for enc in ("utf-8", "cp932", "shift_jis", "gbk", "latin1"):
            try:
                with open(p, "r", encoding=enc, errors="ignore") as f:
                    return f.read().splitlines()
            except Exception:
                continue
        return []

    lines_old = read_lines(old_path)
    lines_new = read_lines(new_path)
    n = max(len(lines_old), len(lines_new))
    rows: List[EntryDiff] = []
    for i in range(n):
        lo = lines_old[i] if i < len(lines_old) else ""
        ln = lines_new[i] if i < len(lines_new) else ""
        if i < len(lines_old) and i < len(lines_new):
            status = "same" if lo == ln else "modified"
        elif i < len(lines_old):
            status = "deleted"
        else:
            status = "added"
        rows.append(EntryDiff(
            index_old=i if i < len(lines_old) else None,
            index_new=i if i < len(lines_new) else None,
            text_old=lo,
            text_new=ln,
            status=status,
        ))
    return rows
