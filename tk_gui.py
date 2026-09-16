from __future__ import annotations

import ctypes
import fnmatch
import json
import os
import queue
import threading
import time
import tkinter as tk
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

from PIL import Image, ImageTk

from retext.archive import (
    DatReferenceRepairService,
    PacDatReferenceRepairService,
    PacFallbackTools,
    PacComparisonSession,
    PacDiffFileRow,
    PacMaterializedEntry,
    PacNodeRef,
    PacWorkbench,
    PacWorkspaceIntegrityError,
    PacWorkspaceManager,
)
from retext.business import (
    BatchMapping,
    BusinessFileTarget,
    WorkspaceBusiness,
    apply_batch_behavior,
    load_batch_behavior,
    parse_mapping_lines,
    serialize_batch_behavior,
)
from retext.domain import GameVersion, WorkflowMode, is_text_document_path
from retext.io_utils import atomic_copy_file
from retext.paths import cleanup_runtime_dir, create_runtime_dir
from retext.model3d import (
    ModelRenderCancelled,
    calculate_render_dimensions,
    companion_model_entry,
    companion_model_path,
    model_3d_service,
)
from retext.playback import (
    MediaPlaybackController,
    PlaybackError,
    PlaybackState,
)
from retext.preview import (
    AssetPreviewService,
    FontGlyph,
    MEDIA_SUFFIXES,
    MediaPreview,
    infer_font_atlas_entry,
    infer_font_atlas_pac_name,
    infer_font_atlas_path,
    infer_image_pac_name,
    infer_model_identity_table_paths,
    infer_model_texture_entry,
    infer_model_texture_path,
    render_font_glyph,
)
from retext.runtime import cleanup_runtime, describe_runtime, list_runtime_entries
from retext.session import DocumentSession, SessionOptions
from retext.version import __version__
from retext.ui_theme import apply_theme
from retext.dpi import configure_dpi_awareness, window_tk_scaling
from retext.adaptive_table import AdaptiveTextTable
from retext.diff_cells import DiffTextCells
from retext.text_presentation import excerpt_parts, find_text_span, resolve_hit_span
from retext.batch_display import HIT_STYLES, hit_presentation, hit_row_presentation
from retext.compact_toolbar import CompactToolbar
from retext.pac_files_view import PacFilesView
from retext.text_search import search_text_targets, search_pac_targets


_SEARCH_BOUNDARY_CHARS = " \t\r\n\v\f\u00a0\u200b\u2060\ufeff"


def _apply_process_dpi_awareness() -> None:
    """Declare DPI awareness before Tk creates its first native window."""
    configure_dpi_awareness()


def _clean_search_input(value: str) -> str:
    """Normalize user-entered search text without folding CJK variants."""

    return unicodedata.normalize("NFC", value.strip(_SEARCH_BOUNDARY_CHARS))


@dataclass(slots=True, frozen=True)
class PreviewFileSelection:
    origin: str
    logical_path: str
    size: int
    pac_ref: PacNodeRef | None = None
    file_path: Path | None = None


@dataclass(slots=True, frozen=True)
class ModelRenderRequest:
    generation: int
    preview: MediaPreview
    display_width: int
    display_height: int
    render_width: int
    render_height: int
    yaw: float
    pitch: float
    zoom: float
    pan_x: float
    pan_y: float
    wireframe: bool
    low_quality: bool
    animation_seconds: float


class ModernScale(tk.Canvas):
    """Compact horizontal slider with a round thumb and variable binding."""

    def __init__(
        self,
        master,
        *,
        variable: tk.DoubleVar,
        from_: float = 0.0,
        to: float = 1.0,
        command=None,
        width: int = 160,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=24,
            bg="#fbf6ed",
            bd=0,
            highlightthickness=1,
            highlightbackground="#fbf6ed",
            highlightcolor="#9b6a43",
            cursor="hand2",
            takefocus=True,
        )
        self.variable = variable
        self.minimum = float(from_)
        self.maximum = float(to)
        self.command = command
        self._hover = False
        self._trace_id = self.variable.trace_add("write", self._on_variable_changed)
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Button-1>", self._on_pointer, add="+")
        self.bind("<B1-Motion>", self._on_pointer, add="+")
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self.bind("<FocusIn>", self._on_configure, add="+")
        self.bind("<FocusOut>", self._on_configure, add="+")
        self.bind("<Left>", lambda _event: self._nudge(-1), add="+")
        self.bind("<Right>", lambda _event: self._nudge(1), add="+")
        self.after_idle(self._redraw)

    def set_range(
        self,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> None:
        if minimum is not None:
            self.minimum = float(minimum)
        if maximum is not None:
            self.maximum = float(maximum)
        if self.maximum < self.minimum:
            self.maximum = self.minimum
        current = self._clamp(self.variable.get())
        if current != self.variable.get():
            self.variable.set(current)
        else:
            self._redraw()

    def _on_configure(self, _event=None) -> None:
        self._redraw()

    def _on_variable_changed(self, *_args) -> None:
        self._redraw()

    def _on_pointer(self, event) -> None:
        self.focus_set()
        width = max(self.winfo_width(), 1)
        start = 10.0
        end = max(start, width - 10.0)
        ratio = min(1.0, max(0.0, (event.x - start) / max(end - start, 1.0)))
        value = self.minimum + ratio * (self.maximum - self.minimum)
        self._set_user_value(value)

    def _nudge(self, direction: int) -> str:
        span = max(self.maximum - self.minimum, 0.0)
        self._set_user_value(self.variable.get() + direction * span / 100.0)
        return "break"

    def _set_user_value(self, value: float) -> None:
        applied = self._clamp(value)
        self.variable.set(applied)
        if self.command is not None:
            self.command(str(applied))

    def _clamp(self, value: float) -> float:
        return min(self.maximum, max(self.minimum, float(value)))

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._redraw()

    def _redraw(self) -> None:
        if not self.winfo_exists():
            return
        self.delete("slider")
        width = max(self.winfo_width(), 24)
        height = max(self.winfo_height(), 20)
        start = 10.0
        end = max(start, width - 10.0)
        center = height / 2.0
        span = self.maximum - self.minimum
        ratio = (
            (self._clamp(self.variable.get()) - self.minimum) / span
            if span > 0
            else 0.0
        )
        thumb_x = start + ratio * (end - start)
        self.create_line(
            start,
            center,
            end,
            center,
            fill="#d5c7b5",
            width=5,
            capstyle=tk.ROUND,
            tags="slider",
        )
        self.create_line(
            start,
            center,
            thumb_x,
            center,
            fill="#a56e42",
            width=5,
            capstyle=tk.ROUND,
            tags="slider",
        )
        radius = 7 if self._hover or self.focus_get() == self else 6
        self.create_oval(
            thumb_x - radius,
            center - radius,
            thumb_x + radius,
            center + radius,
            fill="#7f5b3d",
            outline="#fffdf9",
            width=2,
            tags="slider",
        )

    def destroy(self) -> None:
        try:
            self.variable.trace_remove("write", self._trace_id)
        except tk.TclError:
            pass
        super().destroy()


class MappingTable(ttk.Frame):
    """Excel-like in-place mapping editor shared by both resource modes."""

    def __init__(self, master, checkbox_images: dict[bool, tk.PhotoImage]) -> None:
        super().__init__(master, style="Card.TFrame")
        self.checkbox_images = checkbox_images
        self.editor: ttk.Entry | None = None
        self.editor_item: str | None = None
        self.editor_column: str | None = None
        ttk.Label(self, text="替换映射", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            self,
            text="按上到下顺序替换；双击编辑，Alt+↑/↓ 调序。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        body = ttk.Frame(self, style="Card.TFrame")
        body.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = ttk.Treeview(
            body,
            columns=("old", "new", "full_match"),
            show="tree headings",
            height=8,
            selectmode="extended",
        )
        self.tree.heading("#0", text="启用")
        toggle_width = tkfont.nametofont("TkDefaultFont").measure("启用") + 20
        self.tree.column("#0", width=toggle_width, minwidth=toggle_width, stretch=False, anchor="center")
        self.tree.heading("old", text="旧文本")
        self.tree.heading("new", text="新文本")
        self.tree.heading("full_match", text="完全匹配")
        self.tree.column("old", width=120, minwidth=80, stretch=True)
        self.tree.column("new", width=120, minwidth=80, stretch=True)
        self.tree.column(
            "full_match",
            width=tkfont.nametofont("TkDefaultFont").measure("完全匹配") + 20,
            minwidth=tkfont.nametofont("TkDefaultFont").measure("完全匹配") + 20,
            stretch=False,
            anchor="center",
        )
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<F2>", lambda _event: self.edit())
        self.tree.bind("<Return>", lambda _event: self.edit())

        actions = CompactToolbar(self, style="Card.TFrame")
        actions.pack(side="bottom", fill="x", pady=(8, 0), before=body)
        for label, command in (("新增", self.add), ("删除", self.delete), ("上移", lambda: self.move_rows(-1)),
                               ("下移", lambda: self.move_rows(1)), ("导入", self.load_rows), ("导出", self.save_rows)):
            actions.add(label, command)
        self.tree.bind("<Alt-Up>", lambda _e: self.move_rows(-1))
        self.tree.bind("<Alt-Down>", lambda _e: self.move_rows(1))

    def move_rows(self, direction: int) -> str:
        self._close_editor(save=True)
        selected = set(self.tree.selection())
        rows = list(self.tree.get_children())
        for item in (rows if direction < 0 else list(reversed(rows))):
            if item not in selected:
                continue
            index = rows.index(item)
            neighbor = index + (-1 if direction < 0 else 1)
            if 0 <= neighbor < len(rows) and rows[neighbor] not in selected:
                rows[index], rows[neighbor] = rows[neighbor], rows[index]
                self.tree.move(item, "", neighbor)
        if selected:
            self.tree.see(next(item for item in rows if item in selected))
        return "break"

    def add(self) -> None:
        self._close_editor(save=True)
        item = self._insert(True, "", "", False)
        self.tree.selection_set(item)
        self.tree.focus(item)
        self.tree.see(item)
        self.after_idle(lambda: self._begin_edit(item, "#1"))

    def edit(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self._begin_edit(selection[0], "#1")

    def toggle(self) -> None:
        self._close_editor(save=True)
        for item in self.tree.selection():
            self._set_checked(item, not self._checked(item))

    def toggle_full_match(self) -> None:
        self._close_editor(save=True)
        for item in self.tree.selection():
            self._set_full_match(item, not self._full_match(item))

    def delete(self) -> None:
        self._close_editor(save=False)
        for item in self.tree.selection():
            self.tree.delete(item)

    def get_rows(self) -> list[dict[str, object]]:
        self._close_editor(save=True)
        rows: list[dict[str, object]] = []
        for item in self.tree.get_children():
            old, new, _full_match = self.tree.item(item, "values")
            rows.append(
                {
                    "enabled": self._checked(item),
                    "old": str(old),
                    "new": str(new),
                    "full_match": self._full_match(item),
                }
            )
        return rows

    def get_pairs(self) -> list[BatchMapping]:
        pairs: list[BatchMapping] = []
        for row in self.get_rows():
            old = _clean_search_input(str(row["old"]))
            if bool(row["enabled"]) and old:
                pairs.append(
                    BatchMapping(
                        old=old,
                        new=str(row["new"]),
                        full_match=bool(row["full_match"]),
                    )
                )
        return pairs

    def save_rows(self) -> None:
        target = filedialog.asksaveasfilename(
            title="导出映射表",
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if target:
            Path(target).write_text(
                json.dumps(
                    {
                        "format": "tis-retext-mapping",
                        "version": 2,
                        "mappings": self.get_rows(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    def load_rows(self) -> None:
        target = filedialog.askopenfilename(
            title="导入映射表",
            filetypes=[
                ("映射文件", "*.json *.txt"),
                ("JSON 文件", "*.json"),
                ("文本文件", "*.txt"),
                ("所有文件", "*.*"),
            ],
        )
        if not target:
            return
        self._close_editor(save=False)
        try:
            path = Path(target)
            if path.suffix.lower() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                raw_rows = (
                    payload.get("mappings", [])
                    if isinstance(payload, dict)
                    else payload
                )
                if not isinstance(raw_rows, list):
                    raise ValueError("映射文件缺少 mappings 数组。")
                rows = [
                    {
                        "enabled": bool(row.get("enabled", True)),
                        "old": str(row.get("old", "")),
                        "new": str(row.get("new", "")),
                        "full_match": bool(row.get("full_match", False)),
                    }
                    for row in raw_rows
                    if isinstance(row, dict)
                ]
            else:
                rows = [
                    {
                        "enabled": True,
                        "old": old,
                        "new": new,
                        "full_match": False,
                    }
                    for old, new in parse_mapping_lines(path.read_text(encoding="utf-8"))
                ]
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc), parent=self)
            return
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            self._insert(
                bool(row["enabled"]),
                str(row["old"]),
                str(row["new"]),
                bool(row["full_match"]),
            )

    def _insert(
        self,
        checked: bool,
        old: str,
        new: str,
        full_match: bool = False,
    ) -> str:
        item = self.tree.insert(
            "",
            "end",
            text="",
            image=self.checkbox_images[checked],
            values=(old, new, "是" if full_match else "否"),
        )
        self._set_checked(item, checked)
        return item

    def _on_click(self, event):
        item = self.tree.identify_row(event.y)
        if item and self.tree.identify_column(event.x) == "#0":
            self._close_editor(save=True)
            self._set_checked(item, not self._checked(item))
            self.tree.selection_set(item)
            return "break"
        if item and self.tree.identify_column(event.x) == "#3":
            self._close_editor(save=True)
            self._set_full_match(item, not self._full_match(item))
            self.tree.selection_set(item)
            self.tree.focus(item)
            return "break"
        return None

    def _on_double_click(self, event) -> str | None:
        item = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if item and column in {"#1", "#2"}:
            self._begin_edit(item, column)
            return "break"
        return None

    def _begin_edit(self, item: str, column: str) -> None:
        self._close_editor(save=True)
        bounds = self.tree.bbox(item, column)
        if not bounds:
            return
        x, y, width, height = bounds
        values = list(self.tree.item(item, "values"))
        index = 0 if column == "#1" else 1
        self.editor_item = item
        self.editor_column = column
        self.editor = ttk.Entry(self.tree)
        self.editor.insert(0, str(values[index]))
        self.editor.place(x=x, y=y, width=width, height=height)
        self.editor.focus_set()
        self.editor.selection_range(0, "end")
        self.editor.bind("<Return>", lambda _event: self._close_editor(save=True))
        self.editor.bind("<Escape>", lambda _event: self._close_editor(save=False))
        self.editor.bind("<FocusOut>", lambda _event: self._close_editor(save=True))
        self.editor.bind("<Tab>", self._move_editor)

    def _move_editor(self, _event) -> str:
        item = self.editor_item
        column = self.editor_column
        self._close_editor(save=True)
        if item is None:
            return "break"
        if column == "#1":
            self._begin_edit(item, "#2")
            return "break"
        next_item = self.tree.next(item)
        if not next_item:
            next_item = self._insert(True, "", "", False)
        self.tree.selection_set(next_item)
        self.tree.focus(next_item)
        self.tree.see(next_item)
        self._begin_edit(next_item, "#1")
        return "break"

    def _close_editor(self, *, save: bool) -> None:
        editor = self.editor
        item = self.editor_item
        column = self.editor_column
        if editor is None or item is None or column is None:
            return
        value = editor.get()
        self.editor = None
        self.editor_item = None
        self.editor_column = None
        if save and self.tree.exists(item):
            values = list(self.tree.item(item, "values"))
            values[0 if column == "#1" else 1] = value
            self.tree.item(item, values=tuple(values))
        editor.destroy()

    def _set_checked(self, item: str, checked: bool) -> None:
        self.tree.item(
            item,
            image=self.checkbox_images[checked],
            tags=("checked" if checked else "unchecked",),
        )

    def _checked(self, item: str) -> bool:
        return "checked" in self.tree.item(item, "tags")

    def _set_full_match(self, item: str, full_match: bool) -> None:
        values = list(self.tree.item(item, "values"))
        while len(values) < 3:
            values.append("否")
        values[2] = "是" if full_match else "否"
        self.tree.item(item, values=tuple(values))

    def _full_match(self, item: str) -> bool:
        values = self.tree.item(item, "values")
        return len(values) >= 3 and str(values[2]) == "是"


class PacNodeSelectionEditor(ttk.Frame):
    """Stores PAC tree selections without exposing cache filesystem paths."""

    def __init__(
        self,
        master,
        title: str,
        selection_provider,
        label_provider,
        identity_provider=None,
    ) -> None:
        super().__init__(master, style="Card.TFrame")
        self.selection_provider = selection_provider
        self.label_provider = label_provider
        self.identity_provider = identity_provider or (lambda ref: ref.key)
        self.refs: list[PacNodeRef] = []
        ttk.Label(self, text=title, style="Section.TLabel").pack(anchor="w")
        body = ttk.Frame(self, style="Card.TFrame")
        body.pack(fill="both", expand=True, pady=(6, 0))
        self.listbox = tk.Listbox(
            body,
            height=4,
            selectmode="extended",
            bg="#fffdf9",
            fg="#30271f",
        )
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        actions = ttk.Frame(self, style="Card.TFrame")
        actions.pack(fill="x", pady=(8, 0))
        actions.columnconfigure(0, weight=1, uniform="pac_scope_actions")
        actions.columnconfigure(1, weight=1, uniform="pac_scope_actions")
        ttk.Button(
            actions,
            text="添加左侧所选范围",
            command=self.add_current_selection,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Button(actions, text="移除所选", command=self.remove_selected).grid(
            row=1, column=0, sticky="ew", padx=(0, 3)
        )
        ttk.Button(actions, text="清空", command=self.clear).grid(
            row=1, column=1, sticky="ew", padx=(3, 0)
        )

    def add_current_selection(self) -> None:
        selected = list(self.selection_provider())
        if not selected:
            messagebox.showinfo("尚未选择", "请先在左侧 PAC 文件树中选择节点或文件。")
            return
        existing = {self.identity_provider(ref) for ref in self.refs}
        for ref in selected:
            identity = self.identity_provider(ref)
            if identity not in existing:
                self.refs.append(ref)
                existing.add(identity)
        self._refresh()

    def remove_selected(self) -> None:
        removed = set(self.listbox.curselection())
        self.refs = [ref for index, ref in enumerate(self.refs) if index not in removed]
        self._refresh()

    def clear(self) -> None:
        self.refs.clear()
        self._refresh()

    def get_refs(self) -> list[PacNodeRef]:
        return list(self.refs)

    def prune(self, workspace_ids: set[str]) -> None:
        self.refs = [ref for ref in self.refs if ref.workspace_id in workspace_ids]
        self._refresh()

    def _refresh(self) -> None:
        self.listbox.delete(0, "end")
        for ref in self.refs:
            self.listbox.insert("end", self.label_provider(ref))


class PathScopeSelectionEditor(ttk.Frame):
    """Stores paths selected from the unpacked-mode source tree."""

    def __init__(self, master, title: str, selection_provider) -> None:
        super().__init__(master, style="Card.TFrame")
        self.selection_provider = selection_provider
        self.paths: list[Path] = []
        ttk.Label(self, text=title, style="Section.TLabel").pack(anchor="w")
        body = ttk.Frame(self, style="Card.TFrame")
        body.pack(fill="both", expand=True, pady=(6, 0))
        self.listbox = tk.Listbox(
            body,
            height=6,
            selectmode="extended",
            bg="#fffdf9",
            fg="#30271f",
        )
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        actions = ttk.Frame(self, style="Card.TFrame")
        actions.pack(fill="x", pady=(8, 0))
        actions.columnconfigure(0, weight=1, uniform="path_scope_actions")
        actions.columnconfigure(1, weight=1, uniform="path_scope_actions")
        ttk.Button(
            actions,
            text="添加左侧当前选择",
            command=self.add_current_selection,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Button(actions, text="移除所选", command=self.remove_selected).grid(
            row=1, column=0, sticky="ew", padx=(0, 3)
        )
        ttk.Button(actions, text="清空", command=self.clear).grid(
            row=1, column=1, sticky="ew", padx=(3, 0)
        )

    def add_current_selection(self) -> None:
        selected = [Path(path).resolve() for path in self.selection_provider()]
        if not selected:
            messagebox.showinfo("尚未选择", "请先在左侧解包文件树中选择文件或目录。")
            return
        existing = set(self.paths)
        for path in selected:
            if path not in existing:
                self.paths.append(path)
                existing.add(path)
        self._refresh()

    def remove_selected(self) -> None:
        removed = set(self.listbox.curselection())
        self.paths = [
            path for index, path in enumerate(self.paths) if index not in removed
        ]
        self._refresh()

    def clear(self) -> None:
        self.paths.clear()
        self._refresh()

    def get_paths(self) -> list[str]:
        return [str(path) for path in self.paths if path.exists()]

    def _refresh(self) -> None:
        self.listbox.delete(0, "end")
        for path in self.paths:
            self.listbox.insert("end", str(path))


class ComparisonSourceEditor(ttk.Frame):
    """Old/new source selector shared by PAC and unpacked comparison modes."""

    def __init__(self, master, title: str, mode_provider) -> None:
        super().__init__(master, style="Card.TFrame")
        self.mode_provider = mode_provider
        self.paths: list[Path] = []
        ttk.Label(self, text=title, style="Section.TLabel").pack(anchor="w")
        body = ttk.Frame(self, style="Card.TFrame")
        body.pack(fill="both", expand=True, pady=(6, 0))
        self.listbox = tk.Listbox(
            body,
            height=5,
            selectmode="extended",
            bg="#fffdf9",
            fg="#30271f",
        )
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        actions = ttk.Frame(self, style="Card.TFrame")
        actions.pack(fill="x", pady=(7, 0))
        actions.columnconfigure(0, weight=1, uniform="comparison_source_actions")
        actions.columnconfigure(1, weight=1, uniform="comparison_source_actions")
        self.add_files_button = ttk.Button(
            actions,
            text="添加 PAC",
            command=self.add_files,
        )
        self.add_files_button.grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4)
        )
        self.add_folder_button = ttk.Button(
            actions,
            text="添加 PAC 组目录",
            command=self.add_folder,
        )
        self.add_folder_button.grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(0, 4)
        )
        ttk.Button(actions, text="移除", command=self.remove_selected).grid(
            row=2, column=0, sticky="ew", padx=(0, 3)
        )
        ttk.Button(actions, text="清空", command=self.clear).grid(
            row=2, column=1, sticky="ew", padx=(3, 0)
        )

    def set_mode(self, mode: str) -> None:
        if mode == "pac":
            self.add_files_button.configure(text="添加 PAC")
            self.add_folder_button.configure(text="添加 PAC 组目录")
        else:
            self.add_files_button.configure(text="添加 TBL/DAT")
            self.add_folder_button.configure(text="添加解包目录")
        self.clear()

    def add_files(self) -> None:
        if self.mode_provider() == "pac":
            title = "选择 PAC 文件"
            filetypes = [("PAC 文件", "*.pac"), ("所有文件", "*.*")]
        else:
            title = "选择 TBL/DAT 文件"
            filetypes = [
                ("文本资源", "*.tbl *.dat"),
                ("TBL 文件", "*.tbl"),
                ("DAT 文件", "*.dat"),
                ("所有文件", "*.*"),
            ]
        selected = filedialog.askopenfilenames(title=title, filetypes=filetypes)
        self._add_paths(selected)

    def add_folder(self) -> None:
        title = (
            "选择 PAC 组目录"
            if self.mode_provider() == "pac"
            else "选择解包目录"
        )
        selected = filedialog.askdirectory(title=title)
        if selected:
            self._add_paths([selected])

    def remove_selected(self) -> None:
        removed = set(self.listbox.curselection())
        self.paths = [
            path for index, path in enumerate(self.paths) if index not in removed
        ]
        self._refresh()

    def clear(self) -> None:
        self.paths.clear()
        self._refresh()

    def get_paths(self) -> list[str]:
        return [str(path) for path in self.paths if path.exists()]

    def _add_paths(self, paths) -> None:
        existing = set(self.paths)
        for raw in paths:
            path = Path(raw).resolve()
            if path not in existing:
                self.paths.append(path)
                existing.add(path)
        self._refresh()

    def _refresh(self) -> None:
        self.listbox.delete(0, "end")
        for path in self.paths:
            self.listbox.insert("end", str(path))


class RetextTkApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._native_tk_scaling = window_tk_scaling(self.root)
        self.root.tk.call("tk", "scaling", self._native_tk_scaling)
        self._dpi_refresh_pending = None
        self.business = WorkspaceBusiness()
        self.preview_service = AssetPreviewService()
        self.pac_session_root = create_runtime_dir("pac_session")
        self.pac_manager = PacWorkspaceManager(
            workspaces_root=self.pac_session_root / "workspaces",
            trash_root=self.pac_session_root / "trash",
            ephemeral=True,
        )
        # Historical persistent workspaces created by older releases remain
        # independently manageable, but are never reused by the live editor.
        self.pac_cache_manager = PacWorkspaceManager()
        self.workbench = PacWorkbench(self.pac_manager)
        self.pac_fallback = PacFallbackTools()
        self.pac_compare = PacComparisonSession()
        self.session = DocumentSession()
        self.current_origin = "none"
        self.current_project_id: str | None = None
        self.current_pac_entry: str | None = None
        self.current_file: Path | None = None
        self.current_index: int | None = None
        self.single_editor: tk.Text | None = None
        self.single_editor_item: str | None = None
        self.pac_tree_refs: dict[str, PacNodeRef] = {}
        self._suspend_tree_preview = False
        self.unpacked_roots: list[Path] = []
        self.unpacked_tree_paths: dict[str, Path] = {}
        self.preview_document = None
        self.preview_selection: PreviewFileSelection | None = None
        self.preview_media: MediaPreview | None = None
        self.preview_display_image: Image.Image | None = None
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.preview_font_row_glyphs: dict[str, FontGlyph] = {}
        self.preview_row_indices: dict[str, int] = {}
        self.single_row_indices: dict[str, int] = {}
        self.search_results: list[tuple[int, int, int]] = []
        self.search_cursor = -1
        self.search_signature: tuple[str, bool] | None = None
        self.batch_hits: list[object] = []
        self.batch_targets: list[object] = []
        self.batch_target_by_file: dict[str, object] = {}
        self.batch_scan_options: SessionOptions | None = None
        self.batch_scan_complete = False
        self.batch_behavior_operations: list[dict[str, object]] = []
        self._batch_cell_overlays: dict[tuple[str, str], tk.Text] = {}
        self._batch_rich_after_id: str | None = None
        self.diff_files_all: list[object] = []
        self.diff_files: list[object] = []
        self.diff_entries_all: list[object] = []
        self.diff_entries_visible: list[object] = []
        self.features_active = False
        self._busy = False
        self._background_queue: queue.Queue = queue.Queue()
        self._active_progressbar: ttk.Progressbar | None = None
        self._active_progress_text_var: tk.StringVar | None = None
        self._batch_sash_initialized = False
        self._batch_body_sash_initialized = False
        self._main_sash_guard_after_id: str | None = None
        self.preview_playback: MediaPlaybackController | None = None
        self._preview_playback_source: Path | None = None
        self._preview_progress_after_id: str | None = None
        self._preview_scrubbing = False
        self._preview_resume_after_scrub = False
        self._preview_pending_seek: float | None = None
        self._preview_video_auto_prime_after_id: str | None = None
        self._preview_video_prime_after_id: str | None = None
        self._preview_video_prime_generation = 0
        self._preview_video_prime_target: float | None = None
        self._preview_video_prime_phase = "idle"
        self._preview_video_prime_deadline = 0.0
        self._preview_video_prime_seeked_at = 0.0
        self._preview_video_prime_origin_position: float | None = None
        self._preview_video_prime_ready_polls = 0
        self._preview_video_user_started = False
        self.preview_model_yaw = 0.45
        self.preview_model_pitch = -0.12
        self.preview_model_zoom = 0.95
        self.preview_model_pan_x = 0.0
        self.preview_model_pan_y = 0.0
        self.preview_model_drag_anchor: tuple[int, int] | None = None
        self.preview_model_pan_anchor: tuple[int, int] | None = None
        self._preview_model_low_quality = False
        self._preview_model_render_after_id: str | None = None
        self._preview_model_full_render_after_id: str | None = None
        self._preview_model_generation = 0
        self._preview_model_worker_lock = threading.Lock()
        self._preview_model_request_event = threading.Event()
        self._preview_model_worker_stop = threading.Event()
        self._preview_model_worker_thread: threading.Thread | None = None
        self._preview_model_pending_request: ModelRenderRequest | None = None
        self._preview_model_worker_running = False
        self._preview_model_worker_busy = False
        self._preview_model_results: queue.Queue = queue.Queue()
        self._preview_model_poll_after_id: str | None = None
        self._preview_model_pose_cache_key: tuple[int, float] | None = None
        self._preview_model_pose_cache_geometry: object | None = None
        self.preview_model_animation_time = 0.0
        self._preview_model_animation_playing = False
        self._preview_model_animation_after_id: str | None = None
        self._preview_model_animation_started_at = 0.0
        self._preview_model_animation_origin = 0.0
        self._preview_model_animation_scrubbing = False
        self._preview_model_animation_resume_after_scrub = False
        self._preview_model_animation_frame_dirty = False

        self.tbl_engine_items = [
            ("Kuro", "kuro_tbl"),
            ("Legacy", "legacy"),
        ]
        self.dat_engine_items = [
            ("Legacy", "legacy"),
            ("Kuro", "kuro_dat"),
        ]
        self.tbl_engine_label_to_value = dict(self.tbl_engine_items)
        self.dat_engine_label_to_value = dict(self.dat_engine_items)
        self.tbl_engine_value_to_label = {
            value: label for label, value in self.tbl_engine_items
        }
        self.dat_engine_value_to_label = {
            value: label for label, value in self.dat_engine_items
        }
        self.game_version_items = [
            ("自动识别（推荐）", GameVersion.AUTO.value),
            ("空之轨迹 the 1st", GameVersion.SORA1.value),
            ("空之轨迹 the 2nd", GameVersion.SORA2.value),
        ]
        self.game_version_label_to_value = dict(self.game_version_items)
        self.game_version_value_to_label = {
            value: label for label, value in self.game_version_items
        }
        self.resource_mode_items = [
            ("PAC 模式", "pac"),
            ("解包模式", "unpacked"),
        ]
        self.resource_mode_label_to_value = dict(self.resource_mode_items)
        self.resource_mode_value_to_label = {
            value: label for label, value in self.resource_mode_items
        }
        self.resource_mode_var = tk.StringVar(value="PAC 模式")
        self._previous_resource_mode = "pac"
        self.tbl_engine_var = tk.StringVar(value=self.tbl_engine_items[0][0])
        self.dat_engine_var = tk.StringVar(value=self.dat_engine_items[0][0])
        self._previous_engine_preferences = ("kuro_tbl", "legacy")
        self.game_version_var = tk.StringVar(value=self.game_version_items[0][0])
        self._previous_game_version = GameVersion.AUTO.value
        self.schema_hint_var = tk.StringVar()
        self.keep_artifacts_var = tk.BooleanVar(value=False)
        self.allow_risky_repack_var = tk.BooleanVar(value=False)
        self.route_hint_var = tk.StringVar(value=self._route_message())
        self.batch_backend_hint_var = tk.StringVar(value=self._route_message())
        self.preview_meta_var = tk.StringVar(value="请在左侧 PAC 文件树中选择文件。")
        self.preview_hint_var = tk.StringVar(
            value="如需修改，请双击左侧文件进入“单文件”。"
        )
        self.preview_media_title_var = tk.StringVar(value="媒体预览")
        self.preview_media_warning_var = tk.StringVar()
        self.preview_media_progress_var = tk.DoubleVar(value=0.0)
        self.preview_media_volume_var = tk.DoubleVar(value=80.0)
        self.preview_media_time_var = tk.StringVar(value="0:00 / 0:00")
        self.preview_media_volume_text_var = tk.StringVar(value="80%")
        self.preview_model_wireframe_var = tk.BooleanVar(value=False)
        self.preview_model_animation_progress_var = tk.DoubleVar(value=0.0)
        self.preview_model_animation_time_var = tk.StringVar(
            value="0:00.000 / 0:00.000"
        )
        self.single_meta_var = tk.StringVar(value="请从左侧 PAC 文件树双击 .tbl 或 .dat 文件。")
        self.pac_search_var = tk.StringVar()
        self.pac_search_status_var = tk.StringVar(value="输入文件名可循环查找。")
        self._pac_search_last_query = ""
        self._pac_search_last_match_keys: tuple[tuple[str, str, str], ...] = ()
        self._pac_search_cursor = -1
        self.find_var = tk.StringVar()
        self.replace_var = tk.StringVar()
        self.case_var = tk.BooleanVar(value=False)
        self.batch_globs_var = tk.StringVar(value="*.tbl,*.dat")
        self.batch_intro_var = tk.StringVar(
            value="选择范围 → 查找匹配 → 替换勾选项 → 导出 PAC。"
        )
        self.integrity_status_var = tk.StringVar(
            value="尚未执行 DAT 指针完整性扫描。"
        )
        self.integrity_target_pac: Path | None = None
        self.integrity_reference_pac: Path | None = None
        self.integrity_last_pac_scan_report = None
        self.integrity_strategy_items = [
            ("保守（推荐）", "conservative"),
            ("激进（高风险）", "aggressive"),
        ]
        self.integrity_strategy_label_to_value = dict(self.integrity_strategy_items)
        self.integrity_strategy_var = tk.StringVar(
            value=self.integrity_strategy_items[0][0]
        )
        self.integrity_target_pac_var = tk.StringVar(value="目标 PAC：未加载")
        self.integrity_reference_pac_var = tk.StringVar(value="参考 PAC：未加载")
        self.batch_progress_text_var = tk.StringVar(value="尚未开始批量任务。")
        self.diff_globs_var = tk.StringVar(value="*.tbl,*.dat")
        self.diff_show_same_files_var = tk.BooleanVar(value=False)
        self.diff_show_all_var = tk.BooleanVar(value=False)
        self.diff_progress_text_var = tk.StringVar(value="尚未开始版本对比。")
        self.diff_mode_hint_var = tk.StringVar(
            value="PAC 模式：选择新旧 PAC 文件，或包含 PAC 的目录组。"
        )
        self.pac_fallback_var = tk.BooleanVar(value=False)
        self.pac_meta_var = tk.StringVar(value="尚未打开 PAC。")
        self.pac_data_root_var = tk.StringVar(
            value=f"程序数据目录：{self.pac_cache_manager.data_root}"
        )
        self.status_var = tk.StringVar(value="请先打开一个或多个 PAC。")
        self.ui_scale_choices = ["67%", "75%", "85%", "100%", "125%", "150%", "175%", "200%"]
        self.ui_scale_var = tk.StringVar(value="100%")

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_runtime()
        self.root.bind("<Configure>", self._on_window_dpi_change, add="+")
        self.refresh_pac_cache()

    def _build(self) -> None:
        self.root.title(f"空之轨迹 重制版文本工作台 · {__version__}")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = min(2100, round(screen_width * 0.92))
        window_height = min(1250, round(screen_height * 0.92))
        window_x = max(0, (screen_width - window_width) // 2)
        window_y = max(0, (screen_height - window_height) // 3)
        self.root.geometry(
            f"{window_width}x{window_height}+{window_x}+{window_y}"
        )
        self.root.minsize(min(1060, screen_width - 60), min(680, screen_height - 80))
        self.checkbox_images = self._build_checkbox_images()
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        apply_theme(self.root, self.style, self._native_tk_scaling,
                    self._parse_scale(self.ui_scale_var.get()))

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="空之轨迹 重制版文本工作台",
            style="Title.TLabel",
        ).pack(anchor="w")
        top = ttk.Frame(outer, style="Toolbar.TFrame", padding=8)
        top.pack(fill="x", pady=(8, 10))
        self.pac_toolbar = ttk.Frame(top, style="Toolbar.TFrame")
        self.pac_toolbar.pack(side="left")
        ttk.Button(
            self.pac_toolbar,
            text="打开 PAC…",
            command=self.open_pacs,
            style="Accent.TButton",
        ).pack(side="left")
        ttk.Button(
            self.pac_toolbar,
            text="导出 PAC…",
            command=self.build_selected_pac,
        ).pack(
            side="left",
            padx=6,
        )
        ttk.Button(
            self.pac_toolbar,
            text="关闭 PAC",
            command=self.close_selected_pacs,
        ).pack(
            side="left",
        )
        mode_group = ttk.Frame(top, style="Toolbar.TFrame")
        mode_group.pack(side="right")
        mode_combo = ttk.Combobox(mode_group, textvariable=self.resource_mode_var,
            values=[label for label, _ in self.resource_mode_items], state="readonly", width=9)
        mode_combo.pack(side="left", padx=8)
        mode_combo.bind("<<ComboboxSelected>>", self.on_resource_mode_changed)
        ttk.Button(mode_group, text="设置…", command=self.show_settings).pack(side="right")
        self.settings_window = tk.Toplevel(self.root)
        self.settings_window.withdraw()
        self.settings_window.title("编辑器设置")
        self.settings_window.transient(self.root)
        self.settings_window.protocol("WM_DELETE_WINDOW", self.settings_window.withdraw)
        global_settings = ttk.Frame(self.settings_window, padding=20, style="Card.TFrame")
        global_settings.pack(fill="both", expand=True)

        tbl_group = ttk.Frame(global_settings, style="Toolbar.TFrame")
        tbl_group.pack(fill="x", pady=6)
        ttk.Label(tbl_group, text="TBL 引擎", style="Toolbar.TLabel").pack(
            side="left", padx=(0, 4)
        )
        tbl_engine_combo = ttk.Combobox(
            tbl_group,
            textvariable=self.tbl_engine_var,
            values=[label for label, _value in self.tbl_engine_items],
            state="readonly",
            width=8,
        )
        tbl_engine_combo.pack(side="left")
        tbl_engine_combo.bind(
            "<<ComboboxSelected>>",
            self.on_engine_preferences_changed,
        )

        dat_group = ttk.Frame(global_settings, style="Toolbar.TFrame")
        dat_group.pack(fill="x", pady=6)
        ttk.Label(dat_group, text="DAT 引擎", style="Toolbar.TLabel").pack(
            side="left", padx=(0, 4)
        )
        dat_engine_combo = ttk.Combobox(
            dat_group,
            textvariable=self.dat_engine_var,
            values=[label for label, _value in self.dat_engine_items],
            state="readonly",
            width=8,
        )
        dat_engine_combo.pack(side="left")
        dat_engine_combo.bind(
            "<<ComboboxSelected>>",
            self.on_engine_preferences_changed,
        )

        game_group = ttk.Frame(global_settings, style="Toolbar.TFrame")
        game_group.pack(fill="x", pady=6)
        ttk.Label(game_group, text="游戏版本", style="Toolbar.TLabel").pack(
            side="left", padx=(0, 4)
        )
        game_combo = ttk.Combobox(
            game_group,
            textvariable=self.game_version_var,
            values=[label for label, _value in self.game_version_items],
            state="readonly",
            width=18,
        )
        game_combo.pack(side="left")
        game_combo.bind("<<ComboboxSelected>>", self.on_game_version_changed)

        scale_group = ttk.Frame(global_settings, style="Toolbar.TFrame")
        scale_group.pack(fill="x", pady=6)
        ttk.Label(scale_group, text="界面缩放", style="Toolbar.TLabel").pack(
            side="left", padx=(0, 4)
        )
        ttk.Combobox(
            scale_group,
            textvariable=self.ui_scale_var,
            values=self.ui_scale_choices,
            state="readonly",
            width=7,
        ).pack(side="left")
        ttk.Button(
            scale_group,
            text="应用",
            command=self.apply_ui_scale_choice,
        ).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(global_settings, text="保留 DAT 调试文件", variable=self.keep_artifacts_var).pack(anchor="w", pady=6)
        ttk.Checkbutton(global_settings, text="允许启发式回退写入", variable=self.allow_risky_repack_var,
            command=self.on_risky_repack_changed).pack(anchor="w", pady=6)
        schema = ttk.Frame(global_settings, style="Card.TFrame")
        schema.pack(fill="x", pady=6)
        ttk.Label(schema, text="TBL Schema（可选）").pack(side="left")
        ttk.Entry(schema, textvariable=self.schema_hint_var, width=18).pack(side="left", padx=8)
        ttk.Button(global_settings, text="完成", command=self.settings_window.withdraw).pack(anchor="e", pady=(12, 0))

        self.primary_nb = ttk.Notebook(outer)
        self.primary_nb.pack(fill="both", expand=True)
        self.workspace_page = ttk.Frame(self.primary_nb, padding=(0, 8, 0, 0))
        self.primary_nb.add(self.workspace_page, text="文本工作台")

        self.main_pane = ttk.Panedwindow(self.workspace_page, orient="horizontal")
        self.main_pane.pack(fill="both", expand=True)
        explorer = ttk.Frame(self.main_pane, style="Card.TFrame", width=330)
        work = ttk.Frame(self.main_pane)
        # The explorer is a navigation control, not spare elastic space.  A
        # non-zero weight let notebook/layout recalculations squeeze it down to
        # the sash itself (or make it grow excessively on a wide monitor).
        # Keep its width stable while the work area receives window resizes.
        self.main_pane.add(explorer, weight=0)
        self.main_pane.add(work, weight=1)
        self.main_pane.bind(
            "<Configure>",
            self._schedule_main_sash_guard,
            add="+",
        )
        self.main_pane.bind(
            "<ButtonRelease-1>",
            self._schedule_main_sash_guard,
            add="+",
        )
        self.pac_explorer_frame = ttk.Frame(
            explorer,
            style="Card.TFrame",
            padding=10,
        )
        self.unpacked_explorer_frame = ttk.Frame(
            explorer,
            style="Card.TFrame",
            padding=10,
        )
        self._build_pac_explorer(self.pac_explorer_frame)
        self._build_unpacked_explorer(self.unpacked_explorer_frame)
        self.pac_explorer_frame.pack(fill="both", expand=True)

        self.nb = ttk.Notebook(work)
        self.nb.pack(fill="both", expand=True)
        self._build_welcome_tab()
        self._build_preview_tab()
        self._build_single_tab()
        self._build_batch_tab()
        self.pac_files_tab = PacFilesView(self.nb, workbench=self.workbench,
            open_pacs=self.open_pacs, extract=self.extract_pac_selection,
            replace=self.replace_pac_file, insert=self.insert_pac_files,
            export=self.build_selected_pac, open_file=self._open_explorer_file,
            format_size=self._format_bytes, state_label=self._pac_entry_state_label,
            ensure_idle=self._ensure_idle)
        self._build_integrity_tab()
        self._build_workspace_tab()
        self.feature_tabs = [
            (self.preview_tab, "预览"),
            (self.single_tab, "单文件"),
            (self.batch_tab, "批量"),
            (self.pac_files_tab, "包内文件"),
        ]
        self._build_diff_tab()
        self.primary_nb.insert("end", self.workspace_tab)
        self.nb.bind("<<NotebookTabChanged>>", self._on_workspace_tab_changed)
        self.primary_nb.bind(
            "<<NotebookTabChanged>>",
            self._on_primary_tab_changed,
        )
        self.root.after(350, lambda: self._set_main_sash(330))
        self._apply_ui_scale(self._parse_scale(self.ui_scale_var.get()))
        self.status_label = ttk.Label(
            outer,
            textvariable=self.status_var,
            style="Subtitle.TLabel",
        )
        self.status_label.pack(side="bottom", fill="x", pady=(8, 0), before=self.primary_nb)
        self.root.bind("<Control-o>", lambda _e: self.open_pacs() if self.current_resource_mode() == "pac" else self.add_unpacked_files())
        self.root.bind("<Control-s>", lambda _e: self.save_single() if self.session.document else None)
        self.root.bind("<Control-Shift-S>", lambda _e: self.build_selected_pac() if self.current_resource_mode() == "pac" else self.export_single())

    def show_settings(self) -> None:
        if not self._ensure_idle():
            return
        self.settings_window.update_idletasks()
        width = self.settings_window.winfo_reqwidth()
        height = self.settings_window.winfo_reqheight()
        x = max(0, self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2)
        y = max(0, self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2)
        self.settings_window.geometry(f"+{x}+{y}")
        self.settings_window.deiconify()
        self.settings_window.lift()

    def _build_pac_explorer(self, parent) -> None:
        ttk.Label(parent, text="资源文件", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            parent,
            text="单击文件预览 · 双击 PAC 管理文件",
            style="Muted.TLabel",
            wraplength=260,
            justify="left",
        ).pack(anchor="w", pady=(2, 7))
        ttk.Label(parent, textvariable=self.pac_meta_var, style="Muted.TLabel").pack(
            anchor="w",
            pady=(0, 7),
        )
        workspace_actions = ttk.Frame(parent, style="Card.TFrame")
        workspace_actions.pack(fill="x", pady=(0, 7))
        workspace_actions.columnconfigure(0, weight=1, uniform="pac_workspace")
        workspace_actions.columnconfigure(1, weight=1, uniform="pac_workspace")
        ttk.Button(
            workspace_actions,
            text="重新载入",
            command=self.reload_selected_pac_workspaces,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(
            workspace_actions,
            text="放弃修改",
            command=self.clear_selected_pac_workspaces,
        ).grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self._build_resource_search(parent, pac=True)
        tree_frame = ttk.Frame(parent, style="Card.TFrame")
        tree_frame.pack(fill="both", expand=True)
        self.pac_tree = ttk.Treeview(
            tree_frame,
            columns=("type", "size", "state"),
            show="tree headings",
            selectmode="extended",
        )
        self.pac_tree.heading("#0", text="PAC / 路径")
        self.pac_tree.column("#0", width=170, minwidth=120)
        for key, label, width in (
            ("type", "类型", 54),
            ("size", "大小", 66),
            ("state", "状态", 68),
        ):
            self.pac_tree.heading(key, text=label)
            self.pac_tree.column(key, width=width, minwidth=44, stretch=False)
        ybar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.pac_tree.yview)
        xbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.pac_tree.xview)
        self.pac_tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.pac_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.pac_tree.bind("<<TreeviewSelect>>", self.on_pac_tree_select)
        self.pac_tree.bind("<Double-1>", self.on_pac_tree_double_click)
        self.pac_tree.bind("<Return>", lambda _e: self._open_selected_pac_text())
        self.pac_tree.bind("<Button-3>", self._pac_context_menu)
        self.pac_tree.configure(displaycolumns=("size", "state"))
        self.pac_tree.column("#0", width=170, minwidth=100)
        self.pac_tree.column("size", width=80, minwidth=66)
        self.pac_tree.column("state", width=76, minwidth=60)

    def _pac_context_menu(self, event) -> None:
        item = self.pac_tree.identify_row(event.y)
        if not item:
            return
        if item not in self.pac_tree.selection():
            self.pac_tree.selection_set(item)
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="在包内文件页中打开", command=lambda: self.show_pac_files(self.pac_tree_refs[item]))
        menu.add_command(label="提取所选文件 / 文件夹…", command=self.extract_pac_selection)
        menu.add_command(label="用外部文件替换…", command=self.replace_pac_file)
        menu.add_command(label="插入外部文件…", command=self.insert_pac_files)
        menu.add_separator()
        menu.add_command(label="加入批量处理范围", command=self._add_pac_batch_scope)
        menu.add_command(label="导出 PAC…", command=self.build_selected_pac)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_selected_pac_text(self) -> None:
        refs = self.selected_pac_refs()
        if len(refs) == 1:
            if refs[0].kind == "file":
                self._open_explorer_file(refs[0])
            else:
                self.show_pac_files(refs[0])

    def show_pac_files(self, ref) -> None:
        if not self._ensure_idle():
            return
        self.pac_files_tab.show_ref(ref)
        self.nb.select(self.pac_files_tab)

    def _open_explorer_file(self, ref) -> None:
        if is_text_document_path(ref.path):
            self._open_pac_entry(ref)
        else:
            item = next((i for i, candidate in self.pac_tree_refs.items() if candidate == ref), None)
            if item:
                self.pac_tree.selection_set(item)
                self.pac_tree.see(item)
                self.on_pac_tree_select()
                self.nb.select(self.preview_tab)

    def _add_pac_batch_scope(self) -> None:
        self.batch_sources.add_current_selection()
        self.nb.select(self.batch_tab)

    def extract_pac_selection(self, refs=None) -> None:
        if not self._ensure_idle():
            return
        refs = self.selected_pac_refs() if refs is None else refs
        if not refs:
            messagebox.showinfo("提取文件", "请先选择文件、文件夹或 PAC。", parent=self.root)
            return
        if not self._confirm_pending_changes():
            return
        if len(refs) == 1 and refs[0].kind == "file":
            ref = refs[0]
            target = filedialog.asksaveasfilename(title="提取文件", initialfile=Path(ref.path).name, parent=self.root)
            if not target:
                return
            worker = lambda: [self.workbench.get(ref.workspace_id).workspace.export_entry(ref.path, target)]
        else:
            target = filedialog.askdirectory(title="选择提取目录（保留包内路径）", parent=self.root)
            if not target:
                return
            worker = lambda: self.workbench.export_refs(refs, target)
        self._run_background("正在提取文件…", worker,
            lambda paths: self.status_var.set(f"已提取 {len(paths)} 个文件到 {target}"))

    def replace_pac_file(self, refs=None) -> None:
        if not self._ensure_idle():
            return
        refs = self.selected_pac_refs() if refs is None else refs
        if len(refs) != 1 or refs[0].kind != "file":
            messagebox.showinfo("替换文件", "请选择一个包内文件。", parent=self.root)
            return
        ref = refs[0]
        source = filedialog.askopenfilename(title=f"替换 {ref.path}", parent=self.root)
        if not source or not self._confirm_pending_changes():
            return
        project = self.workbench.get(ref.workspace_id)
        self._run_background("正在导入替换文件…",
            lambda: project.workspace.import_file(ref.path, source, replace_existing=True),
            lambda _entry: self._on_pac_files_imported(ref.workspace_id, [ref.path]))

    def insert_pac_files(self, refs=None) -> None:
        if not self._ensure_idle():
            return
        refs = self.selected_pac_refs() if refs is None else refs
        if len(refs) != 1:
            messagebox.showinfo("插入文件", "请选择目标 PAC、文件夹或文件所在目录。", parent=self.root)
            return
        ref = refs[0]
        sources = filedialog.askopenfilenames(title="选择要插入的外部文件", parent=self.root)
        if not sources:
            return
        folder = ref.path if ref.kind == "folder" else (str(Path(ref.path).parent).replace("\\", "/") if ref.kind == "file" else "")
        folder = "" if folder == "." else folder
        default = f"{folder}/{Path(sources[0]).name}".lstrip("/") if len(sources) == 1 else folder
        target = simpledialog.askstring("插入文件", "包内完整文件路径：" if len(sources) == 1 else "包内目标文件夹（留空表示根目录）：",
            initialvalue=default, parent=self.root)
        if target is None or not self._confirm_pending_changes():
            return
        target = target.replace("\\", "/")
        names = [target] if len(sources) == 1 else [f"{target.rstrip('/')}/{Path(p).name}".lstrip("/") for p in sources]
        project = self.workbench.get(ref.workspace_id)
        def worker():
            if len(set(names)) != len(names):
                raise ValueError("所选外部文件有重名，请分次插入并指定不同的包内路径。")
            return project.workspace.insert_files(dict(zip(names, sources)))
        self._run_background("正在插入文件…", worker,
            lambda _result: self._on_pac_files_imported(ref.workspace_id, names))

    def _on_pac_files_imported(self, workspace_id: str, names: list[str]) -> None:
        project = self.workbench.get(workspace_id)
        if self.current_project_id == workspace_id and self.current_pac_entry in names:
            project.current_entry = None
            project.document_session.document = None
            self._clear_single_document_view()
        self.refresh_pac_tree()
        self.batch_scan_complete = False
        self.on_pac_tree_select()
        self._refresh_pac_dependent_preview()
        self.status_var.set(f"已导入 {len(names)} 个文件；点击“导出 PAC”生成成品。")

    def _set_main_sash(self, width: int) -> None:
        self._ensure_main_sidebar_visible(preferred=width, force=True)

    def _schedule_main_sash_guard(self, _event=None) -> None:
        if self._main_sash_guard_after_id is not None:
            try:
                self.root.after_cancel(self._main_sash_guard_after_id)
            except tk.TclError:
                pass
        self._main_sash_guard_after_id = self.root.after_idle(
            self._ensure_main_sidebar_visible
        )

    @staticmethod
    def _main_sash_target(
        total: int,
        current: int,
        *,
        preferred: int = 330,
        force: bool = False,
        pixel_scale: float = 1.0,
    ) -> int | None:
        """Return a safe explorer width, or ``None`` when no move is needed."""

        if total < 520:
            return None
        minimum_left = min(round(280 * pixel_scale), max(round(180 * pixel_scale), total // 4))
        minimum_right = min(round(720 * pixel_scale), max(round(300 * pixel_scale), round(total * 0.48)))
        maximum_left = max(minimum_left, total - minimum_right)
        preferred = max(minimum_left, min(round(preferred * pixel_scale), maximum_left))
        if force or current < minimum_left:
            return preferred
        if current > maximum_left:
            return maximum_left
        return None

    def _ensure_main_sidebar_visible(
        self,
        *,
        preferred: int = 330,
        force: bool = False,
    ) -> None:
        self._main_sash_guard_after_id = None
        try:
            total = self.main_pane.winfo_width()
            current = self.main_pane.sashpos(0)
            target = self._main_sash_target(
                total,
                current,
                preferred=preferred,
                force=force,
                pixel_scale=self._layout_scale(),
            )
            if target is not None and target != current:
                self.main_pane.sashpos(0, target)
        except tk.TclError:
            pass

    def _fit_batch_sash(self, _event=None) -> None:
        if self._batch_sash_initialized:
            return
        total = self.batch_pane.winfo_width()
        if total < 640:
            return
        scale = self._layout_scale()
        target = max(round(280 * scale), min(round(360 * scale), total - round(460 * scale)))
        self._batch_sash_initialized = True
        self.batch_pane.sashpos(0, target)

    def _fit_batch_body_sash(self, _event=None) -> None:
        if self._batch_body_sash_initialized:
            return
        total = self.batch_body_pane.winfo_height()
        if total < 180:
            return
        target = max(round(total * 0.45), total - round(110 * self._layout_scale()))
        self._batch_body_sash_initialized = True
        self.batch_body_pane.sashpos(0, target)

    def _build_unpacked_explorer(self, parent) -> None:
        ttk.Label(parent, text="解包文件树", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            parent,
            text="添加普通资源文件或包含它们的目录。单击预览；仅 TBL/DAT 可双击编辑。",
            style="Muted.TLabel",
            wraplength=260,
            justify="left",
        ).pack(anchor="w", pady=(2, 7))
        actions = ttk.Frame(parent, style="Card.TFrame")
        actions.pack(fill="x", pady=(0, 8))
        actions.columnconfigure(0, weight=1, uniform="unpacked_tree_actions")
        actions.columnconfigure(1, weight=1, uniform="unpacked_tree_actions")
        ttk.Button(actions, text="添加文件", command=self.add_unpacked_files).grid(
            row=0, column=0, sticky="ew", padx=(0, 3), pady=(0, 4)
        )
        ttk.Button(actions, text="添加目录", command=self.add_unpacked_folder).grid(
            row=0, column=1, sticky="ew", padx=(3, 0), pady=(0, 4)
        )
        ttk.Button(actions, text="移除所选", command=self.remove_unpacked_roots).grid(
            row=1, column=0, columnspan=2, sticky="ew"
        )
        self._build_resource_search(parent, pac=False)
        tree_frame = ttk.Frame(parent, style="Card.TFrame")
        tree_frame.pack(fill="both", expand=True)
        self.unpacked_tree = ttk.Treeview(
            tree_frame,
            columns=("type", "size"),
            show="tree headings",
            selectmode="extended",
        )
        self.unpacked_tree.heading("#0", text="目录 / 文件")
        self.unpacked_tree.column("#0", width=210, minwidth=140)
        self.unpacked_tree.heading("type", text="类型")
        self.unpacked_tree.column("type", width=55, minwidth=44, stretch=False)
        self.unpacked_tree.heading("size", text="大小")
        self.unpacked_tree.column("size", width=70, minwidth=50, stretch=False)
        ybar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.unpacked_tree.yview)
        xbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.unpacked_tree.xview)
        self.unpacked_tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.unpacked_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.unpacked_tree.bind("<<TreeviewSelect>>", self.on_unpacked_tree_select)
        self.unpacked_tree.bind("<Double-1>", self.on_unpacked_tree_double_click)

    def _build_resource_search(self, parent, *, pac) -> None:
        if not hasattr(self, "content_query_var"):
            self.content_query_var = self.pac_search_var
            self.content_status_var = self.pac_search_status_var
            self.resource_search_mode_var = tk.StringVar(self.root, "文件名")
            self._content_signature = None
            self._content_hits = []
            self._content_targets = []
            self._content_cursor = -1
            self._content_generation = 0
            self._content_running = False
            self._content_queue = queue.Queue()
            self._content_stop = None
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 4))
        selector = ttk.Combobox(row, textvariable=self.resource_search_mode_var, values=("文件名", "正文"), state="readonly", width=6)
        selector.pack(side="left", padx=(0, 4))
        selector.bind("<<ComboboxSelected>>", lambda _e: self.content_status_var.set("搜索全部已打开资源；↑↓ 循环定位"))
        entry = ttk.Entry(row, textvariable=self.content_query_var, width=12)
        entry.pack(side="left", fill="x", expand=True)
        if pac:
            self.pac_search_entry = entry
        else:
            self.unpacked_search_entry = entry
        entry.bind("<Return>", lambda _e: self.find_resource(1))
        entry.bind("<Shift-Return>", lambda _e: self.find_resource(-1))
        ttk.Button(row, text="↑", width=2, style="Compact.TButton", command=lambda: self.find_resource(-1)).pack(side="left")
        ttk.Button(row, text="↓", width=2, style="Compact.TButton", command=lambda: self.find_resource(1)).pack(side="left")
        ttk.Label(parent, textvariable=self.content_status_var, style="Muted.TLabel", wraplength=280).pack(anchor="w", pady=(0, 6))

    def find_resource(self, direction=1):
        if self.resource_search_mode_var.get() == "正文":
            return self.find_content(direction)
        if self.current_resource_mode() == "pac":
            return self.find_next_pac_file(direction=direction)
        if not self._ensure_idle():
            return
        query = _clean_search_input(self.pac_search_var.get()).casefold()
        matches = [(iid, path) for iid, path in self.unpacked_tree_paths.items()
                   if query and path.is_file() and query in path.name.casefold()]
        signature = (query, tuple(str(path) for _, path in matches))
        if not matches:
            self.pac_search_status_var.set("没有匹配文件名。" if query else "请输入文件名。")
            return
        cursor = getattr(self, "_unpacked_search_cursor", -1) if signature == getattr(self, "_unpacked_search_signature", None) else (-1 if direction > 0 else 0)
        cursor = (cursor + direction) % len(matches)
        self._unpacked_search_cursor, self._unpacked_search_signature = cursor, signature
        iid, path = matches[cursor]
        self.unpacked_tree.selection_set(iid)
        self.unpacked_tree.focus(iid)
        self.unpacked_tree.see(iid)
        self.pac_search_status_var.set(f"文件名 {cursor + 1}/{len(matches)}（循环）：{path.name}")

    def _content_source_stamp(self):
        paths = [Path(t.path) for t in self._content_targets]
        if self.current_resource_mode() == "pac":
            roots = [(p.workspace.workspace_id, str(p.workspace.archive.source_path), tuple(e.name for e in p.workspace.entries())) for p in self.workbench.projects()]
            paths.extend(p.workspace.archive.source_path for p in self.workbench.projects())
        else:
            roots = list(map(str, self.unpacked_roots))
            # Include newly added files as well as changes to previously scanned files.
            paths.extend(Path(t.path) for t in self.business.collect_file_targets(self.unpacked_roots, "*.tbl,*.dat"))
        stats = []
        for path in paths:
            try:
                stat = path.stat()
                stats.append((str(path), stat.st_size, stat.st_mtime_ns))
            except OSError:
                stats.append((str(path), None, None))
        return repr(roots), tuple(stats)

    def find_content(self, direction=1) -> None:
        if self._busy:
            self.status_var.set("请等待当前任务完成。")
            return
        query = _clean_search_input(self.content_query_var.get())
        if not query:
            self._cancel_content_search()
            self.content_status_var.set("请输入要查找的正文。")
            return
        mode = self.current_resource_mode()
        options = self._resolve_options(Path("search.tbl"))
        options.schema_hint = ""
        key = (mode, query, repr(options))
        if self._content_running and key == self._content_run_key:
            self._navigate_content(direction)
            return
        signature = (*key, self._content_source_stamp())
        if signature == self._content_signature:
            self._navigate_content(direction)
            return
        if not self._confirm_pending_changes():
            return
        self._cancel_content_search()
        if mode == "pac":
            sources = []
            for project in self.workbench.projects():
                workspace = project.workspace
                dirty_names = set(workspace.dirty_entry_names())
                for entry in workspace.editable_entries():
                    sources.append((
                        BusinessFileTarget(str(workspace.entry_path(entry)),
                                           entry.name, workspace.workspace_id),
                        workspace.archive, entry.name in dirty_names,
                    ))
            targets = [source[0] for source in sources]
        else:
            sources = []
            targets = self.business.collect_file_targets(list(self.unpacked_roots), "*.tbl,*.dat")
        if not targets:
            self.content_status_var.set("没有可搜索的文本资源，请先打开 PAC 或目录。")
            return
        self._content_targets = targets
        self._content_hits = []
        self._content_cursor = -1
        self._content_error_count = 0
        self._content_processed = 0
        self._content_run_key = key
        self._content_running = True
        generation = self._content_generation
        stop = self._content_stop = threading.Event()
        events = self._content_queue = queue.Queue()
        self.content_status_var.set("正在搜索；找到首条结果后立即显示…")

        def emit(kind, payload):
            events.put((generation, kind, payload))
        def worker():
            try:
                search = search_pac_targets if mode == "pac" else search_text_targets
                result = search(
                    sources if mode == "pac" else targets, query, options=options,
                    on_hits=lambda hits: emit("hits", hits),
                    on_progress=lambda parsed, errors, name: emit("progress", (parsed, errors, name)),
                    cancelled=stop.is_set,
                )
                emit("done", result)
            except Exception as exc:
                emit("error", exc)

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(40, lambda: self._poll_content_search(generation))

    def _cancel_content_search(self) -> None:
        stop = getattr(self, "_content_stop", None)
        if stop is not None:
            stop.set()
        self._content_generation = getattr(self, "_content_generation", 0) + 1
        if getattr(self, "_content_running", False):
            self._content_signature = None
            self.content_status_var.set("搜索已停止；请重新查找以获得完整结果。")
        self._content_running = False
        self._content_signature = None
        self._content_queue = queue.Queue()

    def _poll_content_search(self, generation) -> None:
        if generation != self._content_generation or not self._content_running:
            return
        mode, query, _options = self._content_run_key
        if (_clean_search_input(self.content_query_var.get()) != query
                or self.current_resource_mode() != mode
                or self.resource_search_mode_var.get() != "正文"):
            self._cancel_content_search()
            return
        deadline = time.perf_counter() + 0.012
        while time.perf_counter() < deadline:
            if generation != self._content_generation or not self._content_running:
                return
            try:
                token, kind, payload = self._content_queue.get_nowait()
            except queue.Empty:
                break
            if token != generation:
                continue
            if kind == "hits":
                self._content_hits.extend(payload)
                if self._content_cursor < 0 and self._content_hits:
                    self._navigate_content(1)
            elif kind == "progress":
                parsed, errors, _name = payload
                self._content_processed = parsed + errors
                self._content_error_count = errors
            elif kind == "done":
                self._content_running = False
                self._content_error_count = len(payload.errors)
                if not payload.cancelled:
                    self._content_signature = (*self._content_run_key, self._content_source_stamp())
                self._update_content_status()
                if payload.errors:
                    messagebox.showwarning(
                        "正文搜索未覆盖全部文件",
                        "以下文件无法解析，其他结果仍可定位：\n" + "\n".join(payload.errors[:12]),
                        parent=self.root,
                    )
                return
            elif kind == "error":
                self._content_running = False
                self._content_signature = None
                self.content_status_var.set(f"正文搜索失败：{payload}")
                return
        self._update_content_status()
        self.root.after(40, lambda: self._poll_content_search(generation))

    def _update_content_status(self):
        count = len(self._content_hits)
        errors = getattr(self, "_content_error_count", 0)
        note = f"；{errors} 个文件未解析" if errors else ""
        if getattr(self, "_content_running", False):
            position = f"当前 {self._content_cursor + 1}/{count}；" if count else ""
            self.content_status_var.set(
                f"{position}已找到 {count} 处，继续搜索中 "
                f"({getattr(self, '_content_processed', 0)}/{len(self._content_targets)} 文件)" + note
            )
        elif count:
            self.content_status_var.set(f"正文 {self._content_cursor + 1}/{count}（循环）" + note)
        else:
            self.content_status_var.set("没有找到匹配文本" + note)

    def _navigate_content(self, direction):
        if self._content_hits:
            self._content_cursor = (self._content_cursor + direction) % len(self._content_hits)
            self._content_navigating = True
            try:
                self._open_text_hit(self._content_hits[self._content_cursor], edit=False)
            finally:
                self._content_navigating = False
        self._update_content_status()

    def _build_welcome_tab(self) -> None:
        self.welcome_tab = ttk.Frame(self.nb, padding=36)
        self.nb.add(self.welcome_tab, text="开始")
        ttk.Label(
            self.welcome_tab,
            text="打开资源，开始编辑",
            style="Title.TLabel",
        ).pack(anchor="w", pady=(20, 10))
        ttk.Label(
            self.welcome_tab,
            text=(
                "浏览图片与语音，编辑游戏文本，或提取和替换包内资源。\n\n"
                "打开 PAC 后，从左侧选择文件；双击 TBL / DAT 即可编辑。"
            ),
            style="Subtitle.TLabel",
            justify="left",
            wraplength=760,
        ).pack(anchor="w")
        ttk.Button(
            self.welcome_tab,
            text="打开一个或多个 PAC",
            command=self.open_pacs,
            style="Accent.TButton",
        ).pack(anchor="w", pady=(22, 0))

    def _build_preview_tab(self) -> None:
        self.preview_tab = ttk.Frame(self.nb, padding=12)
        header = ttk.Frame(self.preview_tab, style="Card.TFrame", padding=10)
        header.pack(fill="x")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="只读预览", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            textvariable=self.preview_meta_var,
            style="Muted.TLabel",
            width=1,
        ).grid(row=0, column=1, sticky="ew", padx=14)
        self.preview_extract_button = ttk.Button(
            header,
            text="提取/另存为…",
            command=self.extract_preview_file,
            state="disabled",
        )
        self.preview_extract_button.grid(row=0, column=2, sticky="e")
        preview_hint = ttk.Label(
            header,
            textvariable=self.preview_hint_var,
            style="Muted.TLabel",
            justify="left",
        )
        preview_hint.grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 0))
        header.bind("<Configure>", lambda event: preview_hint.configure(wraplength=max(120, event.width - 20)))

        self.preview_body = ttk.Frame(self.preview_tab)
        self.preview_body.pack(fill="both", expand=True, pady=(10, 0))
        self.preview_text_panel = ttk.Frame(
            self.preview_body,
            style="Card.TFrame",
            padding=8,
        )
        self.preview_text_panel.pack(fill="both", expand=True)
        ttk.Label(
            self.preview_text_panel,
            text="文本条目",
            style="Section.TLabel",
        ).pack(anchor="w")
        self.preview_tree = self._tree(
            self.preview_text_panel,
            ("index", "location", "text"),
            {"index": "索引", "location": "位置", "text": "文本"},
            {"index": 80, "location": 280, "text": 760},
            adaptive=True,
        )
        self.preview_tree.column("index", stretch=False)
        self.preview_tree.column("location", stretch=False)
        self.preview_tree.bind("<Double-1>", self._on_preview_text_double_click)
        self.preview_tree.bind("<Return>", self._on_preview_text_double_click)

        self.preview_media_panel = ttk.Frame(
            self.preview_body,
            style="Card.TFrame",
            padding=8,
        )
        media_header = ttk.Frame(self.preview_media_panel, style="Card.TFrame")
        media_header.pack(fill="x")
        ttk.Label(
            media_header,
            textvariable=self.preview_media_title_var,
            style="Section.TLabel",
        ).pack(side="left")
        self.preview_font_atlas_button = ttk.Button(
            media_header,
            text="显示完整图集",
            command=self._show_preview_font_atlas,
        )

        media_body = ttk.Panedwindow(
            self.preview_media_panel,
            orient="horizontal",
        )
        media_body.pack(fill="both", expand=True, pady=(8, 0))
        self.preview_media_visual_frame = ttk.Frame(
            media_body,
            style="Panel.TFrame",
        )
        metadata_frame = ttk.Frame(media_body, style="Panel.TFrame", padding=6)
        media_body.add(self.preview_media_visual_frame, weight=4)
        media_body.add(metadata_frame, weight=2)
        self.preview_media_visual_frame.columnconfigure(0, weight=1)
        self.preview_media_visual_frame.rowconfigure(0, weight=1)
        self.preview_media_canvas = tk.Canvas(
            self.preview_media_visual_frame,
            bg="#201c18",
            highlightthickness=0,
            width=780,
            height=520,
        )
        self.preview_media_canvas.grid(row=0, column=0, sticky="nsew")
        self.preview_media_canvas.bind(
            "<Configure>",
            self._on_preview_canvas_configure,
        )
        self.preview_media_canvas.bind(
            "<ButtonPress-1>",
            self._begin_preview_model_orbit,
        )
        self.preview_media_canvas.bind(
            "<B1-Motion>",
            self._drag_preview_model_orbit,
        )
        self.preview_media_canvas.bind(
            "<ButtonRelease-1>",
            self._end_preview_model_orbit,
        )
        self.preview_media_canvas.bind(
            "<ButtonPress-2>",
            self._begin_preview_model_pan,
        )
        self.preview_media_canvas.bind(
            "<B2-Motion>",
            self._drag_preview_model_pan,
        )
        self.preview_media_canvas.bind(
            "<ButtonRelease-2>",
            self._end_preview_model_pan,
        )
        self.preview_media_canvas.bind(
            "<MouseWheel>",
            self._zoom_preview_model,
        )
        self.preview_media_canvas.bind(
            "<Button-4>",
            self._zoom_preview_model,
        )
        self.preview_media_canvas.bind(
            "<Button-5>",
            self._zoom_preview_model,
        )
        self.preview_media_canvas.bind(
            "<Double-Button-1>",
            self._reset_preview_model_view,
        )
        self.preview_media_controls = ttk.Frame(
            self.preview_media_visual_frame,
            style="Panel.TFrame",
            padding=(6, 8, 6, 4),
        )
        self.preview_media_controls.grid(row=1, column=0, sticky="ew")
        self.preview_media_controls.columnconfigure(2, weight=8)
        self.preview_media_controls.columnconfigure(5, weight=2)
        self.preview_media_play_button = ttk.Button(
            self.preview_media_controls,
            text="播放",
            command=self.play_preview_media,
            state="disabled",
            width=8,
        )
        self.preview_media_play_button.grid(row=0, column=0, padx=(0, 6))
        self.preview_media_stop_button = ttk.Button(
            self.preview_media_controls,
            text="停止",
            command=self.stop_preview_media,
            state="disabled",
            width=8,
        )
        self.preview_media_stop_button.grid(row=0, column=1, padx=(0, 8))
        self.preview_media_progress_scale = ModernScale(
            self.preview_media_controls,
            from_=0.0,
            to=1.0,
            variable=self.preview_media_progress_var,
        )
        self.preview_media_progress_scale.grid(row=0, column=2, sticky="ew")
        self.preview_media_progress_scale.bind(
            "<ButtonPress-1>",
            self._begin_preview_scrub,
            add="+",
        )
        self.preview_media_progress_scale.bind(
            "<ButtonRelease-1>",
            self._finish_preview_scrub,
            add="+",
        )
        ttk.Label(
            self.preview_media_controls,
            textvariable=self.preview_media_time_var,
            style="Muted.TLabel",
            width=11,
            anchor="e",
        ).grid(row=0, column=3, padx=(5, 8))
        ttk.Label(
            self.preview_media_controls,
            text="音量",
            style="Muted.TLabel",
        ).grid(row=0, column=4, sticky="e")
        self.preview_media_volume_scale = ModernScale(
            self.preview_media_controls,
            from_=0.0,
            to=100.0,
            variable=self.preview_media_volume_var,
            command=self._change_preview_volume,
            width=110,
        )
        self.preview_media_volume_scale.grid(
            row=0,
            column=5,
            sticky="ew",
            padx=(4, 0),
        )
        ttk.Label(
            self.preview_media_controls,
            textvariable=self.preview_media_volume_text_var,
            style="Muted.TLabel",
            width=4,
            anchor="e",
        ).grid(row=0, column=6, padx=(4, 0))
        self.preview_model_controls = ttk.Frame(
            self.preview_media_visual_frame,
            style="Panel.TFrame",
            padding=(6, 8, 6, 4),
        )
        self.preview_model_controls.grid(row=1, column=0, sticky="ew")
        self.preview_model_controls.columnconfigure(0, weight=1)
        self.preview_model_animation_controls = ttk.Frame(
            self.preview_model_controls,
            style="Panel.TFrame",
        )
        self.preview_model_animation_controls.grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 6),
        )
        self.preview_model_animation_controls.columnconfigure(2, weight=1)
        self.preview_model_animation_play_button = ttk.Button(
            self.preview_model_animation_controls,
            text="播放",
            command=self._toggle_preview_model_animation,
            width=8,
        )
        self.preview_model_animation_play_button.grid(
            row=0,
            column=0,
            padx=(0, 6),
        )
        self.preview_model_animation_stop_button = ttk.Button(
            self.preview_model_animation_controls,
            text="停止",
            command=self._stop_preview_model_animation,
            width=8,
        )
        self.preview_model_animation_stop_button.grid(
            row=0,
            column=1,
            padx=(0, 8),
        )
        self.preview_model_animation_scale = ModernScale(
            self.preview_model_animation_controls,
            from_=0.0,
            to=1.0,
            variable=self.preview_model_animation_progress_var,
            command=self._seek_preview_model_animation,
        )
        self.preview_model_animation_scale.grid(
            row=0,
            column=2,
            sticky="ew",
        )
        self.preview_model_animation_scale.bind(
            "<ButtonPress-1>",
            self._begin_preview_model_animation_scrub,
            add="+",
        )
        self.preview_model_animation_scale.bind(
            "<ButtonRelease-1>",
            self._finish_preview_model_animation_scrub,
            add="+",
        )
        ttk.Label(
            self.preview_model_animation_controls,
            textvariable=self.preview_model_animation_time_var,
            style="Muted.TLabel",
            width=17,
            anchor="e",
        ).grid(row=0, column=3, padx=(6, 0))
        self.preview_model_view_controls = ttk.Frame(
            self.preview_model_controls,
            style="Panel.TFrame",
        )
        self.preview_model_view_controls.grid(
            row=1,
            column=0,
            sticky="ew",
        )
        self.preview_model_view_controls.columnconfigure(2, weight=1)
        ttk.Button(
            self.preview_model_view_controls,
            text="重置视角",
            command=self._reset_preview_model_view,
            width=10,
        ).grid(row=0, column=0, padx=(0, 10))
        ttk.Checkbutton(
            self.preview_model_view_controls,
            text="线框",
            variable=self.preview_model_wireframe_var,
            command=self._schedule_preview_model_redraw,
        ).grid(row=0, column=1, padx=(0, 12))
        ttk.Label(
            self.preview_model_view_controls,
            text="左键旋转 · 中键平移 · 滚轮缩放 · 双击重置",
            style="Muted.TLabel",
        ).grid(row=0, column=2, sticky="e")
        self.preview_model_animation_controls.grid_remove()
        self.preview_model_controls.grid_remove()
        ttk.Label(
            metadata_frame,
            text="预览信息",
            style="Section.TLabel",
        ).pack(anchor="w")
        self.preview_media_detail_notebook = ttk.Notebook(metadata_frame)
        self.preview_media_detail_notebook.pack(fill="both", expand=True, pady=(6, 0))
        self.preview_media_info_tab = ttk.Frame(
            self.preview_media_detail_notebook,
            style="Panel.TFrame",
        )
        self.preview_font_glyph_tab = ttk.Frame(
            self.preview_media_detail_notebook,
            style="Panel.TFrame",
        )
        self.preview_media_detail_notebook.add(
            self.preview_media_info_tab,
            text="信息",
        )
        self.preview_media_detail_notebook.add(
            self.preview_font_glyph_tab,
            text="字形",
        )
        self.preview_media_meta_tree = self._tree(
            self.preview_media_info_tab,
            ("field", "value"),
            {"field": "属性", "value": "值"},
            {"field": 120, "value": 260},
        )
        self.preview_font_glyph_tree = self._tree(
            self.preview_font_glyph_tab,
            ("codepoint", "character", "atlas", "size", "offset", "advance", "channel"),
            {
                "codepoint": "码位",
                "character": "字符",
                "atlas": "图集坐标",
                "size": "尺寸",
                "offset": "偏移",
                "advance": "前进",
                "channel": "通道",
            },
            {
                "codepoint": 82,
                "character": 48,
                "atlas": 90,
                "size": 70,
                "offset": 70,
                "advance": 52,
                "channel": 58,
            },
        )
        self.preview_font_glyph_tree.bind(
            "<<TreeviewSelect>>",
            self._on_preview_font_glyph_select,
        )
        self.preview_media_detail_notebook.hide(self.preview_font_glyph_tab)
        ttk.Label(
            self.preview_media_panel,
            textvariable=self.preview_media_warning_var,
            style="Muted.TLabel",
            justify="left",
            wraplength=1000,
        ).pack(fill="x", pady=(6, 0))
        self._render_preview_message("请在左侧 PAC 文件树中选择一个文件。")

    def _build_single_tab(self) -> None:
        self.single_tab = ttk.Frame(self.nb, padding=12)
        ttk.Label(
            self.single_tab,
            textvariable=self.single_meta_var,
            style="Subtitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            self.single_tab,
            text="双击当前文本编辑 · Enter 确认 · Shift+Enter 换行 · Ctrl+S 保存",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        actions = ttk.Frame(self.single_tab)
        actions.pack(fill="x", pady=(10, 0))
        self.single_save_button = ttk.Button(
            actions,
            text="保存修改",
            command=self.save_single,
            style="Accent.TButton",
        )
        self.single_save_button.pack(
            side="left",
        )
        self.single_export_button = ttk.Button(
            actions,
            text="导出当前文件副本",
            command=self.export_single,
        )
        self.single_export_button.pack(
            side="left",
            padx=6,
        )
        ttk.Button(actions, text="保存预检", command=self.preview_single).pack(
            side="left",
        )
        search = ttk.Frame(self.single_tab, style="Panel.TFrame", padding=8)
        search.pack(fill="x", pady=(7, 0))
        search.columnconfigure(1, weight=1)
        search.columnconfigure(5, weight=1)
        ttk.Label(search, text="查找").grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=(0, 4)
        )
        self.single_find_entry = ttk.Entry(search, textvariable=self.find_var, width=14)
        self.single_find_entry.grid(row=0, column=1, sticky="ew", pady=(0, 4))
        self.single_find_entry.bind("<Return>", lambda _e: self.find_next())
        self.single_find_entry.bind("<Shift-Return>", lambda _e: self.find_next(-1))
        self.single_tree_search_bindings = (("<F3>", 1), ("<Shift-F3>", -1))
        for sequence, direction in self.single_tree_search_bindings:
            self.root.bind(sequence, lambda _e, d=direction: self.find_next(d))
        self.root.bind("<Control-f>", lambda _e: self.single_find_entry.focus_set() if self.session.document else (self.pac_search_entry if self.current_resource_mode() == "pac" else self.unpacked_search_entry).focus_set())
        ttk.Checkbutton(actions, text="区分大小写", variable=self.case_var).pack(side="right")
        ttk.Button(search, text="↑", width=2, style="Compact.TButton", command=lambda: self.find_next(-1)).grid(row=0, column=2, padx=(4, 0))
        ttk.Button(search, text="↓", width=2, style="Compact.TButton", command=self.find_next).grid(row=0, column=3, padx=(0, 6))
        ttk.Label(search, text="替换").grid(
            row=0, column=4, sticky="w", padx=(0, 6)
        )
        ttk.Entry(search, textvariable=self.replace_var, width=14).grid(
            row=0, column=5, sticky="ew"
        )
        ttk.Button(search, text="全部替换", command=self.replace_all).grid(
            row=0, column=6, sticky="ew", padx=(6, 0)
        )

        table = ttk.Frame(self.single_tab, style="Card.TFrame", padding=8)
        table.pack(fill="both", expand=True, pady=(10, 0))
        self.single_tree = self._tree(
            table,
            ("index", "location", "original", "current"),
            {
                "index": "索引",
                "location": "位置",
                "original": "原文本",
                "current": "当前文本",
            },
            {"index": 60, "location": 160, "original": 340, "current": 340},
            adaptive=True,
        )
        self.single_tree.configure(displaycolumns=("index", "original", "current"))
        self.single_tree.column("index", stretch=False, minwidth=50)
        self.single_tree.column("original", minwidth=180)
        self.single_tree.column("current", minwidth=180)
        self.single_tree.bind("<MouseWheel>", lambda _e: self._close_single_editor(save=True), add="+")
        self.single_tree.bind("<<TableGeometryChanged>>", lambda _e: self._close_single_editor(save=True))
        self.single_tree.bind("<<TreeviewSelect>>", self.on_single_select)
        self.single_tree.bind("<Double-1>", self._on_single_double_click)
        self.single_tree.bind("<F2>", lambda _event: self._edit_selected_single_cell())

    def _build_batch_tab(self) -> None:
        self.batch_tab = ttk.Frame(self.nb, padding=12)
        ttk.Label(
            self.batch_tab,
            textvariable=self.batch_intro_var,
            style="Subtitle.TLabel",
            wraplength=1050,
        ).pack(anchor="w")

        self.batch_pane = ttk.Panedwindow(self.batch_tab, orient="horizontal")
        self.batch_pane.pack(fill="both", expand=True, pady=(9, 0))
        left_column = ttk.Frame(self.batch_pane, width=460)
        processing = ttk.Frame(
            self.batch_pane,
            style="Card.TFrame",
            padding=10,
            width=720,
        )
        self.batch_pane.add(left_column, weight=5)
        self.batch_pane.add(processing, weight=8)
        left_column.pack_propagate(False)
        processing.pack_propagate(False)
        self.batch_tab.after(150, self._fit_batch_sash)
        self.batch_pane.bind(
            "<Map>",
            lambda _event: self.batch_pane.after_idle(self._fit_batch_sash),
            add="+",
        )

        sources = ttk.Frame(left_column, style="Card.TFrame", padding=9)
        sources.pack(fill="both", expand=True)
        self.batch_source_host = ttk.Frame(sources, style="Card.TFrame")
        self.batch_source_host.pack(fill="both", expand=True)
        self.batch_sources = PacNodeSelectionEditor(
            self.batch_source_host,
            "处理范围",
            self.selected_pac_refs,
            self.describe_pac_ref,
        )
        self.batch_sources.pack(fill="both", expand=True)

        self.unpacked_batch_sources = PathScopeSelectionEditor(
            self.batch_source_host,
            "处理范围",
            self.selected_unpacked_paths,
        )

        mappings = ttk.Frame(left_column, style="Card.TFrame", padding=9)
        mappings.pack(fill="both", expand=True, pady=(9, 0))
        self.batch_mapping = MappingTable(mappings, self.checkbox_images)
        self.batch_mapping.pack(fill="both", expand=True)

        processing.columnconfigure(0, weight=1)
        processing.rowconfigure(2, weight=1)
        ttk.Label(processing, text="批量处理", style="Section.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        options = ttk.Frame(processing, style="Card.TFrame")
        options.grid(row=1, column=0, sticky="ew", pady=(7, 8))
        settings = ttk.Frame(options, style="Card.TFrame")
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="文件筛选").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        ttk.Entry(settings, textvariable=self.batch_globs_var, width=18).grid(
            row=0, column=1, sticky="w"
        )

        actions = CompactToolbar(options, style="Card.TFrame")
        actions.pack(fill="x", pady=(7, 0))
        actions.add("查找匹配", self.scan_batch)
        actions.add("替换勾选项", self.exec_batch)
        actions.add(
            text="全选",
            command=lambda: self._set_hit_checks(
                self.batch_hit_tree,
                self.batch_hits,
                True,
                selected_only=False,
            ),
        )
        actions.add(
            text="全不选",
            command=lambda: self._set_hit_checks(
                self.batch_hit_tree,
                self.batch_hits,
                False,
                selected_only=False,
            ),
        )
        actions.add("导入选择", self.import_batch_behavior)
        actions.add("导出选择", self.export_batch_behavior)
        actions.add("关闭", self.close_batch)

        self.batch_body_pane = ttk.Panedwindow(processing, orient="vertical")
        self.batch_body_pane.grid(row=2, column=0, sticky="nsew")
        results = ttk.Frame(self.batch_body_pane, style="Card.TFrame")
        self.batch_body_pane.add(results, weight=5)
        results.columnconfigure(0, weight=1)
        results.rowconfigure(0, weight=1)
        hits = ttk.Frame(results, style="Card.TFrame")
        hits.grid(row=0, column=0, sticky="nsew")
        self.batch_hit_tree = self._tree(
            hits,
            ("file", "kind", "location", "write", "old", "new"),
            {
                "file": "PAC 内文件",
                "kind": "类型",
                "location": "位置",
                "write": "写回能力",
                "old": "原文本",
                "new": "新文本",
            },
            {
                "file": 300,
                "kind": 65,
                "location": 170,
                "write": 105,
                "old": 330,
                "new": 330,
            },
            selectmode="extended",
            checkbox_tree=True,
        )
        self.batch_hit_tree.bind(
            "<Button-1>",
            lambda event: self._toggle_hit_from_event(
                event,
                self.batch_hit_tree,
                self.batch_hits,
            ),
        )
        self.batch_hit_tree.bind(
            "<<TreeviewSelect>>",
            lambda _event: self._schedule_batch_rich_cells(),
        )
        self.batch_hit_tree.bind("<Double-1>", self._on_batch_hit_double_click)
        for event_name in (
            "<Configure>",
            "<Map>",
            "<B1-Motion>",
            "<ButtonRelease-1>",
            "<MouseWheel>",
            "<KeyRelease>",
        ):
            self.batch_hit_tree.bind(
                event_name,
                lambda _event: self._schedule_batch_rich_cells(),
                add="+",
            )
        self.batch_hit_tree.configure(
            yscrollcommand=lambda first, last: self._set_batch_scrollbar(
                self.batch_hit_tree._tis_ybar,
                first,
                last,
            ),
            xscrollcommand=lambda first, last: self._set_batch_scrollbar(
                self.batch_hit_tree._tis_xbar,
                first,
                last,
            ),
        )
        self.batch_hit_tree.configure(displaycolumns=("file", "old", "new", "write"))
        for column, width in (("file", 170), ("old", 210), ("new", 210), ("write", 110)):
            self.batch_hit_tree.column(column, width=width, minwidth=90, stretch=True)
        diagnostics = ttk.Frame(
            self.batch_body_pane,
            style="Card.TFrame",
            padding=(0, 6, 0, 0),
        )
        self.batch_body_pane.add(diagnostics, weight=2)
        self.batch_tab.after(180, self._fit_batch_body_sash)
        self.batch_body_pane.bind(
            "<Map>",
            lambda _event: self.batch_body_pane.after_idle(
                self._fit_batch_body_sash
            ),
            add="+",
        )
        progress = ttk.Frame(diagnostics, style="Card.TFrame")
        progress.pack(fill="x")
        self.batch_progress = ttk.Progressbar(
            progress,
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.batch_progress.pack(fill="x", pady=(5, 2))
        ttk.Label(
            progress,
            textvariable=self.batch_progress_text_var,
            style="Muted.TLabel",
            justify="left",
        ).pack(anchor="w")
        self.batch_log = self._text(diagnostics, height=4, readonly=True)

    def _build_diff_tab(self) -> None:
        self.diff_tab = ttk.Frame(self.primary_nb, padding=12)
        self.primary_nb.add(self.diff_tab, text="版本对比")
        header = ttk.Frame(self.diff_tab, style="Card.TFrame", padding=10)
        header.pack(fill="x", pady=(0, 10))
        title_row = ttk.Frame(header, style="Card.TFrame")
        title_row.pack(fill="x")
        ttk.Label(title_row, text="独立版本对比", style="Section.TLabel").pack(
            side="left",
        )
        ttk.Label(
            title_row,
            textvariable=self.diff_mode_hint_var,
            style="Muted.TLabel",
        ).pack(side="left", padx=(14, 0))

        controls = ttk.Frame(header, style="Card.TFrame")
        controls.pack(fill="x", pady=(7, 0))
        ttk.Label(controls, text="文件筛选").pack(side="left")
        ttk.Entry(controls, textvariable=self.diff_globs_var, width=16).pack(
            side="left",
            padx=(6, 0),
        )
        ttk.Button(
            controls,
            text="开始对比",
            command=self.scan_diff,
            style="Accent.TButton",
        ).pack(side="right")
        ttk.Checkbutton(
            controls,
            text="显示无差异的文件",
            variable=self.diff_show_same_files_var,
            command=self.refresh_diff_files,
        ).pack(side="right", padx=(0, 10))
        self.diff_progress = ttk.Progressbar(
            header,
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.diff_progress.pack(fill="x", pady=(8, 2))
        ttk.Label(
            header,
            textvariable=self.diff_progress_text_var,
            style="Muted.TLabel",
            justify="left",
        ).pack(anchor="w")

        self.diff_columns = ttk.Panedwindow(self.diff_tab, orient="horizontal")
        self.diff_columns.pack(fill="both", expand=True)
        left_column = ttk.Frame(self.diff_columns)
        right_column = ttk.Frame(self.diff_columns)
        self.diff_columns.add(left_column, weight=5)
        self.diff_columns.add(right_column, weight=7)

        sources = ttk.Frame(left_column, style="Card.TFrame", padding=10)
        sources.pack(fill="x")
        source_columns = ttk.Panedwindow(sources, orient="horizontal")
        source_columns.pack(fill="both", expand=True)
        old_frame = ttk.Frame(source_columns, style="Card.TFrame")
        new_frame = ttk.Frame(source_columns, style="Card.TFrame")
        source_columns.add(old_frame, weight=1)
        source_columns.add(new_frame, weight=1)
        self.diff_old_sources = ComparisonSourceEditor(
            old_frame,
            "旧版本",
            self.current_resource_mode,
        )
        self.diff_old_sources.pack(fill="both", expand=True)
        self.diff_new_sources = ComparisonSourceEditor(
            new_frame,
            "新版本",
            self.current_resource_mode,
        )
        self.diff_new_sources.pack(fill="both", expand=True, padx=(10, 0))

        files = ttk.Frame(left_column, style="Card.TFrame", padding=10)
        files.pack(fill="both", expand=True, pady=(10, 0))
        ttk.Label(files, text="文件差异", style="Section.TLabel").pack(anchor="w")
        self.diff_file_tree = self._tree(
            files,
            ("status", "rel", "old", "new"),
            {"status": "状态", "rel": "文件 / PAC 内路径", "old": "旧大小", "new": "新大小"},
            {"status": 80, "rel": 350, "old": 90, "new": 90},
        )
        self.diff_file_tree.bind("<<TreeviewSelect>>", self.on_diff_select)

        entries = ttk.Frame(right_column, style="Card.TFrame", padding=10)
        entries.pack(fill="both", expand=True, padx=(10, 0))
        heading = ttk.Frame(entries, style="Card.TFrame")
        heading.pack(fill="x")
        ttk.Label(heading, text="文件条目差异", style="Section.TLabel").pack(
            side="left",
        )
        ttk.Checkbutton(
            heading,
            text="显示相同条目",
            variable=self.diff_show_all_var,
            command=self.refresh_diff_entries,
        ).pack(side="right")
        self.diff_entry_tree = self._tree(
            entries,
            ("index", "status", "old", "new"),
            {"index": "索引", "status": "状态", "old": "旧文本", "new": "新文本"},
            {"index": 110, "status": 80, "old": 340, "new": 340},
        )
        self._diff_text_cells = DiffTextCells(
            self.diff_entry_tree, lambda item: self.diff_entries_visible[int(item)],
        )

    def _build_integrity_tab(self) -> None:
        self.integrity_tab = ttk.Frame(self.nb, padding=12)
        self.nb.add(self.integrity_tab, text="扫描与修复")
        ttk.Label(
            self.integrity_tab,
            text="DAT 指针完整性扫描与参考修复",
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            self.integrity_tab,
            text=(
                "无参考源的只读扫描只能发现越界、乱码等显式无效指针；"
                "与可信参考 DAT/PAC 对照后，才能识别仍可解码但已经串到其他文本的静默错链。"
                "修复只重建可证明的指针关系，保留待修复文件的现有文本和 CLE 封装；"
                "批量处理中无法安全对齐的条目会单独跳过，其余条目继续修复。"
                "参考文件中不存在的全新增指针只能校验结构，不能凭旧参考证明其语义目标。"
            ),
            style="Muted.TLabel",
            wraplength=1180,
            justify="left",
        ).pack(anchor="w", pady=(4, 10))

        strategy_row = ttk.Frame(self.integrity_tab, style="Panel.TFrame", padding=8)
        strategy_row.pack(fill="x", pady=(0, 10))
        ttk.Label(strategy_row, text="对照/修复策略：").pack(side="left")
        strategy_combo = ttk.Combobox(
            strategy_row,
            textvariable=self.integrity_strategy_var,
            values=[label for label, _value in self.integrity_strategy_items],
            state="readonly",
            width=16,
        )
        strategy_combo.pack(side="left", padx=(4, 10))
        strategy_combo.bind(
            "<<ComboboxSelected>>",
            self.on_integrity_strategy_changed,
        )
        ttk.Label(
            strategy_row,
            text=(
                "激进策略会改写参考差异较大时仍可对齐、但缺少邻近损坏证据的有效指针；"
                "它能覆盖更多疑似错链，也可能撤销开发者主动改变的正确关系。"
            ),
            style="Muted.TLabel",
            wraplength=930,
            justify="left",
        ).pack(side="left", fill="x", expand=True)

        actions = ttk.Frame(self.integrity_tab)
        actions.pack(fill="x")
        single = ttk.Frame(actions, style="Card.TFrame", padding=10)
        batch = ttk.Frame(actions, style="Card.TFrame", padding=10)
        single.pack(side="left", fill="both", expand=True, padx=(0, 5))
        batch.pack(side="left", fill="both", expand=True, padx=(5, 0))
        ttk.Label(single, text="单文件 DAT", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            single,
            text="适用于已解包或单独导出的 #scp DAT。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 7))
        single_buttons = ttk.Frame(single, style="Card.TFrame")
        single_buttons.pack(fill="x")
        ttk.Button(
            single_buttons,
            text="只读扫描…",
            command=lambda: self.scan_single_dat_integrity(compare=False),
        ).pack(side="left")
        ttk.Button(
            single_buttons,
            text="与参考 DAT 对照…",
            command=lambda: self.scan_single_dat_integrity(compare=True),
        ).pack(side="left", padx=6)
        ttk.Button(
            single_buttons,
            text="修复并另存…",
            command=self.repair_single_dat_references,
        ).pack(side="left")

        ttk.Label(batch, text="批量（PAC 内全部 DAT）", style="Section.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            batch,
            text="逐条扫描脚本 PAC；参考修复始终另存完整 PAC。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 7))
        batch_buttons = ttk.Frame(batch, style="Card.TFrame")
        batch_buttons.pack(fill="x")
        ttk.Button(
            batch_buttons,
            text="加载目标 PAC…",
            command=self.load_integrity_target_pac,
        ).pack(side="left")
        ttk.Button(
            batch_buttons,
            text="加载参考 PAC…",
            command=self.load_integrity_reference_pac,
        ).pack(side="left", padx=6)
        ttk.Button(
            batch_buttons,
            text="对照分析",
            command=self.analyze_loaded_integrity_pacs,
        ).pack(side="left")
        ttk.Button(
            batch_buttons,
            text="尝试修复",
            command=self.repair_loaded_integrity_pacs,
        ).pack(side="left", padx=(6, 0))
        ttk.Label(
            batch,
            textvariable=self.integrity_target_pac_var,
            style="Muted.TLabel",
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            batch,
            textvariable=self.integrity_reference_pac_var,
            style="Muted.TLabel",
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        progress = ttk.Frame(self.integrity_tab, style="Panel.TFrame", padding=8)
        progress.pack(fill="x", pady=(10, 0))
        self.integrity_progress = ttk.Progressbar(
            progress,
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.integrity_progress.pack(fill="x")
        ttk.Label(
            progress,
            textvariable=self.integrity_status_var,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        log = ttk.Frame(self.integrity_tab, style="Card.TFrame", padding=8)
        log.pack(fill="both", expand=True, pady=(10, 0))
        ttk.Label(log, text="扫描报告", style="Section.TLabel").pack(anchor="w")
        self.integrity_log = self._text(log, height=18, readonly=True)
        self._set_text(
            self.integrity_log,
            "请选择单 DAT 或脚本 PAC 开始扫描。建议先只读扫描，再提供可信参考源进行对照。",
            readonly=True,
        )

    def _build_workspace_tab(self) -> None:
        self.workspace_tab = ttk.Frame(self.primary_nb, padding=12)
        self.primary_nb.add(self.workspace_tab, text="缓存管理")
        ttk.Label(
            self.workspace_tab,
            textvariable=self.pac_data_root_var,
            style="Subtitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            self.workspace_tab,
            text=(
                "当前编辑使用独立的会话缓存：关闭 PAC 或程序时自动销毁，"
                "下次打开一定从源 PAC 重新提取。下表只管理旧版本遗留的持久工作区；"
                "即使 PAC 无法加载，也可按源 PAC 清除其历史缓存。"
            ),
            style="Muted.TLabel",
            wraplength=980,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))
        actions = ttk.Frame(self.workspace_tab, style="Panel.TFrame", padding=9)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="刷新", command=self.refresh_pac_cache).pack(side="left")
        ttk.Button(
            actions,
            text="清理安全工作区",
            command=self.clean_safe_pac_workspaces,
        ).pack(side="left", padx=6)
        ttk.Button(
            actions,
            text="清除所选工作区缓存",
            command=self.delete_selected_pac_workspace,
        ).pack(side="left")
        ttk.Button(
            actions,
            text="按源 PAC 清除全部缓存…",
            command=self.clear_pac_cache_by_source,
        ).pack(side="left", padx=6)
        ttk.Button(
            actions,
            text="按参考 PAC 修复 DAT 指针…",
            command=self.repair_pac_dat_references,
        ).pack(side="left")
        ttk.Button(actions, text="打开数据目录", command=self.open_pac_data_root).pack(
            side="left",
            padx=6,
        )

        split = ttk.Panedwindow(self.workspace_tab, orient="vertical")
        split.pack(fill="both", expand=True, pady=(8, 0))
        cache = ttk.Frame(split, style="Panel.TFrame", padding=8)
        runtime = ttk.Frame(split, style="Panel.TFrame", padding=8)
        split.add(cache, weight=5)
        split.add(runtime, weight=3)
        ttk.Label(cache, text="历史 PAC 工作区缓存（旧版本遗留）").pack(anchor="w")
        self.pac_cache_tree = self._tree(
            cache,
            ("source", "state", "entries", "dirty", "materialized", "size"),
            {
                "source": "源 PAC",
                "state": "状态",
                "entries": "条目",
                "dirty": "修改",
                "materialized": "已缓存",
                "size": "占用",
            },
            {
                "source": 520,
                "state": 95,
                "entries": 70,
                "dirty": 70,
                "materialized": 80,
                "size": 100,
            },
            selectmode="extended",
        )
        runtime_actions = ttk.Frame(runtime)
        runtime_actions.pack(fill="x")
        ttk.Label(runtime_actions, text="运行缓存").pack(side="left")
        ttk.Button(runtime_actions, text="刷新", command=self.refresh_runtime).pack(
            side="right",
        )
        ttk.Button(
            runtime_actions,
            text="清理临时缓存",
            command=self.clean_runtime,
        ).pack(side="right", padx=6)
        self.runtime_meta = ttk.Label(runtime, text="", style="Subtitle.TLabel")
        self.runtime_meta.pack(anchor="w", pady=(6, 4))
        self.runtime_list = tk.Listbox(runtime, height=5, bg="#fffaf1", fg="#2c2218")
        self.runtime_list.pack(fill="both", expand=True)

        ttk.Checkbutton(
            self.workspace_tab,
            text="显示原始 PAC 工具回退方案（不默认使用）",
            variable=self.pac_fallback_var,
            command=self._toggle_pac_fallback,
        ).pack(anchor="w", pady=(10, 0))
        self.fallback_frame = ttk.Frame(
            self.workspace_tab,
            style="Panel.TFrame",
            padding=9,
        )
        ttk.Label(
            self.fallback_frame,
            text="仅在内置解析/构建失败时手动使用。回退输出也会先经过校验。",
            style="Subtitle.TLabel",
        ).pack(side="left")
        ttk.Button(
            self.fallback_frame,
            text="回退解包",
            command=self.fallback_extract_pac,
        ).pack(side="right")
        ttk.Button(
            self.fallback_frame,
            text="回退构建",
            command=self.fallback_build_pac,
        ).pack(side="right", padx=6)

    def _tree(
        self,
        parent,
        columns,
        headings,
        widths,
        selectmode="browse",
        *,
        checkbox_tree: bool = False,
        adaptive: bool = False,
    ) -> ttk.Treeview | AdaptiveTextTable:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = (AdaptiveTextTable(frame, columns=columns) if adaptive else ttk.Treeview(
            frame, columns=columns,
            show="tree headings" if checkbox_tree else "headings", selectmode=selectmode,
        ))
        if checkbox_tree:
            tree.heading("#0", text="勾选")
            tree.column("#0", width=56, minwidth=56, stretch=False, anchor="center")
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor="w")
        ybar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree._tis_ybar = ybar
        tree._tis_xbar = xbar
        tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _text(self, parent, height=10, readonly=False) -> tk.Text:
        widget = tk.Text(
            parent,
            height=height,
            wrap="word",
            undo=not readonly,
            font="TkTextFont",
            bg="#ffffff",
            fg="#202b3b",
            insertbackground="#202b3b",
            relief="flat",
            padx=8,
            pady=6,
        )
        widget.pack(fill="both", expand=True, pady=(4, 0))
        if readonly:
            widget.configure(state="disabled")
        return widget

    def _build_checkbox_images(self) -> dict[bool, tk.PhotoImage]:
        return {
            False: self._make_checkbox_image(False),
            True: self._make_checkbox_image(True),
        }

    def _make_checkbox_image(self, checked: bool) -> tk.PhotoImage:
        image = tk.PhotoImage(width=16, height=16)
        for x in range(2, 14):
            image.put("#5b4734", (x, 2))
            image.put("#5b4734", (x, 13))
        for y in range(2, 14):
            image.put("#5b4734", (2, y))
            image.put("#5b4734", (13, y))
        if checked:
            for x, y in ((4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7), (10, 6), (11, 5)):
                image.put("#2c2218", (x, y))
                image.put("#2c2218", (x, y + 1))
        return image

    def apply_ui_scale_choice(self) -> None:
        scale = self._parse_scale(self.ui_scale_var.get())
        self._apply_ui_scale(scale)
        self.status_var.set(f"界面缩放已调整为 {int(scale * 100)}%。")

    def _on_window_dpi_change(self, event) -> None:
        if event.widget is not self.root or self._dpi_refresh_pending is not None:
            return
        self._dpi_refresh_pending = self.root.after_idle(self._refresh_window_dpi)

    def _refresh_window_dpi(self) -> None:
        self._dpi_refresh_pending = None
        scaling = window_tk_scaling(self.root)
        if abs(scaling - self._native_tk_scaling) < 0.02:
            return
        self._native_tk_scaling = scaling
        self._apply_ui_scale(self._parse_scale(self.ui_scale_var.get()))

    @staticmethod
    def _parse_scale(label: str) -> float:
        try:
            return max(0.67, min(2.0, int(label.rstrip("%")) / 100))
        except Exception:
            return 1.0

    def _apply_ui_scale(self, scale: float) -> None:
        family = "Microsoft YaHei UI"
        base = max(7, round(11 * scale))
        apply_theme(self.root, self.style, self._native_tk_scaling, scale)
        for table_name in ("preview_tree", "single_tree"):
            table = getattr(self, table_name, None)
            if isinstance(table, AdaptiveTextTable):
                table.refresh_metrics()
        if hasattr(self, "batch_match_font"):
            self.batch_match_font.configure(
                family=family,
                size=base,
                weight="bold",
            )
        self.diff_normal_font = tkfont.Font(family=family, size=base)
        self.diff_em_font = tkfont.Font(
            family=family,
            size=base,
            weight="bold",
            slant="italic",
        )
        self._configure_diff_tags()
        if hasattr(self, "_batch_excerpt_cache"):
            self._batch_excerpt_cache.clear()
        self._schedule_batch_rich_cells()
        def refresh_toolbars(widget):
            for child in widget.winfo_children():
                if isinstance(child, CompactToolbar):
                    child._schedule()
                refresh_toolbars(child)
        refresh_toolbars(self.root)
        font = tkfont.nametofont("TkDefaultFont")
        if hasattr(self, "pac_tree"):
            self.pac_tree.column("state", width=font.measure("已修改") + 24, stretch=False)
            self.pac_tree.column("size", width=font.measure("999.9 MB") + 16, stretch=False)
        if hasattr(self, "single_tree"):
            self.single_tree.column("index", width=font.measure("00000") + 24, stretch=False)
        if hasattr(self, "batch_hit_tree"):
            self.batch_hit_tree.column("#0", width=font.measure("勾选") + 24, stretch=False)
        if hasattr(self, "main_pane"):
            self.root.after_idle(lambda: self._set_main_sash(330))
            self._batch_sash_initialized = False
            self.batch_pane.after_idle(self._fit_batch_sash)
            self._batch_body_sash_initialized = False
            self.batch_body_pane.after_idle(self._fit_batch_body_sash)

    def _layout_scale(self) -> float:
        return self._native_tk_scaling / (96 / 72) * self._parse_scale(self.ui_scale_var.get())

    def _set_features_active(self, active: bool) -> None:
        if active == self.features_active:
            self._schedule_main_sash_guard()
            return
        if active:
            if str(self.welcome_tab) in self.nb.tabs():
                self.nb.forget(self.welcome_tab)
            for frame, label in self.feature_tabs:
                self.nb.add(frame, text=label)
            self.nb.insert("end", self.integrity_tab)
            self.nb.select(self.preview_tab)
        else:
            for frame, _label in self.feature_tabs:
                if str(frame) in self.nb.tabs():
                    self.nb.forget(frame)
            if str(self.welcome_tab) not in self.nb.tabs():
                self.nb.add(self.welcome_tab, text="开始")
            self.nb.insert(0, self.welcome_tab)
            self.nb.insert("end", self.integrity_tab)
            self.nb.select(self.welcome_tab)
        self.features_active = active
        self.root.after_idle(lambda: self._set_main_sash(330))

    def current_resource_mode(self) -> str:
        return self.resource_mode_label_to_value.get(
            self.resource_mode_var.get(),
            "pac",
        )

    def current_game_version(self) -> GameVersion:
        return GameVersion.normalize(
            self.game_version_label_to_value.get(
                self.game_version_var.get(),
                GameVersion.AUTO.value,
            )
        )

    def on_game_version_changed(self, _event=None) -> None:
        selected = self.current_game_version()
        if selected.value == self._previous_game_version:
            return
        previous_value = self._previous_game_version
        previous_label = self.game_version_value_to_label[previous_value]
        if not self._ensure_idle() or not self._confirm_pending_changes():
            self.game_version_var.set(previous_label)
            return

        self._previous_game_version = selected.value
        self.batch_hits = []
        self.batch_scan_options = None
        self.batch_scan_complete = False
        self._refresh_hit_tree(self.batch_hit_tree, self.batch_hits)
        self._reset_task_progress(
            self.batch_progress,
            self.batch_progress_text_var,
            "游戏版本已改变，请重新扫描批量范围。",
        )
        self.route_hint_var.set(
            self._route_message(
                Path(self.current_pac_entry or self.current_file or "")
            )
        )
        self.batch_backend_hint_var.set(self._route_message())

        try:
            self._reload_current_for_parsing_options()
            self._reload_preview_for_parsing_options()
        except Exception as exc:
            self._previous_game_version = previous_value
            self.game_version_var.set(previous_label)
            self.route_hint_var.set(self._route_message())
            self.batch_backend_hint_var.set(self._route_message())
            try:
                self._reload_current_for_parsing_options()
                self._reload_preview_for_parsing_options()
            except Exception:
                pass
            messagebox.showerror(
                "切换游戏版本失败",
                f"无法按新版本重新解析当前文件：{exc}",
                parent=self.root,
            )
            return
        label = self.game_version_var.get()
        self.status_var.set(f"游戏版本已切换为“{label}”；批量结果已清空，请重新扫描。")

    def _reload_current_for_parsing_options(self) -> None:
        if self.session.document is None:
            return
        if self.current_origin == "pac" and self.current_project_id and self.current_pac_entry:
            project = self.workbench.get(self.current_project_id)
            document = project.open_entry(
                self.current_pac_entry,
                options=self._resolve_options(Path(self.current_pac_entry)),
            )
            self.session = project.document_session
            self.current_file = project.workspace.entry_path(self.current_pac_entry)
            source_label = project.workspace.archive.source_path.name
            logical_path = self.current_pac_entry
        elif self.current_origin == "unpacked" and self.current_file is not None:
            session = DocumentSession()
            document = session.open_document(
                self.current_file,
                options=self._resolve_options(self.current_file, for_pac=False),
            )
            self.session = session
            source_label = str(self.current_file)
            logical_path = self.current_file.name
        else:
            return
        self._load_document_into_single_view(document)
        self.single_meta_var.set(
            f"{source_label}  ::  {logical_path}"
            f"　·　{len(document.units)} 条文本"
        )

    def _reload_preview_for_parsing_options(self) -> None:
        selection = self.preview_selection
        if selection is None or not is_text_document_path(selection.logical_path):
            return
        if selection.origin == "unpacked" and selection.file_path is not None:
            self._preview_unpacked_file(selection.file_path)
            return
        if selection.pac_ref is None:
            return
        ref = selection.pac_ref
        project = self.workbench.get(ref.workspace_id)
        target = self.workbench.materialize_refs([ref], editable_only=True)[0]
        preview_session = DocumentSession(self.business.service)
        options = self._resolve_options(Path(target.entry_name))
        if Path(target.entry_name).suffix.lower() == ".tbl" and not options.schema_hint:
            options.schema_hint = Path(target.entry_name).stem
        document = preview_session.open_document(
            target.path,
            options=options,
        )
        self._render_document_preview(
            project.workspace.archive.source_path.name,
            target.entry_name,
            document,
        )

    def on_resource_mode_changed(self, _event=None) -> None:
        new_mode = self.current_resource_mode()
        if new_mode == self._previous_resource_mode:
            return
        if not self._ensure_idle():
            self.resource_mode_var.set(
                self.resource_mode_value_to_label[self._previous_resource_mode]
            )
            return
        if not self._confirm_pending_changes():
            self.resource_mode_var.set(
                self.resource_mode_value_to_label[self._previous_resource_mode]
            )
            return
        self._clear_single_document_view()
        self.batch_hits = []
        self.batch_scan_options = None
        self.batch_scan_complete = False
        self._refresh_hit_tree(self.batch_hit_tree, self.batch_hits)
        self._apply_diff_files([])
        self._reset_task_progress(
            self.batch_progress,
            self.batch_progress_text_var,
            "尚未开始批量任务。",
        )
        self._reset_task_progress(
            self.diff_progress,
            self.diff_progress_text_var,
            "尚未开始版本对比。",
        )
        self.pac_compare.close()
        self.pac_compare = PacComparisonSession()
        self._set_preview_selection(None)
        self.diff_old_sources.set_mode(new_mode)
        self.diff_new_sources.set_mode(new_mode)

        if new_mode == "pac":
            self.unpacked_explorer_frame.pack_forget()
            self.pac_explorer_frame.pack(fill="both", expand=True)
            self.pac_toolbar.pack(side="left")
            self.unpacked_batch_sources.pack_forget()
            self.batch_sources.pack(fill="both", expand=True)
            self.batch_intro_var.set(
                "选择范围 → 查找匹配 → 替换勾选项 → 导出 PAC。"
            )
            self.batch_hit_tree.heading("file", text="PAC 内文件")
            self.preview_meta_var.set("请在左侧 PAC 文件树中选择文件。")
            self.preview_hint_var.set("如需修改，请双击左侧文件进入“单文件”。")
            self.single_save_button.configure(text="保存修改")
            self.single_export_button.configure(text="导出当前文件副本")
            self.diff_mode_hint_var.set(
                "PAC 模式：选择新旧 PAC 文件，或包含 PAC 的目录组。"
            )
            self._set_features_active(bool(self.workbench.projects()))
            self.status_var.set("已切换到 PAC 模式。")
        else:
            self.pac_explorer_frame.pack_forget()
            self.unpacked_explorer_frame.pack(fill="both", expand=True)
            self.pac_toolbar.pack_forget()
            self.batch_sources.pack_forget()
            self.unpacked_batch_sources.pack(fill="both", expand=True)
            self.batch_intro_var.set(
                "每行代表一次替换；执行成功后直接写回普通 TBL/DAT，并保留一次备份。"
            )
            self.batch_hit_tree.heading("file", text="文件")
            self.preview_meta_var.set("请在左侧解包文件树中选择文件。")
            self.preview_hint_var.set("如需修改，请双击左侧文件进入“单文件”。")
            self.single_save_button.configure(text="保存修改")
            self.single_export_button.configure(text="另存为")
            self.diff_mode_hint_var.set(
                "解包模式：选择新旧目录，或直接选择 TBL/DAT 文件。"
            )
            self._set_features_active(True)
            self.status_var.set("已切换到解包模式。")
        self._render_preview_message("请从左侧文件树中选择文件。")
        self._previous_resource_mode = new_mode
        self.root.after(120, lambda: self._set_main_sash(330))

    def add_unpacked_files(self) -> None:
        selected = filedialog.askopenfilenames(
            title="添加可预览文件",
            filetypes=[
                ("支持的资源", "*.tbl *.dat *.png *.dds *.wav *.webm *.fnt *.mdl *.mi"),
                ("文本资源", "*.tbl *.dat"),
                ("图片", "*.png *.dds"),
                ("音频", "*.wav"),
                ("视频", "*.webm"),
                ("字体", "*.fnt"),
                ("模型", "*.mdl *.mi"),
                ("TBL 文件", "*.tbl"),
                ("DAT 文件", "*.dat"),
                ("所有文件", "*.*"),
            ],
        )
        self._add_unpacked_roots(selected)

    def add_unpacked_folder(self) -> None:
        selected = filedialog.askdirectory(title="添加解包目录")
        if selected:
            self._add_unpacked_roots([selected])

    def _add_unpacked_roots(self, paths) -> None:
        existing = set(self.unpacked_roots)
        for raw in paths:
            path = Path(raw).resolve()
            if (
                path.exists()
                and path not in existing
                and (path.is_dir() or self._is_previewable_file(path))
            ):
                self.unpacked_roots.append(path)
                existing.add(path)
        self.refresh_unpacked_tree()

    def remove_unpacked_roots(self) -> None:
        roots_to_remove: set[Path] = set()
        for item in self.unpacked_tree.selection():
            top = item
            while self.unpacked_tree.parent(top):
                top = self.unpacked_tree.parent(top)
            path = self.unpacked_tree_paths.get(top)
            if path is not None:
                roots_to_remove.add(path)
        self.unpacked_roots = [
            path for path in self.unpacked_roots if path not in roots_to_remove
        ]
        self.refresh_unpacked_tree()

    def refresh_unpacked_tree(self) -> None:
        self.unpacked_tree.delete(*self.unpacked_tree.get_children())
        self.unpacked_tree_paths.clear()
        for root in self.unpacked_roots:
            if not root.exists():
                continue
            if root.is_file():
                item = self.unpacked_tree.insert(
                    "",
                    "end",
                    text=root.name,
                    values=(root.suffix.lstrip(".").upper(), self._format_bytes(root.stat().st_size)),
                )
                self.unpacked_tree_paths[item] = root
                continue
            root_item = self.unpacked_tree.insert(
                "",
                "end",
                text=root.name,
                open=True,
                values=("目录", ""),
            )
            self.unpacked_tree_paths[root_item] = root
            folders: dict[str, str] = {}
            files = sorted(
                path
                for path in root.rglob("*")
                if path.is_file() and self._is_previewable_file(path)
            )
            for path in files:
                relative = path.relative_to(root)
                parent = root_item
                parts: list[str] = []
                for part in relative.parts[:-1]:
                    parts.append(part)
                    key = "/".join(parts)
                    folder_item = folders.get(key)
                    if folder_item is None:
                        folder_item = self.unpacked_tree.insert(
                            parent,
                            "end",
                            text=part,
                            values=("目录", ""),
                        )
                        folders[key] = folder_item
                        self.unpacked_tree_paths[folder_item] = root.joinpath(*parts)
                    parent = folder_item
                item = self.unpacked_tree.insert(
                    parent,
                    "end",
                    text=path.name,
                    values=(
                        path.suffix.lstrip(".").upper(),
                        self._format_bytes(path.stat().st_size),
                    ),
                )
                self.unpacked_tree_paths[item] = path

    def selected_unpacked_paths(self) -> list[str]:
        return [
            str(self.unpacked_tree_paths[item])
            for item in self.unpacked_tree.selection()
            if item in self.unpacked_tree_paths
        ]

    def on_unpacked_tree_select(self, _event=None) -> None:
        if self._busy:
            return
        paths = [Path(path) for path in self.selected_unpacked_paths()]
        if len(paths) != 1:
            if paths:
                self.status_var.set(
                    f"已选择 {len(paths)} 个节点；当前文件预览保持不变。"
                )
            return
        path = paths[0]
        if path.is_dir():
            count = sum(
                1
                for child in path.rglob("*")
                if child.is_file() and self._is_previewable_file(child)
            )
            self.status_var.set(
                f"{path} 包含 {count} 个可预览文件；当前文件预览保持不变。"
            )
            return
        self._preview_unpacked_file(path)

    def on_unpacked_tree_double_click(self, event) -> None:
        if not self._ensure_idle():
            return
        item = self.unpacked_tree.identify_row(event.y)
        path = self.unpacked_tree_paths.get(item)
        if path is not None and path.is_file():
            if is_text_document_path(path):
                self._open_unpacked_file(path)
            else:
                self._preview_unpacked_file(path)

    def _preview_unpacked_file(self, path: Path) -> None:
        self._set_preview_selection(
            PreviewFileSelection(
                origin="unpacked",
                logical_path=path.name,
                size=path.stat().st_size,
                file_path=path.resolve(),
            )
        )
        if path.suffix.lower() in MEDIA_SUFFIXES:
            self._start_media_preview(self.preview_selection)
            return
        try:
            preview_session = DocumentSession(self.business.service)
            document = preview_session.open_document(
                path,
                options=self._resolve_options(path, for_pac=False),
            )
            self._render_document_preview(path.parent.name, path.name, document)
            self.nb.select(self.preview_tab)
        except Exception as exc:
            self.preview_meta_var.set(str(path))
            self._render_preview_message(f"无法生成预览：\n{exc}")
            self.nb.select(self.preview_tab)

    def _open_unpacked_file(self, path: Path) -> None:
        if self.current_origin == "unpacked" and self.current_file == path.resolve():
            self.nb.select(self.single_tab)
            return
        if not self._confirm_pending_changes():
            return
        self._close_preview_playback()
        try:
            session = DocumentSession()
            document = session.open_document(
                path,
                options=self._resolve_options(path, for_pac=False),
            )
            self.session = session
            self.current_origin = "unpacked"
            self.current_project_id = None
            self.current_pac_entry = None
            self.current_file = path.resolve()
            self.current_index = None
            self._load_document_into_single_view(document)
            self.single_meta_var.set(
                f"{path.name}　·　{len(document.units)} 条文本"
            )
            self.route_hint_var.set(self._route_message(path, document.engine))
            self.nb.select(self.single_tab)
            self.status_var.set(f"已进入单文件编辑：{path.name}")
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    def open_pacs(self) -> None:
        if not self._ensure_idle():
            return
        paths = filedialog.askopenfilenames(
            title="打开一个或多个 PAC",
            filetypes=[("PAC 文件", "*.pac"), ("所有文件", "*.*")],
        )
        if not paths:
            return

        def path_key(value: str | Path) -> str:
            return os.path.normcase(str(Path(value).resolve()))

        open_before = {
            path_key(project.workspace.archive.source_path): project.workspace.workspace_id
            for project in self.workbench.projects()
        }
        replacing_ids = {
            open_before[path_key(raw_path)]
            for raw_path in paths
            if path_key(raw_path) in open_before
        }
        if self.current_project_id in replacing_ids and not self._confirm_pending_changes():
            return
        dirty_replacing = [
            self.workbench.get(workspace_id).workspace.archive.source_path.name
            for workspace_id in replacing_ids
            if self.workbench.get(workspace_id).workspace.state() == "stale"
            and self.workbench.get(workspace_id).workspace.dirty_entry_names()
        ]
        if dirty_replacing and not messagebox.askyesno(
            "重新打开已变化的 PAC",
            (
                "以下 PAC 的源文件已经变化，重新打开会永久销毁当前会话中"
                "尚未回包的修改：\n"
                + "\n".join(dirty_replacing)
                + "\n\n是否继续？"
            ),
            parent=self.root,
        ):
            return
        preview_ref = (
            self.preview_selection.pac_ref
            if self.preview_selection is not None
            and self.preview_selection.origin == "pac"
            else None
        )
        if preview_ref is not None and preview_ref.workspace_id in replacing_ids:
            self._close_preview_playback()

        def worker():
            opened: list[str] = []
            rotated: list[tuple[str, str, str]] = []
            errors: list[str] = []
            for raw_path in paths:
                try:
                    project = self.workbench.open(raw_path)
                    opened.append(str(project.workspace.archive.source_path))
                    old_id = open_before.get(path_key(raw_path))
                    new_id = project.workspace.workspace_id
                    if old_id is not None and old_id != new_id:
                        rotated.append((str(raw_path), old_id, new_id))
                except Exception as exc:
                    errors.append(f"{Path(raw_path).name}：{exc}")
            return opened, rotated, errors

        def completed(result) -> None:
            opened, rotated, errors = result
            rotated_old_ids = {old_id for _path, old_id, _new_id in rotated}
            if self.current_project_id in rotated_old_ids:
                self._clear_single_document_view()
            if rotated:
                self.batch_hits = []
                self.batch_scan_options = None
                self.batch_scan_complete = False
                self._refresh_hit_tree(self.batch_hit_tree, self.batch_hits)
            self.refresh_pac_tree()
            self.refresh_pac_cache()
            self._set_features_active(bool(self.workbench.projects()))
            self.root.after_idle(lambda: self._set_main_sash(330))
            if errors:
                messagebox.showwarning(
                    "部分 PAC 未能打开",
                    "\n".join(errors),
                    parent=self.root,
                )
            if rotated:
                messagebox.showinfo(
                    "已建立新的 PAC 工作区",
                    (
                        f"检测到 {len(rotated)} 个源 PAC 的内容发生变化，已按当前磁盘内容"
                        "自动建立并载入新的会话工作区。\n\n"
                        "旧的会话缓存已销毁，不会继承到当前 PAC。"
                    ),
                    parent=self.root,
                )
            self.status_var.set(
                f"已打开 {len(opened)} 个 PAC；自动换代 {len(rotated)} 个；"
                f"当前共 {len(self.workbench.projects())} 个。"
            )
            self._refresh_pac_dependent_preview()

        self._run_background("正在解析 PAC…", worker, completed)

    def refresh_pac_tree(self) -> None:
        self._suspend_tree_preview = True
        expanded_keys = {ref.key for item, ref in self.pac_tree_refs.items()
                         if self.pac_tree.exists(item) and self.pac_tree.item(item, "open")}
        old_scroll = self.pac_tree.yview()
        selected_keys = {
            self.pac_tree_refs[item].key
            for item in self.pac_tree.selection()
            if item in self.pac_tree_refs
        }
        self.pac_tree.delete(*self.pac_tree.get_children())
        self.pac_tree_refs.clear()
        self._reset_pac_search_cycle()
        restore: list[str] = []
        project_count = 0
        entry_count = 0
        for project in self.workbench.projects():
            project_count += 1
            workspace = project.workspace
            archive = workspace.archive
            entry_count += len(workspace.entries())
            root_ref = PacNodeRef(workspace.workspace_id, "pac")
            root_item = self.pac_tree.insert(
                "",
                "end",
                text=archive.source_path.name,
                open=True,
                values=(
                    "PAC",
                    self._format_bytes(archive.source_size),
                    self._pac_state_label(workspace.state()),
                ),
            )
            self.pac_tree_refs[root_item] = root_ref
            if root_ref.key in selected_keys:
                restore.append(root_item)
            folder_items: dict[str, str] = {}
            for entry in sorted(workspace.entries(), key=lambda e: e.name.casefold()):
                parts = [part for part in entry.name.replace("\\", "/").split("/") if part]
                parent_item = root_item
                folders: list[str] = []
                for part in parts[:-1]:
                    folders.append(part)
                    folder_path = "/".join(folders)
                    item = folder_items.get(folder_path)
                    if item is None:
                        ref = PacNodeRef(workspace.workspace_id, "folder", folder_path)
                        item = self.pac_tree.insert(
                            parent_item,
                            "end",
                            text=part,
                            open=ref.key in expanded_keys,
                            values=("目录", "", ""),
                        )
                        folder_items[folder_path] = item
                        self.pac_tree_refs[item] = ref
                        if ref.key in selected_keys:
                            restore.append(item)
                    parent_item = item
                ref = PacNodeRef(workspace.workspace_id, "file", entry.name)
                item = self.pac_tree.insert(
                    parent_item,
                    "end",
                    text=parts[-1] if parts else entry.name,
                    values=(
                        entry.suffix.lstrip(".").upper() or "文件",
                        self._format_bytes(workspace.current_size(entry.name)),
                        self._pac_entry_state_label(workspace.entry_state(entry.name)),
                    ),
                )
                self.pac_tree_refs[item] = ref
                if ref.key in selected_keys:
                    restore.append(item)
        if restore:
            self.pac_tree.selection_set(restore)
        if old_scroll:
            self.pac_tree.yview_moveto(old_scroll[0])
        # Treeview selection notifications are queued. Keep restoration muted
        # until they have drained, so finishing a search cannot switch to Preview.
        self.root.after_idle(lambda: setattr(self, "_suspend_tree_preview", False))
        self.pac_meta_var.set(
            f"已打开 {project_count} 个 PAC，共 {entry_count} 个文件。"
            if project_count
            else "尚未打开 PAC。"
        )
        valid_ids = {project.workspace.workspace_id for project in self.workbench.projects()}
        if (
            self.preview_selection is not None
            and self.preview_selection.origin == "pac"
            and (
                self.preview_selection.pac_ref is None
                or self.preview_selection.pac_ref.workspace_id not in valid_ids
            )
        ):
            self._set_preview_selection(None)
            self._render_preview_message(
                "当前预览来源已经关闭，请从文件树中重新选择文件。"
            )
        for editor in (
            getattr(self, "batch_sources", None),
        ):
            if editor is not None:
                editor.prune(valid_ids)
        if not project_count:
            self._render_preview_message("请先打开 PAC，然后从文件树中选择文件。")
        if hasattr(self, "pac_files_tab"):
            self.pac_files_tab.refresh()

    def selected_pac_refs(self) -> list[PacNodeRef]:
        return [
            self.pac_tree_refs[item]
            for item in self.pac_tree.selection()
            if item in self.pac_tree_refs
        ]

    @staticmethod
    def _matching_pac_file_items(
        refs: dict[str, PacNodeRef],
        keyword: str,
    ) -> list[tuple[str, PacNodeRef]]:
        normalized = keyword.strip().casefold()
        if not normalized:
            return []
        return [
            (item, ref)
            for item, ref in refs.items()
            if ref.kind == "file"
            and normalized
            in Path(ref.path.replace("\\", "/")).name.casefold()
        ]

    def _reset_pac_search_cycle(self) -> None:
        self._pac_search_last_query = ""
        self._pac_search_last_match_keys = ()
        self._pac_search_cursor = -1
        if hasattr(self, "pac_search_status_var"):
            self.pac_search_status_var.set("选择文件名 / 正文，↑↓ 循环查找。")

    def find_next_pac_file(self, _event=None, *, direction=1) -> None:
        if getattr(self, "_busy", False):
            return
        keyword = self.pac_search_var.get().strip()
        if not keyword:
            self._reset_pac_search_cycle()
            self.pac_search_status_var.set("请输入要查找的文件名。")
            return
        matches = self._matching_pac_file_items(self.pac_tree_refs, keyword)
        if not matches:
            self._reset_pac_search_cycle()
            self.pac_search_status_var.set(f"没有找到包含“{keyword}”的文件名。")
            return

        normalized = keyword.casefold()
        match_keys = tuple(ref.key for _item, ref in matches)
        same_cycle = (
            normalized == self._pac_search_last_query
            and match_keys == self._pac_search_last_match_keys
        )
        wrapped = False
        if same_cycle:
            raw_cursor = self._pac_search_cursor + direction
            wrapped = not 0 <= raw_cursor < len(matches)
            cursor = raw_cursor % len(matches)
        else:
            cursor = 0 if direction > 0 else len(matches) - 1
        self._pac_search_last_query = normalized
        self._pac_search_last_match_keys = match_keys
        self._pac_search_cursor = cursor

        item, ref = matches[cursor]
        parent = self.pac_tree.parent(item)
        while parent:
            self.pac_tree.item(parent, open=True)
            parent = self.pac_tree.parent(parent)
        self.pac_tree.selection_set(item)
        self.pac_tree.focus(item)
        self.pac_tree.see(item)
        prefix = ("已循环至首个匹配；" if direction > 0 else "已循环至末个匹配；") if wrapped else ""
        filename = Path(ref.path.replace("\\", "/")).name
        self.pac_search_status_var.set(
            f"{prefix}第 {cursor + 1}/{len(matches)} 个：{filename}"
        )
        self.status_var.set(f"PAC 文件搜索已定位：{ref.path}")

    def describe_pac_ref(self, ref: PacNodeRef) -> str:
        try:
            name = self.workbench.get(ref.workspace_id).workspace.archive.source_path.name
        except KeyError:
            name = "<已关闭 PAC>"
        if ref.kind == "pac":
            return f"{name}  [整个 PAC]"
        suffix = "/" if ref.kind == "folder" else ""
        return f"{name}  ::  {ref.path}{suffix}"

    def on_pac_tree_select(self, _event=None) -> None:
        if self._suspend_tree_preview:
            return
        if self._busy:
            self.status_var.set("当前任务运行中；当前文件预览保持不变。")
            return
        refs = self.selected_pac_refs()
        if not refs:
            return
        if len(refs) > 1:
            try:
                files = self.workbench.expand_refs(refs, editable_only=False)
                editable = sum(1 for _project, entry in files if entry.editable)
                self.status_var.set(
                    f"已选择 {len(refs)} 个节点，展开后包含 {len(files)} 个文件，"
                    f"其中 {editable} 个可编辑文本文件；当前文件预览保持不变。"
                )
            except Exception as exc:
                self.status_var.set(str(exc))
            return
        ref = refs[0]
        if ref.kind != "file":
            files = self.workbench.expand_refs([ref], editable_only=False)
            editable = sum(1 for _project, entry in files if entry.editable)
            self.status_var.set(
                f"{self.describe_pac_ref(ref)}：包含 {len(files)} 个文件，"
                f"{editable} 个可编辑 TBL/DAT；当前文件预览保持不变。"
            )
            return
        try:
            project = self.workbench.get(ref.workspace_id)
            entry = project.workspace.get_entry(ref.path)
            current_size = project.workspace.current_size(entry.name)
            self._set_preview_selection(
                PreviewFileSelection(
                    origin="pac",
                    logical_path=entry.name,
                    size=current_size,
                    pac_ref=ref,
                )
            )
            if entry.editable:
                target = self.workbench.materialize_refs([ref], editable_only=True)[0]
                options = self._resolve_options(Path(entry.name))
                if entry.suffix == ".tbl" and not options.schema_hint:
                    options.schema_hint = Path(entry.name).stem
                preview_session = DocumentSession(self.business.service)
                document = preview_session.open_document(
                    target.path,
                    options=options,
                )
                self._render_document_preview(
                    target.pac_path.name,
                    target.entry_name,
                    document,
                )
            elif entry.suffix in MEDIA_SUFFIXES:
                self._start_media_preview(self.preview_selection)
                return
            else:
                data = project.workspace.read_entry_prefix(
                    entry.name,
                    512,
                )
                hex_lines = [
                    " ".join(f"{byte:02X}" for byte in data[index:index + 16])
                    for index in range(0, len(data), 16)
                ]
                pac_name = project.workspace.archive.source_path.name
                self.preview_meta_var.set(
                    f"{pac_name}  ::  {entry.name}　|　{self._format_bytes(current_size)}"
                )
                self._render_preview_message(
                    f"PAC：{pac_name}\n"
                    f"路径：{entry.name}\n"
                    f"大小：{self._format_bytes(current_size)}\n"
                    "该类型不可用文本编辑器打开。以下为前 512 字节十六进制预览：\n\n"
                    + "\n".join(hex_lines)
                )
            self.nb.select(self.preview_tab)
        except Exception as exc:
            self.preview_meta_var.set(f"预览失败：{ref.path}")
            self._render_preview_message(f"无法生成预览：\n{exc}")
            self.nb.select(self.preview_tab)

    def _render_document_preview(
        self,
        source_label: str,
        logical_path: str,
        document,
    ) -> None:
        self._show_text_preview()
        self.preview_hint_var.set("行高随内容调整（最多 5 倍）；双击条目查看全文或编辑。")
        self.preview_document = document
        self.preview_meta_var.set(
            f"{source_label}  ::  {logical_path}"
            f"　|　{document.engine}　|　{len(document.units)} 个文本条目"
        )
        self.preview_meta_var.set(
            self.preview_meta_var.get() + self._document_coverage_label(document)
        )
        self.preview_tree.delete(*self.preview_tree.get_children())
        self.preview_row_indices.clear()
        for unit in document.units:
            item = self.preview_tree.insert(
                "",
                "end",
                values=(
                    unit.index,
                    unit.location,
                    unit.current_text,
                ),
            )
            self.preview_row_indices[item] = unit.index

    def _on_preview_text_double_click(self, event) -> str:
        selection = self.preview_selection
        item = (self.preview_tree.identify_row(event.y) if getattr(event, "keysym", "") != "Return"
                else next(iter(self.preview_tree.selection()), ""))
        index = self.preview_row_indices.get(item)
        if selection is None or index is None or not self._ensure_idle():
            return "break"
        if selection.pac_ref is not None:
            self._open_pac_entry(selection.pac_ref)
            opened = (self.current_project_id == selection.pac_ref.workspace_id
                      and self.current_pac_entry == selection.pac_ref.path)
        elif selection.file_path is not None:
            self._open_unpacked_file(selection.file_path)
            opened = (self.current_origin == "unpacked" and self.current_file == selection.file_path.resolve())
        else:
            opened = False
        if opened and self._select_single_unit(index):
            self._begin_single_edit(self.single_tree.selection()[0])
        return "break"

    def _render_preview_message(self, message: str) -> None:
        self._show_text_preview()
        self.preview_hint_var.set("请选择单个文件进行只读预览。")
        self.preview_document = None
        self.preview_media = None
        self.preview_display_image = None
        self.preview_photo = None
        if hasattr(self, "preview_tree"):
            self.preview_tree.delete(*self.preview_tree.get_children())
            self.preview_row_indices.clear()
            for line in message.splitlines() or [""]:
                self.preview_tree.insert("", "end", values=("", "", line))

    def _show_text_preview(self) -> None:
        if not hasattr(self, "preview_text_panel"):
            return
        self._cancel_preview_model_redraw()
        self._close_preview_playback()
        self.preview_media_panel.pack_forget()
        if not self.preview_text_panel.winfo_manager():
            self.preview_text_panel.pack(fill="both", expand=True)

    def _show_media_preview(self) -> None:
        self.preview_text_panel.pack_forget()
        if not self.preview_media_panel.winfo_manager():
            self.preview_media_panel.pack(fill="both", expand=True)

    def _on_workspace_tab_changed(self, _event=None) -> None:
        if self.nb.select() != str(self.preview_tab):
            self._close_preview_playback()
            self._pause_preview_model_animation(sync_frame=False)
        else:
            self._schedule_preview_video_auto_prime()

    def _on_primary_tab_changed(self, _event=None) -> None:
        if self.primary_nb.select() != str(self.workspace_page):
            self._close_preview_playback()
            self._pause_preview_model_animation(sync_frame=False)
            if self.primary_nb.select() == str(self.workspace_tab):
                self.refresh_pac_cache()
                self.refresh_runtime()
        else:
            self._schedule_main_sash_guard()
            if self.nb.select() == str(self.preview_tab):
                self._schedule_preview_video_auto_prime()

    def _start_media_preview(
        self,
        selection: PreviewFileSelection | None,
    ) -> None:
        if selection is None:
            return
        self.preview_meta_var.set(
            f"{selection.logical_path}　|　{self._format_bytes(selection.size)}　|　正在加载媒体…"
        )
        self._render_preview_message("正在后台加载媒体预览，请稍候。")
        self.nb.select(self.preview_tab)

        def worker() -> MediaPreview:
            atlas_path: Path | None = None
            atlas_label = ""
            model_texture_resolver = None
            model_companion_resolver = None
            model_identity_tables: dict[str, Path] = {}
            if selection.origin == "pac":
                ref = selection.pac_ref
                if ref is None:
                    raise RuntimeError("PAC 预览选择已经失效。")
                project = self.workbench.get(ref.workspace_id)
                path = project.workspace.materialize(selection.logical_path)
                if path.suffix.lower() == ".fnt":
                    atlas_label = infer_font_atlas_entry(
                        selection.logical_path
                    ) or ""
                    atlas_path = self._materialize_open_font_atlas(
                        selection.logical_path,
                        preferred_workspace_id=ref.workspace_id,
                    )
                elif path.suffix.lower() == ".mdl":
                    model_texture_resolver = lambda texture_name: (
                        self._materialize_open_model_texture(
                            selection.logical_path,
                            texture_name,
                            preferred_workspace_id=ref.workspace_id,
                        )
                    )
                    model_companion_resolver = lambda model_key: (
                        self._materialize_open_model_companion(
                            selection.logical_path,
                            model_key,
                            preferred_workspace_id=ref.workspace_id,
                        )
                    )
                    model_identity_tables = (
                        self._materialize_open_model_identity_tables(
                            preferred_workspace_id=ref.workspace_id,
                        )
                    )
            else:
                path = selection.file_path
                if path is None:
                    raise RuntimeError("解包文件预览选择已经失效。")
                if path.suffix.lower() == ".fnt":
                    atlas_path = infer_font_atlas_path(path)
                    atlas_label = (
                        str(atlas_path)
                        if atlas_path is not None
                        else ""
                    )
                elif path.suffix.lower() == ".mdl":
                    model_texture_resolver = lambda texture_name: (
                        infer_model_texture_path(path, texture_name)
                    )
                    model_identity_tables = (
                        infer_model_identity_table_paths(path)
                    )
                    model_companion_resolver = lambda model_key: (
                        (
                            candidate,
                            candidate.name,
                        )
                        if (
                            candidate := companion_model_path(
                                path,
                                model_key,
                            )
                        )
                        is not None
                        else None
                    )
            return self.preview_service.load(
                path,
                font_atlas_path=atlas_path,
                font_atlas_label=atlas_label,
                model_texture_resolver=model_texture_resolver,
                model_companion_resolver=model_companion_resolver,
                model_identity_tables=model_identity_tables,
                model_logical_path=(
                    selection.logical_path
                    if selection.origin == "pac"
                    else ""
                ),
            )

        def completed(preview: MediaPreview) -> None:
            if self.preview_selection != selection:
                return
            if (
                preview.kind == "font"
                and preview.font_atlas_image is None
                and selection.origin == "pac"
            ):
                atlas_entry = infer_font_atlas_entry(selection.logical_path)
                atlas_pac = infer_font_atlas_pac_name(selection.logical_path)
                expected = atlas_entry or "对应的 DDS 字体图集"
                suggestion = (
                    f"如需图集和字形图像，可同时打开 {atlas_pac}，"
                    "再点击“显示完整图集”重新查找。"
                    if atlas_pac
                    else "如需图集和字形图像，可同时打开包含对应 DDS 的图片 PAC。"
                )
                preview.warning = (
                    f"可选图集未加载：{expected}。"
                    f"FNT 字形记录仍可正常查看；{suggestion}"
                )
            if (
                preview.kind == "model"
                and preview.model_geometry is not None
                and preview.model_geometry.textured_material_count == 0
                and any(
                    material.base_color_texture
                    for material in preview.model_materials
                )
                and selection.origin == "pac"
            ):
                texture_pac = infer_image_pac_name(
                    selection.logical_path
                )
                if texture_pac:
                    preview.warning += (
                        f"\n如需贴图表面，可同时打开 {texture_pac}，"
                        "再重新选择该 MDL。"
                    )
            self.preview_meta_var.set(
                f"{selection.logical_path}　|　{self._format_bytes(selection.size)}"
            )
            self._render_media_preview(preview)
            self.status_var.set(f"媒体预览已载入：{selection.logical_path}")

        self._run_background("正在加载媒体预览…", worker, completed)

    def _refresh_pac_dependent_preview(self) -> None:
        """Reload the same MDL/FNT when the set of resource PACs changes."""
        selection = self.preview_selection
        if (
            selection is None
            or selection.origin != "pac"
            or Path(selection.logical_path).suffix.lower()
            not in {".mdl", ".fnt"}
        ):
            return
        ref = selection.pac_ref
        if ref is None:
            return
        try:
            self.workbench.get(ref.workspace_id)
        except KeyError:
            return
        self._start_media_preview(selection)

    def _materialize_open_font_atlas(
        self,
        logical_path: str,
        *,
        preferred_workspace_id: str,
    ) -> Path | None:
        atlas_name = infer_font_atlas_entry(logical_path)
        if atlas_name is None:
            return None
        projects = self.workbench.projects()
        projects.sort(
            key=lambda project: (
                project.workspace.workspace_id != preferred_workspace_id,
                str(project.workspace.archive.source_path).casefold(),
            )
        )
        for project in projects:
            try:
                entry = project.workspace.get_entry(atlas_name)
            except KeyError:
                continue
            if entry.suffix != ".dds":
                continue
            return project.workspace.materialize(atlas_name)
        return None

    def _materialize_open_model_texture(
        self,
        logical_path: str,
        texture_name: str,
        *,
        preferred_workspace_id: str,
    ) -> Path | None:
        texture_entry = infer_model_texture_entry(
            logical_path,
            texture_name,
        )
        if texture_entry is None:
            return None
        projects = self.workbench.projects()
        projects.sort(
            key=lambda project: (
                project.workspace.workspace_id != preferred_workspace_id,
                str(project.workspace.archive.source_path).casefold(),
            )
        )
        for project in projects:
            try:
                entry = project.workspace.get_entry(texture_entry)
            except KeyError:
                continue
            if entry.suffix != ".dds":
                continue
            return project.workspace.materialize(texture_entry)
        return None

    def _materialize_open_model_companion(
        self,
        logical_path: str,
        model_key: str,
        *,
        preferred_workspace_id: str,
    ) -> tuple[Path, str] | None:
        companion_entry = companion_model_entry(logical_path, model_key)
        if companion_entry is None:
            return None
        projects = self.workbench.projects()
        projects.sort(
            key=lambda project: (
                project.workspace.workspace_id != preferred_workspace_id,
                str(project.workspace.archive.source_path).casefold(),
            )
        )
        for project in projects:
            try:
                entry = project.workspace.get_entry(companion_entry)
            except KeyError:
                continue
            if entry.suffix != ".mdl":
                continue
            return (
                project.workspace.materialize(companion_entry),
                companion_entry,
            )
        return None

    def _materialize_open_model_identity_tables(
        self,
        *,
        preferred_workspace_id: str,
    ) -> dict[str, Path]:
        candidates: list[
            tuple[int, int, int, str, object, dict[str, str]]
        ] = []
        for project in self.workbench.projects():
            entries = {
                Path(entry.name.replace("\\", "/")).name.casefold(): entry.name
                for entry in project.workspace.entries()
                if Path(entry.name).suffix.lower() == ".tbl"
            }
            matched = {
                key: entries[f"{key}.tbl"]
                for key in ("t_name", "t_status")
                if f"{key}.tbl" in entries
            }
            if not matched:
                continue
            pac_name = project.workspace.archive.source_path.stem.casefold()
            language_rank = (
                0 if "table_tc" in pac_name
                else 1 if "table_sc" in pac_name
                else 2
            )
            preferred_rank = int(
                project.workspace.workspace_id != preferred_workspace_id
            )
            candidates.append(
                (
                    -len(matched),
                    language_rank,
                    preferred_rank,
                    str(project.workspace.archive.source_path).casefold(),
                    project,
                    matched,
                )
            )
        if not candidates:
            return {}
        (
            _count,
            _language,
            _preferred,
            _path,
            project,
            matched,
        ) = min(candidates)
        return {
            key: project.workspace.materialize(entry_name)
            for key, entry_name in matched.items()
        }

    def _render_media_preview(self, preview: MediaPreview) -> None:
        self._close_preview_playback()
        self._cancel_preview_model_redraw()
        self.preview_hint_var.set(
            "字体预览只读；可查看完整图集、选择字形或提取另存。"
            if preview.kind == "font"
            else "模型预览只读；左键旋转、中键平移、滚轮缩放，可提取另存。"
            if preview.kind == "model" and preview.model_geometry is not None
            else "模型预览只读；当前显示结构摘要，可提取另存。"
            if preview.kind == "model"
            else "模型信息预览只读；当前显示二进制 JSON 的字段目录，可提取另存。"
            if preview.kind == "model_info"
            else "媒体预览只读；可播放、拖动进度或提取另存。"
        )
        self.preview_document = None
        self.preview_media = preview
        self.preview_display_image = preview.image
        self.preview_photo = None
        titles = {
            "image": "图片预览",
            "audio": "音频预览",
            "video": "视频预览",
            "font": "字体预览",
            "model": (
                "模型三维预览"
                if preview.model_geometry is not None
                else "模型结构预览"
            ),
            "model_info": "模型信息结构预览",
        }
        self.preview_media_title_var.set(titles[preview.kind])
        if preview.kind == "font":
            self.preview_font_atlas_button.pack(side="right")
        else:
            self.preview_font_atlas_button.pack_forget()
        self.preview_media_warning_var.set(preview.warning)
        self.preview_media_meta_tree.delete(
            *self.preview_media_meta_tree.get_children()
        )
        for field, value in preview.metadata:
            self.preview_media_meta_tree.insert(
                "",
                "end",
                values=(field, value),
            )
        self.preview_font_glyph_tree.delete(
            *self.preview_font_glyph_tree.get_children()
        )
        self.preview_font_row_glyphs.clear()
        for glyph in preview.font_glyphs:
            channel = (
                "G"
                if glyph.channel_flags & 0x100
                else "R" if glyph.channel_flags & 0x200 else f"0x{glyph.channel_flags:04X}"
            )
            item = self.preview_font_glyph_tree.insert(
                "",
                "end",
                values=(
                    f"U+{glyph.codepoint:04X}",
                    glyph.character,
                    f"{glyph.atlas_x}, {glyph.atlas_y}",
                    f"{glyph.width} × {glyph.height}",
                    f"{glyph.offset_x}, {glyph.offset_y}",
                    glyph.advance,
                    channel,
                ),
            )
            self.preview_font_row_glyphs[item] = glyph
        if preview.kind == "font":
            self.preview_media_detail_notebook.add(
                self.preview_font_glyph_tab,
                text="字形",
            )
            self.preview_media_detail_notebook.select(self.preview_font_glyph_tab)
        else:
            self.preview_media_detail_notebook.hide(self.preview_font_glyph_tab)
            self.preview_media_detail_notebook.select(self.preview_media_info_tab)
        duration = max(preview.duration_seconds, 0.0)
        self.preview_media_progress_var.set(0.0)
        self.preview_media_progress_scale.set_range(maximum=max(duration, 1.0))
        self.preview_media_time_var.set(
            f"{self._format_media_duration(0.0)} / "
            f"{self._format_media_duration(duration)}"
        )
        if preview.kind in {"audio", "video"}:
            self.preview_media_controls.grid()
            self.preview_media_play_button.configure(
                text="播放",
                state="normal",
            )
            self.preview_media_stop_button.configure(state="normal")
        else:
            self.preview_media_controls.grid_remove()
            self.preview_media_play_button.configure(
                text="播放",
                state="disabled",
            )
            self.preview_media_stop_button.configure(state="disabled")
        if preview.kind == "model" and preview.model_geometry is not None:
            self.preview_model_yaw = 0.45
            self.preview_model_pitch = -0.12
            self.preview_model_zoom = 0.95
            self.preview_model_pan_x = 0.0
            self.preview_model_pan_y = 0.0
            self.preview_model_drag_anchor = None
            self.preview_model_pan_anchor = None
            self._preview_model_low_quality = False
            self.preview_model_wireframe_var.set(False)
            self.preview_model_controls.grid()
            animation_player = preview.model_animation_player
            if (
                animation_player is not None
                and animation_player.compatible
                and animation_player.clip.duration > 0.0
            ):
                duration = animation_player.clip.duration
                self.preview_model_animation_scale.set_range(
                    minimum=0.0,
                    maximum=duration,
                )
                self._set_preview_model_animation_time(
                    0.0,
                    redraw=False,
                )
                self.preview_model_animation_controls.grid()
            else:
                self.preview_model_animation_controls.grid_remove()
        else:
            self.preview_model_animation_controls.grid_remove()
            self.preview_model_controls.grid_remove()
        if preview.kind == "audio":
            self.preview_media_visual_frame.rowconfigure(0, weight=0)
            self.preview_media_canvas.configure(height=140)
            self.preview_media_canvas.grid(sticky="ew")
        else:
            self.preview_media_visual_frame.rowconfigure(0, weight=1)
            self.preview_media_canvas.configure(height=520)
            self.preview_media_canvas.grid(sticky="nsew")
        self._show_media_preview()
        self.preview_media_panel.update_idletasks()
        self._redraw_media_visual()
        if preview.kind == "video":
            self._schedule_preview_video_auto_prime(preview)

    def _redraw_media_visual(self) -> None:
        if not hasattr(self, "preview_media_canvas"):
            return
        canvas = self.preview_media_canvas
        preview = self.preview_media
        if preview is None:
            canvas.delete("all")
            return
        if preview.kind == "video" and self.preview_playback is not None:
            return
        width = max(canvas.winfo_width(), 320)
        minimum_height = 96 if preview.kind == "audio" else 240
        height = max(canvas.winfo_height(), minimum_height)
        if preview.kind == "model" and preview.model_geometry is not None:
            if self.preview_photo is None:
                canvas.delete("all")
                canvas.create_text(
                    width // 2,
                    height // 2,
                    text="正在生成稳定的三维表面…",
                    fill="#e8ddd0",
                    font=("Microsoft YaHei UI", 13),
                )
            self._request_preview_model_render(preview, width, height)
            return
        canvas.delete("all")
        if preview.kind in {"model", "model_info"}:
            if preview.kind == "model_info":
                canvas.create_text(
                    width // 2,
                    height // 2,
                    text=(
                        "MI 模型信息已识别\n"
                        f"{len(preview.model_info_fields)} 个字段名\n\n"
                        "此文件以二进制格式存储，可提取后查看"
                    ),
                    fill="#e8ddd0",
                    justify="center",
                    font=("Microsoft YaHei UI", 13),
                )
                return
            section_count = len(preview.model_sections)
            material_count = len(preview.model_materials)
            texture_count = len(
                {
                    texture
                    for material in preview.model_materials
                    for texture in material.textures
                    if texture
                }
            )
            animation_tracks = next(
                (
                    section.item_count
                    for section in preview.model_sections
                    if section.section_type == 3
                ),
                0,
            )
            canvas.create_text(
                width // 2,
                height // 2,
                text=(
                    "MDL 结构已解析\n"
                    + (
                        f"{animation_tracks} 条动画轨道 · 时长 "
                        f"{self._format_model_animation_time(preview.duration_seconds)}"
                        if preview.duration_seconds > 0.0
                        else (
                            f"{section_count} 个区段 · "
                            f"{material_count} 个材质 · "
                            f"{texture_count} 个纹理依赖"
                        )
                    )
                    + "\n\n"
                    + (
                        "该动画未找到可安全绑定的基础网格"
                        if preview.duration_seconds > 0.0
                        else "该文件不包含可渲染的静态网格"
                    )
                ),
                fill="#e8ddd0",
                justify="center",
                font=("Microsoft YaHei UI", 13),
            )
            return
        if preview.kind in {"image", "video", "font"}:
            image = self.preview_display_image
            if image is None:
                canvas.create_text(
                    width // 2,
                    height // 2,
                    text=(
                        "未加载对应的字体图集"
                        if preview.kind == "font"
                        else "点击播放或拖动进度条以显示视频画面"
                        if preview.kind == "video"
                        else "暂无可显示的图像"
                    ),
                    fill="#e8ddd0",
                )
                return
            display = image.copy()
            display.thumbnail(
                (max(width - 24, 1), max(height - 24, 1)),
                Image.Resampling.LANCZOS,
            )
            self.preview_photo = ImageTk.PhotoImage(display)
            canvas.create_image(
                width // 2,
                height // 2,
                image=self.preview_photo,
                anchor="center",
            )
            return

        peaks = preview.waveform
        midpoint = height / 2
        canvas.create_line(
            12,
            midpoint,
            width - 12,
            midpoint,
            fill="#776b61",
        )
        if not peaks:
            canvas.create_text(
                width // 2,
                height // 2,
                text="没有可显示的波形数据",
                fill="#e8ddd0",
            )
            return
        available_width = max(width - 24, 1)
        amplitude = max(height * 0.34, 1)
        for pixel in range(available_width):
            index = min(
                len(peaks) - 1,
                int(pixel * len(peaks) / available_width),
            )
            peak = peaks[index] * amplitude
            x = pixel + 12
            canvas.create_line(
                x,
                midpoint - peak,
                x,
                midpoint + peak,
                fill="#d7a05d",
            )
        canvas.create_text(
            18,
            18,
            text=f"时长 {self._format_media_duration(preview.duration_seconds)}",
            fill="#e8ddd0",
            anchor="nw",
        )

    def _has_playable_model_animation(self) -> bool:
        preview = self.preview_media
        player = (
            preview.model_animation_player
            if preview is not None and preview.kind == "model"
            else None
        )
        return bool(
            player is not None
            and player.compatible
            and player.clip.duration > 0.0
        )

    def _model_animation_duration(self) -> float:
        preview = self.preview_media
        player = (
            preview.model_animation_player
            if preview is not None and preview.kind == "model"
            else None
        )
        return (
            max(float(player.clip.duration), 0.0)
            if player is not None
            else 0.0
        )

    def _toggle_preview_model_animation(self) -> None:
        if not self._has_playable_model_animation():
            return
        if self._preview_model_animation_playing:
            self._pause_preview_model_animation()
            return
        duration = self._model_animation_duration()
        if self.preview_model_animation_time >= duration:
            self._set_preview_model_animation_time(0.0)
        self._preview_model_animation_playing = True
        self._preview_model_animation_origin = (
            self.preview_model_animation_time
        )
        self._preview_model_animation_started_at = time.perf_counter()
        self.preview_model_animation_play_button.configure(text="暂停")
        self._advance_preview_model_animation()

    def _pause_preview_model_animation(
        self,
        *,
        sync_frame: bool = True,
    ) -> None:
        was_playing = self._preview_model_animation_playing
        self._preview_model_animation_playing = False
        self._preview_model_animation_frame_dirty = bool(
            sync_frame
            and was_playing
            and self._has_playable_model_animation()
        )
        after_id = self._preview_model_animation_after_id
        self._preview_model_animation_after_id = None
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except (RuntimeError, tk.TclError):
                pass
        if hasattr(self, "preview_model_animation_play_button"):
            self.preview_model_animation_play_button.configure(text="继续")
        if self._preview_model_animation_frame_dirty:
            self._flush_preview_model_animation_frame()

    def _stop_preview_model_animation(self) -> None:
        self._pause_preview_model_animation(sync_frame=False)
        self._set_preview_model_animation_time(0.0)
        if hasattr(self, "preview_model_animation_play_button"):
            self.preview_model_animation_play_button.configure(text="播放")

    def _cancel_preview_model_animation(self) -> None:
        self._pause_preview_model_animation(sync_frame=False)
        self.preview_model_animation_time = 0.0
        self._preview_model_animation_scrubbing = False
        self._preview_model_animation_resume_after_scrub = False
        if hasattr(self, "preview_model_animation_progress_var"):
            self.preview_model_animation_progress_var.set(0.0)
        if hasattr(self, "preview_model_animation_play_button"):
            self.preview_model_animation_play_button.configure(text="播放")

    def _advance_preview_model_animation(self) -> None:
        self._preview_model_animation_after_id = None
        if (
            not self._preview_model_animation_playing
            or not self._has_playable_model_animation()
        ):
            return
        duration = self._model_animation_duration()
        now = time.perf_counter()
        value = (
            self._preview_model_animation_origin
            + now
            - self._preview_model_animation_started_at
        )
        if duration > 0.0 and value >= duration:
            value %= duration
            self._preview_model_animation_origin = value
            self._preview_model_animation_started_at = now
        self._set_preview_model_animation_time(value, redraw=False)
        self._preview_model_animation_frame_dirty = True
        self._flush_preview_model_animation_frame()
        player = self.preview_media.model_animation_player
        frame_delay = (
            33
            if player is not None and getattr(player, "uses_numpy", False)
            else 250
        )
        self._preview_model_animation_after_id = self.root.after(
            frame_delay,
            self._advance_preview_model_animation,
        )

    def _flush_preview_model_animation_frame(self) -> None:
        if (
            not self._preview_model_animation_frame_dirty
            or not self._has_interactive_model_preview()
        ):
            return
        with self._preview_model_worker_lock:
            renderer_idle = (
                not self._preview_model_worker_busy
                and self._preview_model_pending_request is None
            )
        if (
            renderer_idle
            and self._preview_model_render_after_id is None
        ):
            self._preview_model_animation_frame_dirty = False
            self._perform_preview_model_redraw()

    def _set_preview_model_animation_time(
        self,
        seconds: float,
        *,
        redraw: bool = True,
    ) -> None:
        duration = self._model_animation_duration()
        value = min(max(float(seconds), 0.0), duration) if duration else 0.0
        self.preview_model_animation_time = value
        self.preview_model_animation_progress_var.set(value)
        self.preview_model_animation_time_var.set(
            f"{self._format_model_animation_time(value)} / "
            f"{self._format_model_animation_time(duration)}"
        )
        if redraw and self._has_playable_model_animation():
            self._schedule_preview_model_redraw()

    def _seek_preview_model_animation(self, raw_value: str) -> None:
        if not self._has_playable_model_animation():
            return
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return
        self._set_preview_model_animation_time(value)
        if self._preview_model_animation_playing:
            self._preview_model_animation_origin = (
                self.preview_model_animation_time
            )
            self._preview_model_animation_started_at = time.perf_counter()

    def _begin_preview_model_animation_scrub(self, _event=None) -> None:
        self._preview_model_animation_scrubbing = True
        self._preview_model_animation_resume_after_scrub = (
            self._preview_model_animation_playing
        )
        if self._preview_model_animation_playing:
            self._pause_preview_model_animation(sync_frame=False)

    def _finish_preview_model_animation_scrub(self, _event=None) -> None:
        resume = self._preview_model_animation_resume_after_scrub
        self._preview_model_animation_scrubbing = False
        self._preview_model_animation_resume_after_scrub = False
        self._set_preview_model_animation_time(
            self.preview_model_animation_progress_var.get()
        )
        if resume:
            self._toggle_preview_model_animation()

    def _request_preview_model_render(
        self,
        preview: MediaPreview,
        width: int,
        height: int,
    ) -> None:
        geometry = preview.model_geometry
        if geometry is None:
            return
        interaction_requested = (
            self._preview_model_low_quality
            and not self.preview_model_wireframe_var.get()
        )
        reduce_resolution = (
            interaction_requested
            and model_3d_service.interaction_requires_low_resolution
        )
        render_width, render_height = calculate_render_dimensions(
            width,
            height,
            interactive=reduce_resolution,
        )
        low_quality = (render_width, render_height) != (width, height)
        self._preview_model_generation += 1
        request = ModelRenderRequest(
            generation=self._preview_model_generation,
            preview=preview,
            display_width=width,
            display_height=height,
            render_width=render_width,
            render_height=render_height,
            yaw=self.preview_model_yaw,
            pitch=self.preview_model_pitch,
            zoom=self.preview_model_zoom,
            pan_x=self.preview_model_pan_x * render_width / width,
            pan_y=self.preview_model_pan_y * render_height / height,
            wireframe=self.preview_model_wireframe_var.get(),
            low_quality=low_quality,
            animation_seconds=self.preview_model_animation_time,
        )
        if (
            preview.model_animation_player is not None
            and request.animation_seconds
            == self.preview_model_animation_time
        ):
            self._preview_model_animation_frame_dirty = False
        start_worker = False
        with self._preview_model_worker_lock:
            self._preview_model_pending_request = request
            if not self._preview_model_worker_running:
                self._preview_model_worker_running = True
                start_worker = True
        self._preview_model_request_event.set()
        if start_worker:
            worker = threading.Thread(
                target=self._preview_model_render_worker,
                daemon=True,
                name="tis-model-render",
            )
            self._preview_model_worker_thread = worker
            worker.start()
        self._schedule_preview_model_result_poll()

    def _preview_model_render_worker(self) -> None:
        try:
            while not self._preview_model_worker_stop.is_set():
                self._preview_model_request_event.wait()
                self._preview_model_request_event.clear()
                if self._preview_model_worker_stop.is_set():
                    break
                with self._preview_model_worker_lock:
                    request = self._preview_model_pending_request
                    self._preview_model_pending_request = None
                    if request is not None:
                        self._preview_model_worker_busy = True
                if request is None:
                    continue
                geometry = request.preview.model_geometry
                try:
                    if geometry is None:
                        raise RuntimeError("MDL 三维几何已经失效。")
                    geometry = self._prepare_preview_model_geometry(request)
                    image = model_3d_service.render(
                        geometry,
                        request.render_width,
                        request.render_height,
                        yaw=request.yaw,
                        pitch=request.pitch,
                        zoom=request.zoom,
                        pan_x=request.pan_x,
                        pan_y=request.pan_y,
                        wireframe=request.wireframe,
                        cancelled=lambda: (
                            self._preview_model_worker_stop.is_set()
                            or self._preview_model_generation
                            != request.generation
                        ),
                    )
                    if image.size != (
                        request.display_width,
                        request.display_height,
                    ):
                        image = image.resize(
                            (
                                request.display_width,
                                request.display_height,
                            ),
                            Image.Resampling.BILINEAR,
                        )
                    error = None
                except ModelRenderCancelled:
                    with self._preview_model_worker_lock:
                        self._preview_model_worker_busy = False
                    continue
                except Exception as exc:
                    image = None
                    error = exc
                self._preview_model_results.put((request, image, error))
                with self._preview_model_worker_lock:
                    self._preview_model_worker_busy = False
        finally:
            try:
                model_3d_service.close_current_thread()
            except Exception:
                pass
            with self._preview_model_worker_lock:
                self._preview_model_worker_running = False
                self._preview_model_worker_busy = False

    def _stop_preview_model_worker(self) -> bool:
        self._preview_model_generation += 1
        self._preview_model_worker_stop.set()
        with self._preview_model_worker_lock:
            self._preview_model_pending_request = None
        self._preview_model_request_event.set()
        worker = self._preview_model_worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
        if worker is not None and worker.is_alive():
            return False
        self._preview_model_worker_thread = None
        return True

    def _prepare_preview_model_geometry(
        self,
        request: ModelRenderRequest,
    ):
        geometry = request.preview.model_geometry
        if geometry is None:
            raise RuntimeError("MDL 三维几何已经失效。")
        animation_player = request.preview.model_animation_player
        if (
            animation_player is None
            or not animation_player.compatible
        ):
            return geometry
        cache_key = (
            id(animation_player),
            float(request.animation_seconds),
        )
        if (
            self._preview_model_pose_cache_key == cache_key
            and self._preview_model_pose_cache_geometry is not None
        ):
            return self._preview_model_pose_cache_geometry
        geometry = animation_player.sample(request.animation_seconds)
        self._preview_model_pose_cache_key = cache_key
        self._preview_model_pose_cache_geometry = geometry
        return geometry

    def _schedule_preview_model_result_poll(self) -> None:
        if self._preview_model_poll_after_id is None:
            self._preview_model_poll_after_id = self.root.after(
                20,
                self._poll_preview_model_results,
            )

    def _poll_preview_model_results(self) -> None:
        self._preview_model_poll_after_id = None
        while True:
            try:
                request, image, error = (
                    self._preview_model_results.get_nowait()
                )
            except queue.Empty:
                break
            preview = self.preview_media
            if (
                request.generation != self._preview_model_generation
                or preview is not request.preview
                or preview.kind != "model"
            ):
                continue
            if error is not None:
                message = f"MDL 三维表面生成失败：{error}"
                current = self.preview_media_warning_var.get().strip()
                if message not in current:
                    self.preview_media_warning_var.set(
                        "\n".join(
                            value for value in (current, message) if value
                        )
                    )
                self.status_var.set(message)
                continue
            self.preview_photo = ImageTk.PhotoImage(image)
            canvas = self.preview_media_canvas
            canvas.delete("all")
            canvas.create_image(
                request.display_width // 2,
                request.display_height // 2,
                image=self.preview_photo,
                anchor="center",
            )
            geometry = request.preview.model_geometry
            if geometry is None:
                continue
            triangle_label = (
                f"已采样 {len(geometry.faces):,} / "
                f"{geometry.source_triangle_count:,} 个三角形"
                if geometry.sampled
                else f"{len(geometry.faces):,} 个三角形"
            )
            if request.low_quality:
                triangle_label += " · 交互低分辨率"
            elif geometry.textured_material_count:
                triangle_label += (
                    f" · {geometry.textured_material_count} 个材质已贴图"
                )
            if request.preview.model_animation_player is not None:
                triangle_label += (
                    " · 动画 "
                    f"{self._format_model_animation_time(request.animation_seconds)}"
                )
            triangle_label += f" · {model_3d_service.last_backend_label}"
            canvas.create_text(
                13,
                13,
                text=triangle_label,
                fill="#17130f",
                anchor="nw",
                font=("Microsoft YaHei UI", 9),
            )
            canvas.create_text(
                12,
                12,
                text=triangle_label,
                fill="#eadfce",
                anchor="nw",
                font=("Microsoft YaHei UI", 9),
            )
        with self._preview_model_worker_lock:
            active = (
                self._preview_model_worker_busy
                or self._preview_model_pending_request is not None
            )
        if (
            self._preview_model_animation_frame_dirty
            and not active
            and self._preview_model_render_after_id is None
        ):
            self._flush_preview_model_animation_frame()
            return
        if active or not self._preview_model_results.empty():
            self._schedule_preview_model_result_poll()

    def _invalidate_preview_model_requests(self) -> None:
        self._preview_model_generation += 1
        self._preview_model_pose_cache_key = None
        self._preview_model_pose_cache_geometry = None
        with self._preview_model_worker_lock:
            self._preview_model_pending_request = None

    def _has_interactive_model_preview(self) -> bool:
        preview = self.preview_media
        return bool(
            preview is not None
            and preview.kind == "model"
            and preview.model_geometry is not None
        )

    def _on_preview_canvas_configure(self, _event=None) -> None:
        if self._has_interactive_model_preview():
            self._schedule_preview_model_redraw(interactive=True)
        else:
            self._redraw_media_visual()

    def _begin_preview_model_orbit(self, event) -> str | None:
        if not self._has_interactive_model_preview():
            return None
        self.preview_model_drag_anchor = (event.x, event.y)
        return "break"

    def _drag_preview_model_orbit(self, event) -> str | None:
        if not self._has_interactive_model_preview():
            return None
        anchor = self.preview_model_drag_anchor
        if anchor is None:
            self.preview_model_drag_anchor = (event.x, event.y)
            return "break"
        delta_x = event.x - anchor[0]
        delta_y = event.y - anchor[1]
        self.preview_model_yaw = (
            self.preview_model_yaw + delta_x * 0.01
        ) % (2.0 * 3.141592653589793)
        self.preview_model_pitch = min(
            1.45,
            max(-1.45, self.preview_model_pitch + delta_y * 0.01),
        )
        self.preview_model_drag_anchor = (event.x, event.y)
        self._schedule_preview_model_redraw(interactive=True)
        return "break"

    def _end_preview_model_orbit(self, _event=None) -> str | None:
        if not self._has_interactive_model_preview():
            return None
        self.preview_model_drag_anchor = None
        self._queue_preview_model_full_redraw(delay=220)
        return "break"

    def _begin_preview_model_pan(self, event) -> str | None:
        if not self._has_interactive_model_preview():
            return None
        self.preview_model_pan_anchor = (event.x, event.y)
        return "break"

    def _drag_preview_model_pan(self, event) -> str | None:
        if not self._has_interactive_model_preview():
            return None
        anchor = self.preview_model_pan_anchor
        if anchor is None:
            self.preview_model_pan_anchor = (event.x, event.y)
            return "break"
        self.preview_model_pan_x += event.x - anchor[0]
        self.preview_model_pan_y += event.y - anchor[1]
        self.preview_model_pan_anchor = (event.x, event.y)
        self._schedule_preview_model_redraw(interactive=True)
        return "break"

    def _end_preview_model_pan(self, _event=None) -> str | None:
        if not self._has_interactive_model_preview():
            return None
        self.preview_model_pan_anchor = None
        self._queue_preview_model_full_redraw(delay=220)
        return "break"

    def _zoom_preview_model(self, event) -> str | None:
        if not self._has_interactive_model_preview():
            return None
        direction = 0
        delta = getattr(event, "delta", 0)
        button = getattr(event, "num", 0)
        if delta:
            direction = 1 if delta > 0 else -1
        elif button in {4, 5}:
            direction = 1 if button == 4 else -1
        if direction:
            factor = 1.12 if direction > 0 else 1 / 1.12
            self.preview_model_zoom = min(
                8.0,
                max(0.2, self.preview_model_zoom * factor),
            )
            self._schedule_preview_model_redraw(interactive=True)
        return "break"

    def _reset_preview_model_view(self, event=None) -> str | None:
        if not self._has_interactive_model_preview():
            return None
        self.preview_model_yaw = 0.45
        self.preview_model_pitch = -0.12
        self.preview_model_zoom = 0.95
        self.preview_model_pan_x = 0.0
        self.preview_model_pan_y = 0.0
        self.preview_model_drag_anchor = None
        self.preview_model_pan_anchor = None
        self._preview_model_low_quality = False
        self._cancel_preview_model_full_redraw()
        self._schedule_preview_model_redraw()
        return "break" if event is not None else None

    def _schedule_preview_model_redraw(
        self,
        *,
        interactive: bool = False,
    ) -> None:
        if (
            not self._has_interactive_model_preview()
            or self._preview_model_render_after_id is not None
        ):
            if interactive and self._has_interactive_model_preview():
                self._preview_model_low_quality = True
                self._queue_preview_model_full_redraw()
            return
        if interactive:
            self._preview_model_low_quality = True
            self._queue_preview_model_full_redraw()
        self._preview_model_render_after_id = self.root.after(
            16,
            self._perform_preview_model_redraw,
        )

    def _perform_preview_model_redraw(self) -> None:
        self._preview_model_render_after_id = None
        if self._has_interactive_model_preview():
            self._redraw_media_visual()

    def _queue_preview_model_full_redraw(self, *, delay: int = 250) -> None:
        if not self._has_interactive_model_preview():
            return
        self._cancel_preview_model_full_redraw()
        self._preview_model_full_render_after_id = self.root.after(
            delay,
            self._perform_preview_model_full_redraw,
        )

    def _perform_preview_model_full_redraw(self) -> None:
        self._preview_model_full_render_after_id = None
        self._preview_model_low_quality = False
        self._schedule_preview_model_redraw()

    def _cancel_preview_model_full_redraw(self) -> None:
        after_id = self._preview_model_full_render_after_id
        self._preview_model_full_render_after_id = None
        if after_id is None:
            return
        try:
            self.root.after_cancel(after_id)
        except (RuntimeError, tk.TclError):
            pass

    def _cancel_preview_model_redraw(self) -> None:
        self._cancel_preview_model_animation()
        self._invalidate_preview_model_requests()
        poll_after_id = self._preview_model_poll_after_id
        self._preview_model_poll_after_id = None
        if poll_after_id is not None:
            try:
                self.root.after_cancel(poll_after_id)
            except (RuntimeError, tk.TclError):
                pass
        after_id = self._preview_model_render_after_id
        self._preview_model_render_after_id = None
        self.preview_model_drag_anchor = None
        self.preview_model_pan_anchor = None
        self._preview_model_low_quality = False
        self._cancel_preview_model_full_redraw()
        if after_id is None:
            return
        try:
            self.root.after_cancel(after_id)
        except (RuntimeError, tk.TclError):
            pass

    def _on_preview_font_glyph_select(self, _event=None) -> None:
        preview = self.preview_media
        if preview is None or preview.kind != "font":
            return
        selected = self.preview_font_glyph_tree.selection()
        if len(selected) != 1:
            return
        glyph = self.preview_font_row_glyphs.get(selected[0])
        if glyph is None:
            return
        image = render_font_glyph(preview, glyph)
        if image is None:
            self.status_var.set(
                f"U+{glyph.codepoint:04X} 的图集图像不可用。"
            )
            return
        self.preview_display_image = image
        self.preview_photo = None
        self.preview_media_title_var.set(
            f"字体字形 · U+{glyph.codepoint:04X} {glyph.character}"
        )
        self._redraw_media_visual()
        self.status_var.set(
            f"字形 U+{glyph.codepoint:04X}："
            f"{glyph.width} × {glyph.height}，前进宽度 {glyph.advance}"
        )

    def _show_preview_font_atlas(self) -> None:
        preview = self.preview_media
        if preview is None or preview.kind != "font":
            return
        if preview.font_atlas_path is None:
            selection = self.preview_selection
            if selection is not None and self._ensure_idle():
                self.status_var.set("正在重新查找可选的 DDS 字体图集…")
                self._start_media_preview(selection)
            return
        self.preview_display_image = preview.image
        self.preview_photo = None
        self.preview_media_title_var.set("字体预览")
        self._redraw_media_visual()
        self.status_var.set(
            f"字体图集：{preview.font_atlas_label or preview.font_atlas_path.name}"
        )

    def play_preview_media(self) -> None:
        preview = self.preview_media
        if preview is None or preview.kind not in {"audio", "video"}:
            return
        self._cancel_preview_video_auto_prime()
        try:
            playback = self._ensure_preview_playback()
            was_priming = self._preview_video_prime_phase != "idle"
            prime_target = self._preview_video_prime_target
            if was_priming:
                self._cancel_preview_video_prime(restore_audio=True)
            state = playback.state()
            if was_priming:
                if prime_target is not None:
                    if state in {PlaybackState.PLAYING, PlaybackState.PAUSED}:
                        playback.seek_seconds(prime_target)
                    else:
                        self._preview_pending_seek = prime_target
                self._preview_video_user_started = True
                if state not in {
                    PlaybackState.OPENING,
                    PlaybackState.BUFFERING,
                    PlaybackState.PLAYING,
                }:
                    playback.play()
                self.preview_media_play_button.configure(text="暂停")
                self.status_var.set(f"正在播放：{preview.source_path.name}")
                self._schedule_preview_progress()
                return
            if state in {
                PlaybackState.OPENING,
                PlaybackState.BUFFERING,
                PlaybackState.PLAYING,
            }:
                playback.pause()
                self.preview_media_play_button.configure(text="继续")
                self.status_var.set(f"已暂停：{preview.source_path.name}")
            else:
                if state is PlaybackState.ENDED:
                    playback.seek_seconds(0.0)
                playback.set_muted(False)
                playback.set_volume(self.preview_media_volume_var.get())
                self._preview_video_user_started = True
                playback.play()
                self.preview_media_play_button.configure(text="暂停")
                self.status_var.set(f"正在播放：{preview.source_path.name}")
            self._schedule_preview_progress()
        except Exception as exc:
            self._report_preview_playback_error(exc)

    def _ensure_preview_playback(self) -> MediaPlaybackController:
        preview = self.preview_media
        if preview is None or preview.kind not in {"audio", "video"}:
            raise PlaybackError("当前文件不是可播放的媒体。")
        playback = self.preview_playback
        if (
            playback is not None
            and not playback.is_closed
            and self._preview_playback_source == preview.source_path
        ):
            return playback

        self._close_preview_playback(clear_pending=False)
        playback = MediaPlaybackController()
        try:
            window_handle = (
                int(self.preview_media_canvas.winfo_id())
                if preview.kind == "video"
                else None
            )
            playback.load(
                preview.source_path,
                window_handle=window_handle,
            )
            playback.set_volume(self.preview_media_volume_var.get())
        except Exception:
            playback.close()
            raise
        self.preview_playback = playback
        self._preview_playback_source = preview.source_path
        if preview.kind == "video":
            self.preview_media_canvas.delete("all")
        return playback

    def stop_preview_media(self) -> None:
        preview = self.preview_media
        self._close_preview_playback(reset_progress=True)
        if preview is not None:
            self.status_var.set(f"已停止：{preview.source_path.name}")
        self._redraw_media_visual()

    def _close_preview_playback(
        self,
        *,
        reset_progress: bool = False,
        clear_pending: bool = True,
    ) -> None:
        self._cancel_preview_video_auto_prime()
        self._cancel_preview_video_prime(restore_audio=False)
        after_id = self._preview_progress_after_id
        self._preview_progress_after_id = None
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except (RuntimeError, tk.TclError):
                pass
        playback = self.preview_playback
        self.preview_playback = None
        self._preview_playback_source = None
        self._preview_scrubbing = False
        self._preview_resume_after_scrub = False
        self._preview_video_user_started = False
        if clear_pending:
            self._preview_pending_seek = None
        if playback is not None:
            playback.close()
        if hasattr(self, "preview_media_play_button"):
            self.preview_media_play_button.configure(text="播放")
        if reset_progress and self.preview_media is not None:
            duration = max(self.preview_media.duration_seconds, 0.0)
            self.preview_media_progress_var.set(0.0)
            self.preview_media_time_var.set(
                f"{self._format_media_duration(0.0)} / "
                f"{self._format_media_duration(duration)}"
            )

    def _schedule_preview_progress(self) -> None:
        if self._preview_progress_after_id is None:
            self._preview_progress_after_id = self.root.after(
                150,
                self._poll_preview_progress,
            )

    def _poll_preview_progress(self) -> None:
        self._preview_progress_after_id = None
        playback = self.preview_playback
        preview = self.preview_media
        if playback is None or preview is None:
            return
        if self._preview_video_prime_phase != "idle":
            return
        try:
            state = playback.state()
            pending_seek = self._preview_pending_seek
            if (
                pending_seek is not None
                and state in {PlaybackState.PLAYING, PlaybackState.PAUSED}
            ):
                playback.seek_seconds(pending_seek)
                self._preview_pending_seek = None
            position = playback.position()
            duration = playback.duration() or preview.duration_seconds
        except PlaybackError as exc:
            self._report_preview_playback_error(exc)
            return

        duration = max(duration, 0.0)
        position = min(max(position, 0.0), duration) if duration else max(position, 0.0)
        if duration:
            self.preview_media_progress_scale.set_range(maximum=duration)
        if not self._preview_scrubbing:
            self.preview_media_progress_var.set(position)
        displayed_position = (
            self.preview_media_progress_var.get()
            if self._preview_scrubbing
            else position
        )
        self.preview_media_time_var.set(
            f"{self._format_media_duration(displayed_position)} / "
            f"{self._format_media_duration(duration)}"
        )
        keep_polling = state in {
            PlaybackState.OPENING,
            PlaybackState.BUFFERING,
            PlaybackState.PLAYING,
        }
        if keep_polling:
            self.preview_media_play_button.configure(text="暂停")
        elif state is PlaybackState.PAUSED:
            self.preview_media_play_button.configure(text="继续")
        elif state is PlaybackState.ENDED:
            self._close_preview_playback()
            self.preview_media_progress_var.set(duration)
            self.preview_media_time_var.set(
                f"{self._format_media_duration(duration)} / "
                f"{self._format_media_duration(duration)}"
            )
            self.preview_media_play_button.configure(text="重播")
            self.status_var.set(f"播放完成：{preview.source_path.name}")
            self._redraw_media_visual()
            return
        elif state is PlaybackState.ERROR:
            self._report_preview_playback_error(
                PlaybackError("LibVLC 无法继续播放当前媒体。")
            )
            return
        if keep_polling:
            self._schedule_preview_progress()

    def _begin_preview_scrub(self, _event=None) -> None:
        self._cancel_preview_video_auto_prime()
        self._preview_scrubbing = True
        playback = self.preview_playback
        self._preview_resume_after_scrub = False
        if playback is None:
            return
        try:
            if self._preview_video_prime_phase != "idle":
                # The prime player is actively decoding while muted. Keep it
                # muted until pause has taken effect; restoring audio first can
                # produce a short audible burst on LibVLC's output thread.
                self._cancel_preview_video_prime(restore_audio=False)
                playback.pause()
                return
            state = playback.state()
            self._preview_resume_after_scrub = state in {
                PlaybackState.OPENING,
                PlaybackState.BUFFERING,
                PlaybackState.PLAYING,
            }
            if self._preview_resume_after_scrub:
                playback.pause()
        except PlaybackError as exc:
            self._report_preview_playback_error(exc)

    def _finish_preview_scrub(self, _event=None) -> None:
        value = max(self.preview_media_progress_var.get(), 0.0)
        resume = self._preview_resume_after_scrub
        self._preview_scrubbing = False
        self._preview_resume_after_scrub = False
        playback = self.preview_playback
        if playback is None:
            duration = (
                max(self.preview_media.duration_seconds, 0.0)
                if self.preview_media is not None
                else 0.0
            )
            self.preview_media_time_var.set(
                f"{self._format_media_duration(value)} / "
                f"{self._format_media_duration(duration)}"
            )
            if self.preview_media is not None and self.preview_media.kind == "video":
                self._prime_preview_video_frame(value)
            else:
                self._preview_pending_seek = value
            return
        try:
            if self.preview_media is not None and self.preview_media.kind == "video":
                if resume:
                    self._cancel_preview_video_prime(restore_audio=True)
                    state = playback.state()
                    if state not in {PlaybackState.PLAYING, PlaybackState.PAUSED}:
                        self._preview_pending_seek = value
                        playback.play()
                    else:
                        playback.seek_seconds(value)
                        playback.play()
                    self.preview_media_play_button.configure(text="暂停")
                    self._schedule_preview_progress()
                else:
                    self._prime_preview_video_frame(value)
                return
            state = playback.state()
            if state not in {PlaybackState.PLAYING, PlaybackState.PAUSED}:
                self._preview_pending_seek = value
                if resume:
                    playback.play()
                self._schedule_preview_progress()
                return
            playback.seek_seconds(value)
            if resume:
                playback.play()
            state = playback.state()
            self.preview_media_play_button.configure(
                text=(
                    "暂停"
                    if resume
                    else "继续" if state is PlaybackState.PAUSED else "播放"
                )
            )
            self._schedule_preview_progress()
        except Exception as exc:
            self._report_preview_playback_error(exc)

    def _prime_preview_video_frame(self, seconds: float) -> None:
        preview = self.preview_media
        if preview is None or preview.kind != "video":
            return
        self._cancel_preview_video_auto_prime()
        duration = max(preview.duration_seconds, 0.0)
        timestamp = max(float(seconds), 0.0)
        if duration > 0.0:
            timestamp = min(timestamp, max(duration - 0.05, 0.0))
        self._cancel_preview_video_prime(restore_audio=False)
        try:
            playback = self._ensure_preview_playback()
            playback.set_volume(0)
            playback.set_muted(True)
            playback.play()
        except Exception as exc:
            self._fail_preview_video_prime(f"无法启动视频定位：{exc}")
            return

        self._preview_video_prime_generation += 1
        generation = self._preview_video_prime_generation
        self._preview_video_prime_target = timestamp
        self._preview_video_prime_phase = "opening"
        self._preview_video_prime_deadline = time.monotonic() + 5.0
        self._preview_video_prime_seeked_at = 0.0
        self._preview_video_prime_origin_position = None
        self._preview_video_prime_ready_polls = 0
        self.preview_media_play_button.configure(text="定位中…")
        self.status_var.set(
            f"正在定位视频画面：{self._format_media_duration(timestamp)}"
        )
        self._schedule_preview_video_prime(generation)

    def _schedule_preview_video_prime(
        self,
        generation: int,
        *,
        delay_ms: int = 35,
    ) -> None:
        if (
            generation != self._preview_video_prime_generation
            or self._preview_video_prime_phase == "idle"
            or self._preview_video_prime_after_id is not None
        ):
            return
        self._preview_video_prime_after_id = self.root.after(
            delay_ms,
            lambda: self._poll_preview_video_prime(generation),
        )

    def _poll_preview_video_prime(self, generation: int) -> None:
        self._preview_video_prime_after_id = None
        if (
            generation != self._preview_video_prime_generation
            or self._preview_video_prime_phase == "idle"
        ):
            return
        playback = self.preview_playback
        preview = self.preview_media
        target = self._preview_video_prime_target
        if (
            playback is None
            or preview is None
            or preview.kind != "video"
            or target is None
        ):
            self._cancel_preview_video_prime(restore_audio=False)
            return
        now = time.monotonic()
        if now >= self._preview_video_prime_deadline:
            self._fail_preview_video_prime(
                "视频画面定位超时；仍可点击播放，或稍后再次拖动进度条。"
            )
            return
        try:
            state = playback.state()
            if state is PlaybackState.ERROR:
                raise PlaybackError("LibVLC 解码视频时发生错误。")
            if self._preview_video_prime_phase == "opening":
                if state in {PlaybackState.PLAYING, PlaybackState.PAUSED}:
                    if state is PlaybackState.PAUSED:
                        playback.play()
                    if playback.is_seekable():
                        self._preview_video_prime_origin_position = (
                            playback.position()
                        )
                        playback.seek_seconds(target)
                        self._preview_video_prime_phase = "seeking"
                        self._preview_video_prime_seeked_at = now
                        self._preview_video_prime_ready_polls = 0
            elif self._preview_video_prime_phase == "seeking":
                elapsed = now - self._preview_video_prime_seeked_at
                position = playback.position()
                origin = self._preview_video_prime_origin_position
                left_target = target - 0.08
                right_target = target + 0.50
                moved_from_old_frame = (
                    origin is None
                    or abs(position - origin) >= 0.08
                    or abs(origin - target) <= 0.08
                )
                position_ready = (
                    left_target <= position <= right_target
                    and moved_from_old_frame
                )
                self._preview_video_prime_ready_polls = (
                    self._preview_video_prime_ready_polls + 1
                    if position_ready
                    else 0
                )
                if (
                    elapsed >= 0.18
                    and self._preview_video_prime_ready_polls >= 2
                    and playback.has_video_output()
                ):
                    playback.pause()
                    self._preview_video_prime_phase = "pausing"
            elif self._preview_video_prime_phase == "pausing":
                # set_pause(1) is an asynchronous state request. Do not unmute
                # until LibVLC reports that output has actually paused.
                if state is PlaybackState.PAUSED:
                    self._finish_preview_video_prime(target)
                    return
        except PlaybackError as exc:
            self._fail_preview_video_prime(f"视频画面定位失败：{exc}")
            return
        self._schedule_preview_video_prime(generation)

    def _finish_preview_video_prime(self, target: float) -> None:
        playback = self.preview_playback
        if playback is not None:
            self._restore_preview_playback_audio(playback)
        duration = (
            max(self.preview_media.duration_seconds, 0.0)
            if self.preview_media is not None
            else 0.0
        )
        self.preview_media_progress_var.set(target)
        self.preview_media_time_var.set(
            f"{self._format_media_duration(target)} / "
            f"{self._format_media_duration(duration)}"
        )
        self._cancel_preview_video_prime(restore_audio=False)
        self.preview_media_play_button.configure(
            text="继续" if self._preview_video_user_started else "播放"
        )
        self.status_var.set(
            f"已定位视频画面：{self._format_media_duration(target)}"
        )

    def _fail_preview_video_prime(self, message: str) -> None:
        # A timeout/error means we cannot prove that pause has reached the
        # native output thread. Close the still-muted player so recovery cannot
        # leak audio; the next Play action creates a fresh controller.
        self._cancel_preview_video_prime(restore_audio=False)
        if self.preview_playback is not None:
            self._close_preview_playback(clear_pending=False)
        current = self.preview_media_warning_var.get().strip()
        if message and message not in current:
            self.preview_media_warning_var.set(
                "\n".join(value for value in (current, message) if value)
            )
        self.preview_media_play_button.configure(
            text="继续" if self._preview_video_user_started else "播放"
        )
        self.status_var.set(message)
        self._redraw_media_visual()

    def _cancel_preview_video_prime(self, *, restore_audio: bool) -> None:
        after_id = self._preview_video_prime_after_id
        self._preview_video_prime_after_id = None
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except (RuntimeError, tk.TclError):
                pass
        self._preview_video_prime_generation += 1
        self._preview_video_prime_target = None
        self._preview_video_prime_phase = "idle"
        self._preview_video_prime_deadline = 0.0
        self._preview_video_prime_seeked_at = 0.0
        self._preview_video_prime_origin_position = None
        self._preview_video_prime_ready_polls = 0
        if restore_audio and self.preview_playback is not None:
            self._restore_preview_playback_audio(self.preview_playback)

    def _restore_preview_playback_audio(
        self,
        playback: MediaPlaybackController,
    ) -> None:
        """Restore user volume while preserving mute until volume is ready."""

        try:
            playback.set_volume(self.preview_media_volume_var.get())
        except PlaybackError:
            pass
        try:
            playback.set_muted(False)
        except PlaybackError:
            pass

    def _preview_tabs_are_visible(self) -> bool:
        try:
            return (
                self.primary_nb.select() == str(self.workspace_page)
                and self.nb.select() == str(self.preview_tab)
            )
        except (AttributeError, RuntimeError, tk.TclError):
            return False

    def _schedule_preview_video_auto_prime(
        self,
        preview: MediaPreview | None = None,
    ) -> None:
        self._cancel_preview_video_auto_prime()
        current = self.preview_media if preview is None else preview
        if (
            current is None
            or current is not self.preview_media
            or current.kind != "video"
            or not self._preview_tabs_are_visible()
        ):
            return

        def run() -> None:
            self._preview_video_auto_prime_after_id = None
            if (
                self.preview_media is current
                and current.kind == "video"
                and self.preview_playback is None
                and self._preview_video_prime_phase == "idle"
                and not self._preview_video_user_started
                and self._preview_tabs_are_visible()
            ):
                self._prime_preview_video_frame(
                    max(self.preview_media_progress_var.get(), 0.0)
                )

        self._preview_video_auto_prime_after_id = self.root.after_idle(run)

    def _cancel_preview_video_auto_prime(self) -> None:
        after_id = self._preview_video_auto_prime_after_id
        self._preview_video_auto_prime_after_id = None
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except (RuntimeError, tk.TclError):
                pass

    def _change_preview_volume(self, value: str) -> None:
        try:
            applied = max(0, min(100, round(float(value))))
        except (TypeError, ValueError):
            return
        self.preview_media_volume_text_var.set(f"{applied}%")
        playback = self.preview_playback
        if playback is None or self._preview_video_prime_phase != "idle":
            return
        try:
            playback.set_volume(applied)
        except PlaybackError as exc:
            self.status_var.set(f"音量调整失败：{exc}")

    def _report_preview_playback_error(self, exc: Exception) -> None:
        self._close_preview_playback()
        message = str(exc)
        current = self.preview_media_warning_var.get().strip()
        if message and message not in current:
            self.preview_media_warning_var.set(
                "\n".join(value for value in (current, message) if value)
            )
        self.status_var.set(f"媒体播放失败：{message}")
        messagebox.showerror("播放失败", message, parent=self.root)
        self._redraw_media_visual()

    def _set_preview_selection(
        self,
        selection: PreviewFileSelection | None,
    ) -> None:
        self.preview_selection = selection
        if hasattr(self, "preview_extract_button"):
            self.preview_extract_button.configure(
                state="normal" if selection is not None else "disabled"
            )

    def extract_preview_file(self) -> None:
        selection = self.preview_selection
        if selection is None:
            messagebox.showinfo(
                "尚未选择文件",
                "请先在左侧文件树中选择一个文件。",
                parent=self.root,
            )
            return
        if not self._ensure_idle() or not self._confirm_pending_changes():
            return

        suffix = Path(selection.logical_path).suffix
        if selection.origin == "pac":
            ref = selection.pac_ref
            if ref is None:
                return
            project = self.workbench.get(ref.workspace_id)
            initial_dir = project.workspace.archive.source_path.parent
        else:
            source = selection.file_path
            if source is None:
                return
            initial_dir = source.parent

        filetypes = [("所有文件", "*.*")]
        if suffix:
            filetypes.insert(
                0,
                (f"{suffix.lstrip('.').upper()} 文件", f"*{suffix}"),
            )
        target = filedialog.asksaveasfilename(
            title="提取/另存为",
            initialdir=str(initial_dir),
            initialfile=Path(selection.logical_path).name,
            defaultextension=suffix,
            filetypes=filetypes,
            parent=self.root,
        )
        if not target:
            return

        def worker():
            if selection.origin == "pac":
                ref = selection.pac_ref
                if ref is None:
                    raise RuntimeError("PAC 预览选择已经失效。")
                project = self.workbench.get(ref.workspace_id)
                return project.workspace.export_entry(
                    selection.logical_path,
                    target,
                    prefer_current=True,
                )
            source = selection.file_path
            if source is None:
                raise RuntimeError("解包文件预览选择已经失效。")
            return atomic_copy_file(source, target)

        def completed(output: Path) -> None:
            self.status_var.set(f"已提取：{output}")
            messagebox.showinfo(
                "提取完成",
                f"文件已保存到：\n{output}",
                parent=self.root,
            )

        self._run_background("正在提取所选文件…", worker, completed)

    def on_pac_tree_double_click(self, event) -> None:
        if not self._ensure_idle():
            return
        item = self.pac_tree.identify_row(event.y)
        ref = self.pac_tree_refs.get(item)
        if ref is None:
            return
        if ref.kind != "file":
            self.show_pac_files(ref)
            return "break"
        project = self.workbench.get(ref.workspace_id)
        entry = project.workspace.get_entry(ref.path)
        if not entry.editable:
            self.status_var.set(f"{entry.name} 不是可编辑的 TBL/DAT 文件。")
            return
        self._open_pac_entry(ref)

    def _open_pac_entry(self, ref: PacNodeRef) -> None:
        if (
            self.current_project_id == ref.workspace_id
            and self.current_pac_entry == ref.path
        ):
            self.nb.select(self.single_tab)
            return
        if not self._confirm_pending_changes():
            return
        try:
            project = self.workbench.get(ref.workspace_id)
            document = project.open_entry(
                ref.path,
                options=self._resolve_options(Path(ref.path)),
            )
            self.session = project.document_session
            self.current_origin = "pac"
            self.current_project_id = ref.workspace_id
            self.current_pac_entry = ref.path
            self.current_file = project.workspace.entry_path(ref.path)
            self.current_index = None
            self._load_document_into_single_view(document)
            self.single_meta_var.set(
                f"{project.workspace.archive.source_path.name}  ::  {ref.path}"
                f"　·　{len(document.units)} 条文本"
            )
            self.route_hint_var.set(self._route_message(Path(ref.path), document.engine))
            self.nb.select(self.single_tab)
            self.status_var.set(f"已进入单文件编辑：{ref.path}")
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    def close_selected_pacs(self) -> None:
        if not self._ensure_idle():
            return
        ids = self._selected_workspace_ids()
        if not ids:
            messagebox.showinfo("尚未选择", "请在左侧 PAC 树中选择要关闭的 PAC。")
            return
        if self.current_project_id in ids and not self._confirm_pending_changes():
            return
        dirty = []
        for workspace_id in ids:
            project = self.workbench.get(workspace_id)
            if project.workspace.unbuilt_entry_names():
                dirty.append(project.workspace.archive.source_path.name)
        if dirty and not messagebox.askyesno(
            "关闭 PAC",
            (
                "以下 PAC 含有尚未构建回包的工作区修改：\n"
                + "\n".join(dirty)
                + "\n\n关闭后会话工作区和其中修改将永久销毁。是否继续？"
            ),
            parent=self.root,
        ):
            return
        preview_ref = (
            self.preview_selection.pac_ref
            if self.preview_selection is not None
            and self.preview_selection.origin == "pac"
            else None
        )
        if preview_ref is not None and preview_ref.workspace_id in ids:
            self._close_preview_playback()
        for workspace_id in ids:
            self.workbench.close(workspace_id)
        if self.current_project_id in ids:
            self._clear_single_document_view()
        self.refresh_pac_tree()
        self.refresh_pac_cache()
        self._set_features_active(bool(self.workbench.projects()))
        self.status_var.set(f"已关闭 {len(ids)} 个 PAC。")
        self._refresh_pac_dependent_preview()

    def reload_selected_pac_workspaces(self) -> None:
        """Discard selected session workspaces and re-inspect their sources."""

        if not self._ensure_idle():
            return
        ids = self._selected_workspace_ids()
        if not ids:
            messagebox.showinfo(
                "尚未选择",
                "请在左侧 PAC 树中选择要更新的 PAC 或其任意子节点。",
                parent=self.root,
            )
            return
        if self.current_project_id in ids and not self._confirm_pending_changes():
            return
        stale = [
            self.workbench.get(workspace_id).workspace.archive.source_path.name
            for workspace_id in ids
            if self.workbench.get(workspace_id).workspace.state() == "stale"
        ]
        if stale and not messagebox.askyesno(
            "更新 PAC 工作区",
            (
                "以下源 PAC 的磁盘内容已经变化：\n"
                + "\n".join(stale)
                + "\n\n程序将销毁当前会话工作区，并按磁盘上的当前内容重新打开。"
                "尚未构建回包的修改不会保留。是否继续？"
            ),
            parent=self.root,
        ):
            return
        self._reload_pac_workspaces(ids, discard_cache=False)

    def clear_selected_pac_workspaces(self) -> None:
        """Destroy selected session caches and reopen clean sources."""

        if not self._ensure_idle():
            return
        ids = self._selected_workspace_ids()
        if not ids:
            messagebox.showinfo(
                "尚未选择",
                "请在左侧 PAC 树中选择要清除缓存的 PAC 或其任意子节点。",
                parent=self.root,
            )
            return
        if self.current_project_id in ids and not self._confirm_pending_changes():
            return
        dirty_count = sum(
            len(self.workbench.get(workspace_id).workspace.dirty_entry_names())
            for workspace_id in ids
        )
        warning = (
            f"\n\n其中包含 {dirty_count} 个尚未回包的修改条目；这些修改将退出当前工作流。"
            if dirty_count
            else ""
        )
        if not messagebox.askyesno(
            "清除 PAC 工作区缓存",
            (
                f"将清除 {len(ids)} 个所选 PAC 的物化缓存，并从源 PAC 重新建立干净工作区。"
                f"{warning}\n\n会话缓存将直接销毁，不能恢复。是否继续？"
            ),
            parent=self.root,
        ):
            return
        self._reload_pac_workspaces(ids, discard_cache=True)

    def _reload_pac_workspaces(
        self,
        workspace_ids: list[str],
        *,
        discard_cache: bool,
    ) -> None:
        old_ids = set(workspace_ids)
        preview_ref = (
            self.preview_selection.pac_ref
            if self.preview_selection is not None
            and self.preview_selection.origin == "pac"
            else None
        )
        if preview_ref is not None and preview_ref.workspace_id in old_ids:
            self._close_preview_playback()

        def worker():
            refreshed: list[tuple[str, str]] = []
            errors: list[str] = []
            for workspace_id in workspace_ids:
                try:
                    project = self.workbench.reload_from_source(
                        workspace_id,
                        discard_cache=discard_cache,
                    )
                    refreshed.append((workspace_id, project.workspace.workspace_id))
                except Exception as exc:
                    errors.append(f"{workspace_id}：{exc}")
            return refreshed, errors

        def completed(result) -> None:
            refreshed, errors = result
            if self.current_project_id in old_ids:
                self._clear_single_document_view()
            self.batch_hits = []
            self.batch_scan_options = None
            self.batch_scan_complete = False
            self._refresh_hit_tree(self.batch_hit_tree, self.batch_hits)
            self.refresh_pac_tree()
            self.refresh_pac_cache()
            self._set_features_active(bool(self.workbench.projects()))
            self._refresh_pac_dependent_preview()
            action = "清除缓存并重新载入" if discard_cache else "更新"
            self.status_var.set(f"已{action} {len(refreshed)} 个 PAC 工作区。")
            if errors:
                messagebox.showwarning(
                    "部分 PAC 工作区处理失败",
                    "\n".join(errors),
                    parent=self.root,
                )

        self._run_background(
            "正在清除并重建 PAC 工作区…"
            if discard_cache
            else "正在更新 PAC 工作区…",
            worker,
            completed,
        )

    def build_selected_pac(self, workspace_id=None) -> None:
        if not self._ensure_idle():
            return
        ids = [workspace_id] if workspace_id else self._selected_workspace_ids()
        if len(ids) != 1:
            messagebox.showinfo(
                "请选择一个 PAC",
                "请在左侧树中选择且只选择一个 PAC（或它的任意子节点）。",
            )
            return
        workspace_id = ids[0]
        if self.current_project_id == workspace_id and not self._confirm_pending_changes():
            return
        project = self.workbench.get(workspace_id)
        source = project.workspace.archive.source_path
        target = filedialog.asksaveasfilename(
            title="构建 PAC",
            initialdir=str(source.parent),
            initialfile=f"{source.stem}.rebuilt.pac",
            defaultextension=".pac",
            filetypes=[("PAC 文件", "*.pac"), ("所有文件", "*.*")],
        )
        if not target:
            return

        self._start_workspace_pac_build(project, target)

    def _start_workspace_pac_build(
        self,
        project,
        target: str | Path,
        *,
        allow_integrity_errors: bool = False,
        known_failures: tuple[str, ...] = (),
    ) -> None:

        def worker():
            try:
                return (
                    "built",
                    project.build(
                        target,
                        allow_integrity_errors=allow_integrity_errors,
                    ),
                )
            except PacWorkspaceIntegrityError as exc:
                return "integrity-error", exc

        def completed(result) -> None:
            kind, payload = result
            if kind == "integrity-error":
                failures = tuple(payload.failures)
                preview = "\n".join(failures[:12])
                remainder = len(failures) - 12
                if remainder > 0:
                    preview += f"\n……另有 {remainder} 个失败条目。"
                if messagebox.askyesno(
                    "检测到完整性错误，仍然导出？",
                    (
                        f"候选 PAC 中有 {len(failures)} 个无法严格解析的 TBL/DAT：\n\n"
                        f"{preview}\n\n"
                        "继续后会原样保留这些错误条目并完成 PAC 导出；输出可能仍会"
                        "卡死或显示错误文本。本次修改不会自动视为修复。\n\n"
                        "是否明确承担风险并继续另存？"
                    ),
                    parent=self.root,
                ):
                    self.root.after(
                        0,
                        lambda: self._start_workspace_pac_build(
                            project,
                            target,
                            allow_integrity_errors=True,
                            known_failures=failures,
                        ),
                    )
                else:
                    self.status_var.set("已取消带完整性错误的 PAC 导出。")
                return

            report = payload
            self.refresh_pac_tree()
            self.refresh_pac_cache()
            details = (
                f"输出：{report.output_path}\n"
                f"条目：{report.entry_count}\n"
                f"写入修改：{len(report.changed_entries)}\n"
                f"大小：{self._format_bytes(report.output_size)}"
            )
            if known_failures:
                messagebox.showwarning(
                    "PAC 已带错误导出",
                    details
                    + f"\n\n已明确保留 {len(known_failures)} 个完整性错误条目。",
                    parent=self.root,
                )
            else:
                messagebox.showinfo(
                    "构建完成",
                    details,
                    parent=self.root,
                )
            self.status_var.set(f"PAC 构建完成：{report.output_path.name}")

        self._run_background(
            "正在带警告构建 PAC…"
            if allow_integrity_errors
            else "正在校验并构建 PAC…",
            worker,
            completed,
        )

    def _selected_workspace_ids(self) -> list[str]:
        ids: list[str] = []
        for ref in self.selected_pac_refs():
            if ref.workspace_id not in ids:
                ids.append(ref.workspace_id)
        return ids

    def current_engine_preferences(self) -> tuple[str, str]:
        return (
            self.tbl_engine_label_to_value[self.tbl_engine_var.get()],
            self.dat_engine_label_to_value[self.dat_engine_var.get()],
        )

    def on_engine_preferences_changed(self, _event=None) -> None:
        selected = self.current_engine_preferences()
        if selected == self._previous_engine_preferences:
            return
        previous_tbl, previous_dat = self._previous_engine_preferences
        if not self._ensure_idle() or not self._confirm_pending_changes():
            self.tbl_engine_var.set(self.tbl_engine_value_to_label[previous_tbl])
            self.dat_engine_var.set(self.dat_engine_value_to_label[previous_dat])
            return
        self._previous_engine_preferences = selected
        logical = Path(self.current_pac_entry) if self.current_pac_entry else None
        self.route_hint_var.set(self._route_message(logical))
        self.batch_backend_hint_var.set(self._route_message())
        self.batch_hits = []
        self.batch_scan_options = None
        self.batch_scan_complete = False
        self._refresh_hit_tree(self.batch_hit_tree, self.batch_hits)
        self.batch_progress_text_var.set("解析策略已改变，请重新扫描批量范围。")
        try:
            self._reload_current_for_parsing_options()
            self._reload_preview_for_parsing_options()
        except Exception as exc:
            self._previous_engine_preferences = (previous_tbl, previous_dat)
            self.tbl_engine_var.set(self.tbl_engine_value_to_label[previous_tbl])
            self.dat_engine_var.set(self.dat_engine_value_to_label[previous_dat])
            self.route_hint_var.set(self._route_message(logical))
            self.batch_backend_hint_var.set(self._route_message())
            try:
                self._reload_current_for_parsing_options()
                self._reload_preview_for_parsing_options()
            except Exception:
                pass
            messagebox.showerror(
                "切换文本引擎失败",
                f"无法按新的全局引擎选项重新解析当前文件：{exc}",
                parent=self.root,
            )
            return
        self.status_var.set(
            f"全局文本引擎已更新：TBL={selected[0]}，DAT={selected[1]}；"
            "批量结果已清空，请重新扫描。"
        )

    def on_risky_repack_changed(self) -> None:
        """Invalidate preflight results when write authorization changes."""

        enabled = bool(self.allow_risky_repack_var.get())
        self.session.options.allow_risky_repack = enabled
        self.batch_hits = []
        self.batch_scan_options = None
        self.batch_scan_complete = False
        self._refresh_hit_tree(self.batch_hit_tree, self.batch_hits)
        self._reset_task_progress(
            self.batch_progress,
            self.batch_progress_text_var,
            "启发式回退写入设置已改变，请重新扫描批量范围。",
        )
        logical = Path(self.current_pac_entry or self.current_file or "")
        self.route_hint_var.set(self._route_message(logical))
        self.batch_backend_hint_var.set(self._route_message())
        self.status_var.set(
            "启发式回退写入已开启；仅在结构化算法不可用时启用，保存前仍会确认。"
            if enabled
            else "启发式回退写入已关闭；TBL/#scp DAT 的结构化变长写入不受影响。"
        )

    def _resolve_options(
        self,
        logical_path: Path,
        *,
        for_pac: bool = True,
    ) -> SessionOptions:
        tbl_engine, dat_engine = self.current_engine_preferences()
        return SessionOptions(
            mode=WorkflowMode.SAFE,
            engine_override="auto",
            tbl_engine=tbl_engine,
            dat_engine=dat_engine,
            game=self.current_game_version(),
            schema_hint=self.schema_hint_var.get().strip(),
            keep_artifacts=self.keep_artifacts_var.get(),
            do_backup=not for_pac,
            allow_risky_repack=self.allow_risky_repack_var.get(),
        )

    def _confirm_experimental_save(self, session: DocumentSession) -> bool:
        """Require an explicit per-save acknowledgement for unsafe rebuilds."""

        plan = session.preview_save()
        if plan.safe or not session.changed_count():
            return True
        experimental_modes = {
            "legacy": {"repack"},
            "kuro_dat": {"script-roundtrip"},
        }
        eligible = bool(
            plan.requires_rebuild
            and session.document is not None
            and plan.mode in experimental_modes.get(session.document.engine, set())
        )
        if not eligible:
            messagebox.showerror(
                "当前修改不可写",
                "\n".join(plan.notes) or "当前后端无法安全写回这些修改。",
                parent=self.root,
            )
            return False
        if not session.options.allow_risky_repack:
            messagebox.showwarning(
                "需要启发式回退授权",
                (
                    "当前文件无法建立完整结构布局，只能使用启发式回退。"
                    "请先勾选“允许启发式回退写入”，"
                    "再重新保存。\n\n" + "\n".join(plan.notes)
                ),
                parent=self.root,
            )
            return False
        return messagebox.askyesno(
            "确认启发式回退写入",
            (
                "该文件将先写入同目录暂存文件，并进行完整文本回读；#scp DAT 还会比较"
                "非文本脚本结构。验证失败不会覆盖当前文件。\n\n"
                "这些门禁仍不足以证明所有未知字段或游戏运行语义完全不变。"
                "请确保保留原 PAC/备份，并在游戏中验证。\n\n"
                + "\n".join(plan.notes)
                + "\n\n是否继续？"
            ),
            parent=self.root,
        )

    def save_single(self) -> None:
        if not self._ensure_idle():
            return
        if self.session.document is None:
            messagebox.showinfo("尚未打开文件", "请从左侧文件树双击 TBL/DAT 文件。")
            return
        try:
            self._commit_current_edit()
            self.session.options.allow_risky_repack = bool(
                self.allow_risky_repack_var.get()
            )
            if not self._confirm_experimental_save(self.session):
                return
            if self.current_origin == "pac":
                project = self.workbench.get(self.current_project_id or "")
                saved = project.save_current()
                self.session = project.document_session
                self.refresh_pac_tree()
                message = f"已保存到 PAC 工作区：{saved.name}"
            else:
                saved = self.session.save()
                self.current_file = saved.resolve()
                message = f"已保存文件：{saved}"
            self._load_document_into_single_view(self.session.document)
            self.status_var.set(message)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)

    def export_single(self) -> None:
        if not self._ensure_idle():
            return
        if self.session.document is None or self.current_file is None:
            messagebox.showinfo("尚未打开文件", "请从左侧文件树双击 TBL/DAT 文件。")
            return
        logical_name = self.current_pac_entry or self.current_file.name
        suffix = Path(logical_name).suffix
        target = filedialog.asksaveasfilename(
            title="导出当前文件副本" if self.current_origin == "pac" else "另存为",
            initialfile=Path(logical_name).name,
            defaultextension=suffix,
            filetypes=[(suffix.upper().lstrip(".") + " 文件", f"*{suffix}"), ("所有文件", "*.*")],
        )
        if not target:
            return
        try:
            self._commit_current_edit()
            self.session.options.allow_risky_repack = bool(
                self.allow_risky_repack_var.get()
            )
            if not self._confirm_experimental_save(self.session):
                return
            if self.current_origin == "pac":
                project = self.workbench.get(self.current_project_id or "")
                output = project.export_current(target)
                self.session = project.document_session
                self.refresh_pac_tree()
            else:
                output = self.session.save_as(target)
                self.current_file = output.resolve()
            self._load_document_into_single_view(self.session.document)
            self.status_var.set(f"已导出：{output}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)

    def preview_single(self) -> None:
        if self.session.document is None:
            return
        try:
            self._commit_current_edit()
            plan = self.session.preview_save()
            notes = "\n".join(plan.notes) if plan.notes else "无额外提示"
            permission = (
                "已允许（保存时仍需确认）"
                if self.allow_risky_repack_var.get()
                else "未允许"
            )
            messagebox.showinfo(
                "保存预检",
                (
                    f"引擎：{plan.engine}\n模式：{plan.mode}\n"
                    f"安全标记：{'是' if plan.safe else '否'}\n"
                    f"需要重建：{'是' if plan.requires_rebuild else '否'}\n"
                    f"启发式回退授权：{permission}\n\n{notes}"
                ),
                parent=self.root,
            )
        except Exception as exc:
            messagebox.showerror("预检失败", str(exc), parent=self.root)

    def on_single_select(self, _event=None) -> None:
        selection = self.single_tree.selection()
        if not selection or self.session.document is None:
            return
        item = selection[0]
        index = self.single_row_indices.get(item)
        if index is None:
            return
        self.current_index = index

    def apply_single(self) -> None:
        self._close_single_editor(save=True)

    def _on_single_double_click(self, event) -> str | None:
        item = self.single_tree.identify_row(event.y)
        column = self.single_tree.identify_column(event.x)
        if item and column and self.single_tree.column(column, "id") == "current":
            self._begin_single_edit(item)
            return "break"
        return None

    def _edit_selected_single_cell(self) -> None:
        selection = self.single_tree.selection()
        if selection:
            self._begin_single_edit(selection[0])

    def _begin_single_edit(self, item: str) -> None:
        if not self._ensure_idle():
            return
        if self.session.document is None:
            return
        self._close_single_editor(save=True)
        # A preview/batch jump can have just mapped the single-file tab. Finish
        # its geometry before placing the editor, or Configure closes it again.
        self.single_tree.update_idletasks()
        self.single_tree.see(item)
        bounds = self.single_tree.bbox(item, "current")
        index = self.single_row_indices.get(item)
        if not bounds or index is None:
            return
        x, y, width, height = bounds
        unit = self.session.document.get_unit(index)
        editor_height = min(self.single_tree.max_rowheight, max(self.single_tree.base_rowheight * 3, height))
        available = max(1, self.single_tree.winfo_height() - y - 4)
        editor_height = min(editor_height, available)
        editor = tk.Text(
            self.single_tree,
            wrap="word",
            font="TkTextFont",
            undo=True,
            bg="#fffdf9",
            fg="#30271f",
            insertbackground="#30271f",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=4,
        )
        editor.insert("1.0", unit.current_text)
        editor.place(x=x, y=y, width=width, height=editor_height)
        editor.focus_set()
        match = getattr(self, "_single_match", None)
        if match is not None and match[0] == item and unit.current_text[match[1]:match[2]] == match[3]:
            # Text's +N chars navigation counts Unicode characters (unlike
            # Tcl string length, which can count a non-BMP character twice).
            start, end = match[1:3]
            editor.tag_add("sel", f"1.0+{start}c", f"1.0+{end}c")
            editor.mark_set("insert", f"1.0+{start}c")
            editor.see("insert")
            editor.after_idle(lambda: editor.see("insert") if editor.winfo_exists() else None)
        else:
            editor.tag_add("sel", "1.0", "end-1c")
        editor.bind("<Return>", lambda _event: self._close_single_editor(save=True))
        editor.bind("<Shift-Return>", self._insert_single_editor_newline)
        editor.bind("<Escape>", lambda _event: self._close_single_editor(save=False))
        editor.bind("<FocusOut>", lambda _event: self._close_single_editor(save=True))
        self.single_editor = editor
        self.single_editor_item = item
        self.current_index = index

    def _insert_single_editor_newline(self, _event) -> str:
        if self.single_editor is not None:
            self.single_editor.insert("insert", "\n")
        return "break"

    def _close_single_editor(self, *, save: bool) -> str:
        editor = self.single_editor
        item = self.single_editor_item
        if editor is None or item is None:
            return "break"
        value = editor.get("1.0", "end-1c")
        self.single_editor = None
        self.single_editor_item = None
        if save and self.session.document is not None and self.single_tree.exists(item):
            index = self.single_row_indices.get(item)
            if index is not None:
                self.session.update_unit(index, value)
                unit = self.session.document.get_unit(index)
                values = list(self.single_tree.item(item, "values"))
                if values[3] != unit.current_text:
                    values[3] = unit.current_text
                    self._single_match = None
                    self.single_tree.item(
                        item, values=tuple(values),
                        tags=("changed",) if unit.changed else (),
                    )
        editor.destroy()
        return "break"

    def find_next(self, direction: int = 1) -> None:
        if self.session.document is None:
            return
        self._commit_current_edit()
        keyword = _clean_search_input(self.find_var.get())
        self._single_match = None
        self.single_tree.clear_contexts()
        if not keyword:
            return
        if keyword != self.find_var.get():
            self.find_var.set(keyword)
        case_sensitive = self.case_var.get()
        signature = (keyword, case_sensitive)
        results = self.session.find_matches(keyword, case_sensitive=case_sensitive)
        if not results:
            self.search_results = []
            self.search_cursor = -1
            self.search_signature = signature
            self.status_var.set("没有找到匹配文本。")
            return
        if signature != self.search_signature or results != self.search_results:
            self.search_signature = signature
            self.search_results = results
            self.search_cursor = -1 if direction > 0 else 0
        self.search_cursor = (self.search_cursor + direction) % len(results)
        target, start, end = results[self.search_cursor]
        item = next(
            (iid for iid, index in self.single_row_indices.items() if index == target),
            None,
        )
        unit = self.session.document.get_unit(target)
        if item:
            self._show_single_match(item, (start, end))
            self.single_tree.selection_set(item)
            self.single_tree.focus(item)
            self.single_tree.see(item)
        suffix = "；继续点击可循环查找" if len(results) > 1 else ""
        self.status_var.set(
            f"匹配 {self.search_cursor + 1}/{len(results)}：{unit.location}{suffix}；双击当前文本查看全文。"
        )

    def replace_all(self) -> None:
        if not self._ensure_idle():
            return
        find_text = _clean_search_input(self.find_var.get())
        if self.session.document is None or not find_text:
            return
        self._commit_current_edit()
        if find_text != self.find_var.get():
            self.find_var.set(find_text)
        count = self.session.replace_all(
            find_text,
            self.replace_var.get(),
            case_sensitive=self.case_var.get(),
        )
        self._refresh_single_document()
        destination = (
            "PAC 工作区" if self.current_origin == "pac" else "当前文件"
        )
        self.status_var.set(f"已修改 {count} 个文本条目，尚未保存到{destination}。")

    def export_batch_behavior(self) -> None:
        if not self.batch_hits:
            messagebox.showinfo(
                "没有处理行",
                "请先扫描命中，再导出逐次处理选择。",
                parent=self.root,
            )
            return
        target = filedialog.asksaveasfilename(
            title="导出批量处理选择",
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if not target:
            return
        payload = serialize_batch_behavior(self.batch_hits)
        Path(target).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.batch_behavior_operations = load_batch_behavior(payload)
        self.status_var.set(f"已导出 {len(self.batch_hits)} 条逐次处理选择。")

    def import_batch_behavior(self) -> None:
        target = filedialog.askopenfilename(
            title="导入批量处理选择",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if not target:
            return
        try:
            operations = load_batch_behavior(Path(target).read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc), parent=self.root)
            return
        self.batch_behavior_operations = operations
        applied = apply_batch_behavior(self.batch_hits, operations)
        self._refresh_hit_tree(self.batch_hit_tree, self.batch_hits)
        if self.batch_hits:
            message = f"已导入处理选择；当前命中匹配 {applied}/{len(self.batch_hits)} 条。"
        else:
            message = "已导入处理选择；将在下一次扫描后按逐次命中应用。"
        self.batch_progress_text_var.set(message)
        self.status_var.set(message)

    def _batch_business_targets(self) -> list[BusinessFileTarget]:
        targets: list[BusinessFileTarget] = []
        for target in self.batch_targets:
            if isinstance(target, PacMaterializedEntry):
                targets.append(
                    BusinessFileTarget(
                        path=str(target.path),
                        logical_path=target.entry_name,
                        source_id=target.workspace_id,
                    )
                )
            elif isinstance(target, BusinessFileTarget):
                targets.append(target)
        return targets

    def close_batch(self) -> None:
        if not self._ensure_idle():
            return
        self.batch_hits = []
        self.batch_targets = []
        self.batch_target_by_file = {}
        self.batch_behavior_operations = []
        self.batch_scan_options = None
        self.batch_scan_pairs = None
        self.batch_scan_complete = False
        self._refresh_hit_tree(self.batch_hit_tree, [])
        self._set_text(self.batch_log, "", readonly=True)
        self._reset_task_progress(
            self.batch_progress, self.batch_progress_text_var,
            "批量结果已关闭；映射和处理范围已保留。",
        )
        self.nb.select(self.preview_tab)
        self.status_var.set("已关闭批量结果；已写入的工作区修改不会撤销。")

    def scan_batch(self) -> None:
        resource_mode = self.current_resource_mode()
        refs = self.batch_sources.get_refs() if resource_mode == "pac" else []
        unpacked_roots = (
            self.unpacked_batch_sources.get_paths()
            if resource_mode == "unpacked"
            else []
        )
        pairs = self.batch_mapping.get_pairs()
        if self.batch_hits:
            self.batch_behavior_operations = load_batch_behavior(
                serialize_batch_behavior(self.batch_hits)
            )
        if resource_mode == "pac" and not refs:
            messagebox.showinfo("范围为空", "请从左侧 PAC 树添加处理范围。")
            return
        if resource_mode == "unpacked" and not unpacked_roots:
            messagebox.showinfo("范围为空", "请从左侧解包文件树添加处理范围。")
            return
        if not pairs:
            messagebox.showinfo("映射为空", "请添加至少一条启用的替换映射。")
            return
        if not self._confirm_pending_changes() or not self._ensure_idle():
            return
        batch_globs = self.batch_globs_var.get()
        patterns = self._split_globs(batch_globs)
        batch_options = self._resolve_options(Path("batch.tbl"))
        batch_options.schema_hint = ""
        def worker():
            if resource_mode == "pac":
                display_targets = self.workbench.materialize_refs(
                    refs,
                    editable_only=True,
                )
                display_targets = [
                    target
                    for target in display_targets
                    if self._logical_matches(target.entry_name, patterns)
                ]
                targets = [
                    BusinessFileTarget(
                        path=str(item.path),
                        logical_path=item.entry_name,
                        source_id=item.workspace_id,
                    )
                    for item in display_targets
                ]
            else:
                targets = self.business.collect_file_targets(
                    unpacked_roots,
                    batch_globs,
                )
                display_targets = targets
            scan = self.business.scan_mixed_batch_targets(
                targets,
                batch_globs,
                pairs,
                use_equal_fast_path=False,
                options=batch_options,
                ordered=True,
            )
            return display_targets, scan

        def completed(result) -> None:
            display_targets, scan = result
            self.batch_targets = display_targets
            self.batch_target_by_file = {
                str(target.path): target for target in display_targets
            }
            self.batch_hits = scan.hits
            self.batch_scan_options = batch_options
            self.batch_scan_pairs = tuple(pairs)
            self.batch_scan_complete = scan.complete
            applied = apply_batch_behavior(
                self.batch_hits,
                self.batch_behavior_operations,
            )
            self._refresh_hit_tree(self.batch_hit_tree, self.batch_hits)
            if resource_mode == "pac":
                self.refresh_pac_tree()
            writable_count = sum(hit.writable for hit in scan.hits)
            blocked_count = len(scan.hits) - writable_count
            search_only_count = sum(
                getattr(hit, "write_mode", "") == "search-only"
                for hit in scan.hits
            )
            state_label = "扫描完整" if scan.complete else "扫描不完整（禁止执行替换）"
            lines = [
                (
                    f"{state_label}：请求 {scan.target_count} 个 TBL/DAT，"
                    f"成功解析 {scan.parsed_file_count} 个，"
                    f"检查 {scan.text_unit_count} 个文本单元，"
                    f"找到 {len(scan.hits)} 个文本匹配（可替换 {writable_count}，"
                    f"未变更 {search_only_count}）。"
                ),
                (
                    "映射按上到下逐条执行；下方规则搜索的是上方规则处理后的文本。"
                    f"可写 {writable_count}，只读/需跳过 {blocked_count}。"
                ),
            ]
            if self.batch_behavior_operations:
                lines.append(f"已从处理选择方案匹配 {applied} 行。")
            lines.extend(scan.errors)
            self._set_text(self.batch_log, "\n".join(lines), readonly=True)
            self.batch_progress_text_var.set(lines[0])
            self.status_var.set(lines[0])
            if not scan.complete:
                messagebox.showerror(
                    "批量搜索不完整",
                    (
                        "所选范围中至少有一个 TBL/DAT 未能完成解析或搜索。\n\n"
                        "当前页面保留部分命中仅供诊断，执行替换已被禁止。"
                        "请查看日志中的 [INCOMPLETE] 项，修复源文件或调整引擎后重新扫描。"
                    ),
                    parent=self.root,
                )

        label = (
            "正在扫描 PAC 文本…"
            if resource_mode == "pac"
            else "正在扫描解包文件…"
        )
        self._run_background(
            label,
            worker,
            completed,
            progressbar=self.batch_progress,
            progress_text_var=self.batch_progress_text_var,
        )

    def exec_batch(self) -> None:
        if tuple(self.batch_mapping.get_pairs()) != getattr(self, "batch_scan_pairs", None):
            messagebox.showinfo("请重新查找", "映射内容或顺序已改变，请重新查找匹配后再执行。", parent=self.root)
            return
        if not self.batch_scan_complete:
            messagebox.showerror(
                "不能执行批量替换",
                (
                    "尚未取得覆盖全部所选 TBL/DAT 的完整搜索清单。"
                    "请重新扫描，并确认状态为“扫描完整”后再执行。"
                ),
                parent=self.root,
            )
            return
        selected = self._checked_hits(self.batch_hits)
        if not selected:
            messagebox.showinfo("没有命中", "没有已勾选的批量命中。")
            return
        resource_mode = self.current_resource_mode()
        risky_count = sum(
            getattr(hit, "write_mode", "") == "repack-risk"
            for hit in selected
        )
        confirmation = (
            f"将修改 {len(selected)} 个命中。修改写入 PAC 工作区，"
            "不会立即覆盖源 PAC。是否继续？"
            if resource_mode == "pac"
            else f"将修改 {len(selected)} 个命中并直接写回文件，同时创建备份。是否继续？"
        )
        if risky_count:
            confirmation += (
                f"\n\n其中 {risky_count} 次操作需要启发式回退写入。"
                "程序会先暂存、回读并在失败时停止写入，但无法证明未知字段或脚本语义完全不变。"
            )
        if not messagebox.askyesno(
            "执行批量替换",
            confirmation,
            parent=self.root,
        ):
            return
        if not self._confirm_pending_changes() or not self._ensure_idle():
            return
        pairs = self.batch_mapping.get_pairs()
        behavior_before = load_batch_behavior(
            serialize_batch_behavior(self.batch_hits)
        )
        affected_files = {hit.file for hit in selected}
        targets = [
            target
            for target in self.batch_targets
            if str(target.path) in affected_files
        ]
        rescan_targets = self._batch_business_targets()
        batch_globs = self.batch_globs_var.get()
        batch_options = self.batch_scan_options or self._resolve_options(Path("batch.tbl"))
        batch_options.schema_hint = ""

        def worker():
            ok, fail, logs = self.business.execute_mixed_batch(
                selected,
                pairs,
                do_backup=resource_mode == "unpacked",
                options=batch_options,
            )
            if resource_mode == "pac":
                self.workbench.refresh_materialized(targets)
            scan = self.business.scan_mixed_batch_targets(
                rescan_targets,
                batch_globs,
                pairs,
                use_equal_fast_path=False,
                options=batch_options,
                ordered=True,
            )
            apply_batch_behavior(scan.hits, behavior_before)
            return ok, fail, logs, scan

        def completed(result) -> None:
            ok, fail, logs, scan = result
            self._append(self.batch_log, logs)
            if ok:
                suffix = (
                    "成功结果已保存到 PAC 工作区；无需再执行单独保存。"
                    if resource_mode == "pac"
                    else "成功结果已写回；请复核结果与备份。"
                )
            else:
                suffix = "本次没有写入任何文件。"
            self._append(
                self.batch_log,
                [f"完成：成功 {ok}，失败/跳过 {fail}。{suffix}"],
            )
            if resource_mode == "pac" and ok:
                self._append(
                    self.batch_log,
                    [
                        "构建步骤：在左侧 PAC 树选择对应 PAC 或其任意子节点，"
                        "直接点击“构建所选 PAC”并选择新文件名。"
                    ],
                )
            if scan.errors:
                self._append(self.batch_log, scan.errors)
            self.batch_behavior_operations = behavior_before
            self.batch_hits = scan.hits
            self.batch_scan_complete = scan.complete
            self._refresh_hit_tree(self.batch_hit_tree, self.batch_hits)
            if resource_mode == "pac":
                self.refresh_pac_tree()
                self.refresh_pac_cache()
            else:
                self.refresh_unpacked_tree()
            self._reload_current_after_external_change(affected_files)
            message = (
                f"批量替换完成：成功 {ok}，失败/跳过 {fail}；"
                f"复核扫描剩余 {len(scan.hits)} 次操作。"
            )
            self.batch_progress_text_var.set(message)
            self.status_var.set(message)
            if not scan.complete:
                self._append(
                    self.batch_log,
                    [
                        "[INCOMPLETE] 写入后的自动复核未覆盖全部目标；"
                        "后续批量执行已锁定，请查看解析错误并重新扫描。"
                    ],
                )
            if resource_mode == "pac":
                detail = (
                    f"成功 {ok}，失败/跳过 {fail}。\n\n"
                    + (
                        "成功项已经保存到受管 PAC 工作区，不需要再点其他保存按钮。\n"
                        "接下来在左侧选择对应 PAC，然后点击“构建所选 PAC”另存为新 PAC。\n\n"
                        if ok
                        else "本次没有写入 PAC 工作区；请查看只读原因或失败日志。\n\n"
                    )
                    + f"自动复核后仍有 {len(scan.hits)} 次操作；失败详情请查看执行日志。"
                )
                dialog = messagebox.showwarning if fail or not ok else messagebox.showinfo
                dialog("批量替换完成", detail, parent=self.root)

        label = (
            "正在写入 PAC 工作区…"
            if resource_mode == "pac"
            else "正在写入解包文件…"
        )
        self._run_background(
            label,
            worker,
            completed,
            progressbar=self.batch_progress,
            progress_text_var=self.batch_progress_text_var,
        )

    def scan_diff(self) -> None:
        old_inputs = self.diff_old_sources.get_paths()
        new_inputs = self.diff_new_sources.get_paths()
        if not old_inputs or not new_inputs:
            messagebox.showinfo("对比源为空", "请分别添加旧版本与新版本来源。")
            return
        if not self._ensure_idle():
            return
        resource_mode = self.current_resource_mode()
        diff_globs = self.diff_globs_var.get()
        patterns = self._split_globs(diff_globs)
        if resource_mode == "pac":
            self.pac_compare.close()
            self.pac_compare = PacComparisonSession()

        def worker():
            if resource_mode == "pac":
                rows = self.pac_compare.compare(old_inputs, new_inputs)
                return [
                    row
                    for row in rows
                    if self._logical_matches(
                        (
                            row.old_entry.name
                            if row.old_entry is not None
                            else row.new_entry.name
                            if row.new_entry is not None
                            else row.rel
                        ),
                        patterns,
                    )
                ]
            old_targets = self.business.collect_file_targets(
                old_inputs,
                diff_globs,
            )
            new_targets = self.business.collect_file_targets(
                new_inputs,
                diff_globs,
            )
            if (
                len(old_inputs) == len(new_inputs) == 1
                and Path(old_inputs[0]).is_file()
                and Path(new_inputs[0]).is_file()
                and len(old_targets) == len(new_targets) == 1
            ):
                logical = (
                    f"{Path(old_inputs[0]).name} ↔ {Path(new_inputs[0]).name}"
                )
                old_targets = [
                    BusinessFileTarget(
                        old_targets[0].path,
                        logical,
                        old_targets[0].source_id,
                    )
                ]
                new_targets = [
                    BusinessFileTarget(
                        new_targets[0].path,
                        logical,
                        new_targets[0].source_id,
                    )
                ]
            return self.business.build_target_diff_index(old_targets, new_targets)

        def completed(rows) -> None:
            self._apply_diff_files(rows)
            message = (
                f"版本对比完成：共 {len(rows)} 个文件条目，"
                f"当前显示 {len(self.diff_files)} 个。"
            )
            self.diff_progress_text_var.set(message)
            self.status_var.set(message)

        self._run_background(
            "正在构建版本差异索引…",
            worker,
            completed,
            progressbar=self.diff_progress,
            progress_text_var=self.diff_progress_text_var,
        )

    def _apply_diff_files(self, rows) -> None:
        self.diff_files_all = list(rows)
        self.refresh_diff_files()

    def refresh_diff_files(self) -> None:
        show_same = self.diff_show_same_files_var.get()
        self.diff_files = [
            row
            for row in self.diff_files_all
            if show_same or row.status != "same"
        ]
        self.diff_file_tree.delete(*self.diff_file_tree.get_children())
        for index, row in enumerate(self.diff_files):
            self.diff_file_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    self._status_label(row.status),
                    row.rel,
                    row.old_size,
                    row.new_size,
                ),
                tags=(row.status,),
            )
        self.diff_entries_all = []
        self.refresh_diff_entries()
        self._reset_diff_detail()

    def on_diff_select(self, _event=None) -> None:
        if not self._ensure_idle():
            return
        selection = self.diff_file_tree.selection()
        if not selection:
            return
        row = self.diff_files[int(selection[0])]
        self.diff_entries_all = []
        self.diff_entries_visible = []
        self.diff_entry_tree.delete(*self.diff_entry_tree.get_children())
        self._diff_text_cells.reset()
        diff_options = self._resolve_options(Path(row.rel))

        def worker():
            if isinstance(row, PacDiffFileRow):
                logical_path = (
                    row.old_entry.name
                    if row.old_entry is not None
                    else row.new_entry.name
                    if row.new_entry is not None
                    else row.rel
                )
                if not is_text_document_path(logical_path):
                    return None
                old_path, new_path, logical_path = self.pac_compare.materialize_row(row)
            else:
                old_path = row.old_path
                new_path = row.new_path
                logical_path = row.rel
            return self.business.compute_entry_diff_auto(
                old_path,
                new_path,
                logical_path=logical_path,
                options=diff_options,
            )

        def completed(entries) -> None:
            if entries is None:
                self.diff_entry_tree.insert(
                    "",
                    "end",
                    values=("", "文件级", "该 PAC 条目不是 TBL/DAT。", ""),
                )
                message = "文件级差异已载入；该条目不是 TBL/DAT。"
            else:
                self.diff_entries_all = entries
                self.refresh_diff_entries()
                message = f"条目对比完成：解析 {len(entries)} 个文本条目。"
            self.diff_progress_text_var.set(message)
            self.status_var.set(message)

        self._run_background(
            "正在解析文件条目差异…",
            worker,
            completed,
            progressbar=self.diff_progress,
            progress_text_var=self.diff_progress_text_var,
        )

    def refresh_diff_entries(self) -> None:
        show_all = self.diff_show_all_var.get()
        self.diff_entries_visible = [
            row
            for row in self.diff_entries_all
            if show_all or row.status != "same"
        ]
        self.diff_entry_tree.delete(*self.diff_entry_tree.get_children())
        for index, row in enumerate(self.diff_entries_visible):
            old_index = "" if row.index_old is None else str(row.index_old)
            new_index = "" if row.index_new is None else str(row.index_new)
            label = old_index if old_index == new_index else f"{old_index} → {new_index}"
            self.diff_entry_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    label,
                    self._status_label(row.status),
                    self._preview(row.text_old),
                    self._preview(row.text_new),
                ),
                tags=(row.status,),
            )
        self._reset_diff_detail()
        if hasattr(self, "_diff_text_cells"):
            self._diff_text_cells.reset()

    def refresh_pac_cache(self) -> None:
        if not hasattr(self, "pac_cache_tree"):
            return
        self.pac_cache_tree.delete(*self.pac_cache_tree.get_children())
        for summary in self.pac_cache_manager.list_summaries():
            self.pac_cache_tree.insert(
                "",
                "end",
                iid=summary.workspace_id,
                values=(
                    str(summary.source_path),
                    self._pac_state_label(summary.state),
                    summary.entry_count,
                    summary.dirty_count,
                    summary.materialized_count,
                    self._format_bytes(summary.size_bytes),
                ),
            )

    def clean_safe_pac_workspaces(self) -> None:
        if not self._ensure_idle():
            return
        try:
            removed = self.pac_cache_manager.clean_safe()
            self.refresh_pac_cache()
            self.status_var.set(f"已将 {len(removed)} 个安全工作区移入程序回收目录。")
        except Exception as exc:
            messagebox.showerror("清理失败", str(exc), parent=self.root)

    def clear_pac_cache_by_source(self) -> None:
        """Clear every cache generation without requiring the PAC to load."""

        if not self._ensure_idle():
            return
        paths = filedialog.askopenfilenames(
            title="选择要清除全部历史缓存的源 PAC",
            filetypes=[("PAC 文件", "*.pac"), ("所有文件", "*.*")],
        )
        if not paths:
            return

        summaries_by_id = {}
        sources_by_id: dict[str, Path] = {}
        for raw_path in paths:
            source = Path(raw_path).resolve()
            for summary in self.pac_cache_manager.summaries_for_source(source):
                summaries_by_id[summary.workspace_id] = summary
                sources_by_id[summary.workspace_id] = source
        if not summaries_by_id:
            messagebox.showinfo(
                "没有缓存",
                "所选源 PAC 没有历史工作区缓存，无需清理。",
                parent=self.root,
            )
            return

        dirty_count = sum(summary.dirty_count for summary in summaries_by_id.values())
        dirty_warning = (
            f"\n\n其中共有 {dirty_count} 个相对源包有变化的条目（可能已另存导出）。"
            if dirty_count
            else ""
        )
        if not messagebox.askyesno(
            "按源 PAC 清除全部缓存",
            (
                f"将清除 {len(summaries_by_id)} 个历史工作区，覆盖所选源 PAC 的全部缓存代次。"
                f"{dirty_warning}\n\n工作区会移入程序回收目录，不会直接永久删除。是否继续？"
            ),
            parent=self.root,
        ):
            return

        workspace_ids = list(summaries_by_id)

        def worker():
            removed: list[str] = []
            errors: list[str] = []
            for workspace_id in workspace_ids:
                try:
                    self.pac_cache_manager.delete(workspace_id, allow_dirty=True)
                    removed.append(workspace_id)
                except Exception as exc:
                    source = sources_by_id.get(workspace_id, Path())
                    errors.append(f"{source.name or workspace_id} / {workspace_id}：{exc}")
            return removed, errors

        def completed(result) -> None:
            removed, errors = result
            self.batch_hits = []
            self.batch_scan_options = None
            self.batch_scan_complete = False
            self._refresh_hit_tree(self.batch_hit_tree, self.batch_hits)
            self.refresh_pac_tree()
            self.refresh_pac_cache()
            self._set_features_active(bool(self.workbench.projects()))
            self._refresh_pac_dependent_preview()
            self.status_var.set(
                f"已按源 PAC 将 {len(removed)} 个历史工作区移入程序回收目录。"
            )
            if errors:
                messagebox.showwarning(
                    "部分缓存未能清除",
                    "\n".join(errors),
                    parent=self.root,
                )

        self._run_background("正在按源 PAC 清除全部历史缓存…", worker, completed)

    @staticmethod
    def _integrity_result_line(result) -> str:
        status = {
            "clean": "CLEAN",
            "clean-uncompared": "UNCOMPARED",
            "damaged": "DAMAGED",
            "repairable": "REPAIRABLE",
            "uncertain": "UNCERTAIN",
            "incompatible": "INCOMPATIBLE",
            "not-scp": "NOT-SCP",
        }.get(result.status, result.status.upper())
        silent = (
            "未知"
            if result.silent_pointer_count is None
            else str(result.silent_pointer_count)
        )
        return (
            f"[{status}] {result.logical_path}：文本 {result.text_count}，"
            f"显式无效 {result.invalid_pointer_count}，"
            f"需纠正 {result.corrected_pointer_count}，静默错链 {silent}，"
            f"保守跳过 {result.skipped_uncertain_pointer_count}，"
            f"激进改写 {result.aggressive_pointer_count}。"
            f"{result.message}"
        )

    def current_integrity_strategy(self) -> str:
        return self.integrity_strategy_label_to_value[
            self.integrity_strategy_var.get()
        ]

    def on_integrity_strategy_changed(self, _event=None) -> None:
        self.integrity_last_pac_scan_report = None
        strategy = self.current_integrity_strategy()
        self.integrity_status_var.set(
            "已选择激进策略；必须重新对照分析，修复结果须另存并游戏验证。"
            if strategy == "aggressive"
            else "已选择保守策略；必须重新对照分析。"
        )

    def _format_pac_integrity_report(self, report) -> list[str]:
        silent = (
            "未知"
            if report.silent_pointer_count is None
            else str(report.silent_pointer_count)
        )
        lines = [
            f"目标 PAC：{report.source_path}",
            f"参考 PAC：{report.reference_path or '未提供'}",
            (
                f"扫描 {'完整' if report.complete else '存在不兼容项'}："
                f"请求 {report.requested_dat_count}，解析 {report.parsed_dat_count}，"
                f"物理文本 {report.text_count}，显式无效 {report.invalid_pointer_count}，"
                f"需纠正 {report.corrected_pointer_count}，静默错链 {silent}，"
                f"保守跳过 {report.skipped_uncertain_pointer_count}，"
                f"激进改写 {report.aggressive_pointer_count}，"
                f"策略 {report.strategy}。"
            ),
        ]
        priority = {
            "incompatible": 0,
            "damaged": 1,
            "repairable": 2,
            "uncertain": 3,
        }
        problems = sorted(
            (item for item in report.items if item.status in priority),
            key=lambda item: (priority[item.status], item.logical_path.casefold()),
        )
        clean = sorted(
            (
                item
                for item in report.items
                if item.status in {"clean", "clean-uncompared"}
            ),
            key=lambda item: item.logical_path.casefold(),
        )
        not_scp = sorted(
            (item for item in report.items if item.status == "not-scp"),
            key=lambda item: item.logical_path.casefold(),
        )
        lines.extend(["", "=== 错误 / 需处理 ==="])
        if problems:
            lines.extend(self._integrity_result_line(item) for item in problems)
        else:
            lines.append("（无）")
        lines.extend(["", "=== CLEAN ==="])
        if clean:
            lines.extend(self._integrity_result_line(item) for item in clean)
        else:
            lines.append("（无）")
        if not_scp:
            lines.extend(["", "=== 不适用 #scp 指针扫描 ==="])
            lines.extend(self._integrity_result_line(item) for item in not_scp)
        return lines

    def _show_pac_integrity_report(self, report) -> None:
        lines = self._format_pac_integrity_report(report)
        self._set_text(self.integrity_log, "\n".join(lines), readonly=True)
        self.integrity_status_var.set(lines[2])
        self.status_var.set(lines[2])

    def _load_integrity_pac(self, *, reference: bool) -> None:
        if not self._ensure_idle():
            return
        role = "参考" if reference else "目标"
        selected = filedialog.askopenfilename(
            title=f"加载{role}脚本 PAC",
            filetypes=[("PAC 文件", "*.pac"), ("所有文件", "*.*")],
        )
        if not selected:
            return

        def worker():
            return PacDatReferenceRepairService().archive_service.inspect(selected)

        def completed(archive) -> None:
            path = archive.source_path.resolve()
            if reference:
                self.integrity_reference_pac = path
                self.integrity_reference_pac_var.set(
                    f"参考 PAC：{path}（{len(archive.entries)} 个条目）"
                )
            else:
                self.integrity_target_pac = path
                self.integrity_target_pac_var.set(
                    f"目标 PAC：{path}（{len(archive.entries)} 个条目）"
                )
            self.integrity_last_pac_scan_report = None
            self.integrity_status_var.set(f"已加载{role} PAC；对照结果已清除。")
            self.status_var.set(self.integrity_status_var.get())

        self._run_background(
            f"正在验证并加载{role} PAC…",
            worker,
            completed,
            progressbar=self.integrity_progress,
            progress_text_var=self.integrity_status_var,
        )

    def load_integrity_target_pac(self) -> None:
        self._load_integrity_pac(reference=False)

    def load_integrity_reference_pac(self) -> None:
        self._load_integrity_pac(reference=True)

    def analyze_loaded_integrity_pacs(self) -> None:
        if not self._ensure_idle():
            return
        target = self.integrity_target_pac
        reference = self.integrity_reference_pac
        if target is None or reference is None:
            messagebox.showwarning(
                "尚未加载 PAC",
                "请先分别执行“加载目标 PAC”和“加载参考 PAC”。",
                parent=self.root,
            )
            return
        strategy = self.current_integrity_strategy()

        def worker():
            return PacDatReferenceRepairService().scan(
                target,
                reference,
                strategy=strategy,
                progress=self._post_background_progress,
            )

        def completed(report) -> None:
            self.integrity_last_pac_scan_report = report
            self._show_pac_integrity_report(report)

        self._run_background(
            "正在逐条对照 PAC 内 DAT 指针…",
            worker,
            completed,
            progressbar=self.integrity_progress,
            progress_text_var=self.integrity_status_var,
        )

    def _show_single_integrity_result(
        self,
        result,
        *,
        source: str | Path,
        reference: str | Path | None,
    ) -> None:
        lines = [
            f"目标：{Path(source).resolve()}",
            f"参考：{Path(reference).resolve() if reference else '未提供'}",
            "",
            self._integrity_result_line(result),
        ]
        self._set_text(self.integrity_log, "\n".join(lines), readonly=True)
        self.integrity_status_var.set(
            f"单文件扫描完成：显式无效 {result.invalid_pointer_count}，"
            f"需纠正 {result.corrected_pointer_count}。"
        )
        self.status_var.set(self.integrity_status_var.get())

    def scan_single_dat_integrity(self, *, compare: bool) -> None:
        if not self._ensure_idle():
            return
        damaged = filedialog.askopenfilename(
            title="选择待扫描 #scp DAT",
            filetypes=[("DAT 文件", "*.dat"), ("所有文件", "*.*")],
        )
        if not damaged:
            return
        reference = None
        if compare:
            reference = filedialog.askopenfilename(
                title="选择可信参考 DAT（同版本、同脚本基础）",
                filetypes=[("DAT 文件", "*.dat"), ("所有文件", "*.*")],
            )
            if not reference:
                return
        strategy = self.current_integrity_strategy()

        def worker():
            return DatReferenceRepairService().scan(
                damaged,
                reference,
                strategy=strategy,
            )

        def completed(result) -> None:
            self._show_single_integrity_result(
                result,
                source=damaged,
                reference=reference,
            )

        self._run_background(
            "正在扫描单文件 DAT 指针…",
            worker,
            completed,
            progressbar=self.integrity_progress,
            progress_text_var=self.integrity_status_var,
        )

    def repair_single_dat_references(self) -> None:
        if not self._ensure_idle():
            return
        reference = filedialog.askopenfilename(
            title="选择可信参考 DAT（同版本、同脚本基础）",
            filetypes=[("DAT 文件", "*.dat"), ("所有文件", "*.*")],
        )
        if not reference:
            return
        damaged = filedialog.askopenfilename(
            title="选择待修复 DAT",
            filetypes=[("DAT 文件", "*.dat"), ("所有文件", "*.*")],
        )
        if not damaged:
            return
        damaged_path = Path(damaged).resolve()
        output = filedialog.asksaveasfilename(
            title="另存修复后的 DAT",
            initialdir=str(damaged_path.parent),
            initialfile=f"{damaged_path.stem}.pointer-fixed.dat",
            defaultextension=".dat",
            filetypes=[("DAT 文件", "*.dat"), ("所有文件", "*.*")],
        )
        if not output:
            return
        strategy = self.current_integrity_strategy()
        if not messagebox.askyesno(
            "确认单文件 DAT 指针修复",
            (
                "程序会先完整对照并另存新 DAT；待修复文件的当前文本和 CLE 封装"
                "保持不变，输入文件不会被覆盖。"
                + (
                    "\n\n当前为激进策略：证据不足但能够对齐的有效指针也会被改写，"
                    "可能撤销补丁主动修改的正确关系。"
                    if strategy == "aggressive"
                    else ""
                )
                + "\n\n是否继续？"
            ),
            parent=self.root,
        ):
            return

        def worker():
            return DatReferenceRepairService().repair(
                reference,
                damaged,
                output,
                do_backup=False,
                strategy=strategy,
            )

        def completed(report) -> None:
            self._show_single_integrity_result(
                report.scan,
                source=report.source_path,
                reference=report.reference_path,
            )
            self._append(
                self.integrity_log,
                ["", f"[OUTPUT] {report.output_path}", "输出已通过严格指针解析。"],
            )
            messagebox.showinfo(
                "单文件 DAT 修复完成",
                (
                    f"输出：{report.output_path}\n"
                    f"修正指针：{report.scan.corrected_pointer_count}\n"
                    f"其中静默错链：{report.scan.silent_pointer_count or 0}"
                ),
                parent=self.root,
            )

        self._run_background(
            "正在对齐并修复单文件 DAT…",
            worker,
            completed,
            progressbar=self.integrity_progress,
            progress_text_var=self.integrity_status_var,
        )

    def scan_pac_dat_integrity(self, *, compare: bool) -> None:
        if not self._ensure_idle():
            return
        damaged = filedialog.askopenfilename(
            title="选择待扫描脚本 PAC",
            filetypes=[("PAC 文件", "*.pac"), ("所有文件", "*.*")],
        )
        if not damaged:
            return
        reference = None
        if compare:
            reference = filedialog.askopenfilename(
                title="选择可信参考 PAC（同版本、同脚本基础）",
                filetypes=[("PAC 文件", "*.pac"), ("所有文件", "*.*")],
            )
            if not reference:
                return
        strategy = self.current_integrity_strategy()

        def worker():
            return PacDatReferenceRepairService().scan(
                damaged,
                reference,
                strategy=strategy,
                progress=self._post_background_progress,
            )

        def completed(report) -> None:
            self._show_pac_integrity_report(report)

        self._run_background(
            "正在逐条扫描 PAC 内 DAT 指针…",
            worker,
            completed,
            progressbar=self.integrity_progress,
            progress_text_var=self.integrity_status_var,
        )

    def repair_loaded_integrity_pacs(self) -> None:
        if not self._ensure_idle():
            return
        target = self.integrity_target_pac
        reference = self.integrity_reference_pac
        if target is None or reference is None:
            messagebox.showwarning(
                "尚未加载 PAC",
                "请先分别执行“加载目标 PAC”和“加载参考 PAC”。",
                parent=self.root,
            )
            return
        self._start_pac_dat_repair(reference, target)

    def _start_pac_dat_repair(
        self,
        reference: str | Path,
        damaged: str | Path,
    ) -> None:
        damaged_path = Path(damaged).resolve()
        output = filedialog.asksaveasfilename(
            title="另存修复后的 PAC",
            initialdir=str(damaged_path.parent),
            initialfile=f"{damaged_path.stem}.pointer-fixed.pac",
            defaultextension=".pac",
            filetypes=[("PAC 文件", "*.pac"), ("所有文件", "*.*")],
        )
        if not output:
            return
        strategy = self.current_integrity_strategy()
        aggressive_warning = (
            "\n\n当前为激进策略：缺少邻近损坏证据、但能够与参考包对齐的有效指针"
            "也会被改写。这可能纠正更多静默错链，也可能撤销补丁主动修改的正确关系。"
            if strategy == "aggressive"
            else ""
        )
        if not messagebox.askyesno(
            "确认尝试修复 DAT 指针",
            (
                "程序会逐条对照全部 #scp DAT，只改写能够证明的字符串指针，"
                "保留目标 PAC 的当前文本和 CLE 封装。\n\n"
                "单个 DAT 无法安全对齐时会保留原条目并继续处理其余 DAT；"
                "因此存在失败项的输出 PAC 只是“部分修复”，报告会在最上方列出失败项。"
                "输出始终另存，不覆盖输入。"
                + aggressive_warning
                + "\n\n是否继续？"
            ),
            parent=self.root,
        ):
            return

        def worker():
            return PacDatReferenceRepairService().repair(
                reference,
                damaged,
                output,
                strategy=strategy,
                progress=self._post_background_progress,
            )

        def completed(report) -> None:
            self.refresh_pac_cache()
            summary = (
                f"检查 DAT {report.examined_dat_count}，修复 DAT "
                f"{report.repaired_dat_count}，修正指针 {report.corrected_pointer_count}，"
                f"修复前显式无效 {report.invalid_pointer_count_before}，"
                f"保守跳过 {report.skipped_uncertain_pointer_count}，"
                f"激进改写 {report.aggressive_pointer_count}，"
                f"失败条目 {len(report.failed_entries)}。"
            )
            lines = [
                f"目标 PAC：{report.source_path}",
                f"参考 PAC：{report.reference_path}",
                f"输出 PAC：{report.output_path}",
                summary,
                "",
                "=== 错误 / 未修复 ===",
            ]
            if report.failed_entries:
                lines.extend(
                    f"[FAILED] {name}：{message}"
                    for name, message in report.failed_entries
                )
            else:
                lines.append("（无）")
            lines.extend(["", "=== 已修复 ==="])
            if report.repaired_entries:
                lines.extend(
                    f"[REPAIRED] {name}" for name in report.repaired_entries
                )
            else:
                lines.append("（无）")
            self._set_text(self.integrity_log, "\n".join(lines), readonly=True)
            self.integrity_status_var.set(summary)
            dialog_text = (
                f"输出：{report.output_path}\n"
                f"检查 DAT：{report.examined_dat_count}\n"
                f"修复 DAT：{report.repaired_dat_count}\n"
                f"修正指针：{report.corrected_pointer_count}\n"
                f"失败条目：{len(report.failed_entries)}"
            )
            if report.failed_entries:
                dialog_text += (
                    "\n\n其余条目已经尽量修复；失败条目保留了目标 PAC 中的原始内容。"
                    "该输出不能视为完全修复，请查看报告顶部。"
                )
                messagebox.showwarning(
                    "DAT 指针部分修复完成",
                    dialog_text,
                    parent=self.root,
                )
            else:
                dialog_text += (
                    "\n\n所有适用条目均已处理，输出 PAC 已通过容器构建校验。"
                )
                messagebox.showinfo(
                    "DAT 指针修复完成",
                    dialog_text,
                    parent=self.root,
                )
            self.status_var.set(summary)

        self._run_background(
            "正在逐条对齐并尽量修复全部 DAT 指针…",
            worker,
            completed,
            progressbar=self.integrity_progress,
            progress_text_var=self.integrity_status_var,
        )

    def repair_pac_dat_references(self) -> None:
        """Rebuild damaged and silently stale DAT pointers from a trusted PAC."""

        if not self._ensure_idle():
            return
        reference = filedialog.askopenfilename(
            title="选择可信参考 PAC（修改前、同版本、同脚本基础）",
            filetypes=[("PAC 文件", "*.pac"), ("所有文件", "*.*")],
        )
        if not reference:
            return
        damaged = filedialog.askopenfilename(
            title="选择包含错误 DAT 指针的待修复 PAC",
            filetypes=[("PAC 文件", "*.pac"), ("所有文件", "*.*")],
        )
        if not damaged:
            return
        self._start_pac_dat_repair(reference, damaged)

    def delete_selected_pac_workspace(self) -> None:
        if not self._ensure_idle():
            return
        selected = self.pac_cache_tree.selection()
        if not selected:
            return
        summaries = {
            item.workspace_id: item
            for item in self.pac_cache_manager.list_summaries()
        }
        dirty = [summaries[item] for item in selected if item in summaries and summaries[item].dirty_count]
        warning = (
            "\n包含未回包修改，删除会放弃这些修改。"
            if dirty
            else "\n所选工作区会移入程序回收目录。"
        )
        if not messagebox.askyesno(
            "删除工作区",
            f"确定删除 {len(selected)} 个工作区吗？{warning}",
            parent=self.root,
        ):
            return
        try:
            for workspace_id in selected:
                self.pac_cache_manager.delete(workspace_id, allow_dirty=True)
            self.refresh_pac_cache()
            self.status_var.set(f"已移除 {len(selected)} 个 PAC 工作区。")
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc), parent=self.root)

    def refresh_runtime(self) -> None:
        if not hasattr(self, "runtime_list"):
            return
        self.runtime_list.delete(0, "end")
        for entry in list_runtime_entries():
            self.runtime_list.insert(
                "end",
                f"{entry.name}　{self._format_bytes(entry.size_bytes)}",
            )
        info = describe_runtime()
        self.runtime_meta.configure(
            text=(
                f"{info['entry_count']} 项，合计 {self._format_bytes(info['total_bytes'])}；"
                f"可安全清理临时项 {info['transient_count']} 个。"
            )
        )

    def clean_runtime(self) -> None:
        removed = cleanup_runtime(remove_all=False)
        self.refresh_runtime()
        self.status_var.set(f"已清理 {len(removed)} 个临时运行缓存。")

    def open_pac_data_root(self) -> None:
        try:
            os.startfile(self.pac_cache_manager.data_root)
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.root)

    def _toggle_pac_fallback(self) -> None:
        if self.pac_fallback_var.get():
            self.fallback_frame.pack(fill="x", pady=(6, 0))
        else:
            self.fallback_frame.pack_forget()

    def fallback_extract_pac(self) -> None:
        source = filedialog.askopenfilename(
            title="选择要回退解包的 PAC",
            filetypes=[("PAC 文件", "*.pac"), ("所有文件", "*.*")],
        )
        if not source:
            return
        target = filedialog.askdirectory(title="选择空的输出目录")
        if not target or not self._ensure_idle():
            return
        self._run_background(
            "正在使用回退工具解包…",
            lambda: self.pac_fallback.extract(source, target),
            lambda output: messagebox.showinfo(
                "回退解包完成",
                f"输出：{output}",
                parent=self.root,
            ),
        )

    def fallback_build_pac(self) -> None:
        source = filedialog.askdirectory(title="选择回退构建的文件夹")
        if not source:
            return
        target = filedialog.asksaveasfilename(
            title="回退构建 PAC",
            defaultextension=".pac",
            filetypes=[("PAC 文件", "*.pac"), ("所有文件", "*.*")],
        )
        if not target or not self._ensure_idle():
            return
        self._run_background(
            "正在使用回退工具构建…",
            lambda: self.pac_fallback.build(source, target),
            lambda output: messagebox.showinfo(
                "回退构建完成",
                f"输出：{output}",
                parent=self.root,
            ),
        )

    def _load_document_into_single_view(self, document) -> None:
        self._close_single_editor(save=False)
        self.single_tree.delete(*self.single_tree.get_children())
        self.single_row_indices.clear()
        self.search_results = []
        self.search_cursor = -1
        self.search_signature = None
        self._single_match = None
        for row, unit in enumerate(document.units):
            item = self.single_tree.insert(
                "",
                "end",
                values=(
                    unit.index,
                    unit.location,
                    unit.original_text,
                    unit.current_text,
                ),
                tags=("changed",) if unit.changed else (),
            )
            self.single_row_indices[item] = unit.index
        self.single_tree.tag_configure("changed", background="#fff0c9")
        self.current_index = None

    def _refresh_single_document(self) -> None:
        document = self.session.document
        if document is None:
            return
        selected_index = self.current_index
        self._load_document_into_single_view(document)
        if selected_index is not None:
            item = next(
                (
                    iid
                    for iid, index in self.single_row_indices.items()
                    if index == selected_index
                ),
                None,
            )
            if item:
                self.single_tree.selection_set(item)
                self.single_tree.focus(item)
                self.single_tree.see(item)

    def _commit_current_edit(self) -> None:
        self._close_single_editor(save=True)

    def _clear_single_document_view(self) -> None:
        self.session = DocumentSession()
        self.current_origin = "none"
        self.current_project_id = None
        self.current_pac_entry = None
        self.current_file = None
        self.current_index = None
        self.search_results = []
        self.search_cursor = -1
        self.search_signature = None
        if hasattr(self, "single_tree"):
            self._close_single_editor(save=False)
            self.single_tree.delete(*self.single_tree.get_children())
            self.single_row_indices.clear()
        self.single_meta_var.set("请从左侧文件树双击 .tbl 或 .dat 文件。")
        self.route_hint_var.set(self._route_message())

    def _confirm_pending_changes(self) -> bool:
        if self.session.document is None:
            return True
        self._commit_current_edit()
        if not self.session.changed_count():
            return True
        answer = messagebox.askyesnocancel(
            "尚未保存",
            (
                f"{self.current_pac_entry or self.current_file or '当前文件'} 有未保存修改。\n"
                "“是”保存修改，“否”重新加载并放弃本次内存修改。"
            ),
            parent=self.root,
        )
        if answer is None:
            return False
        if answer:
            try:
                if self.current_origin == "pac":
                    project = self.workbench.get(self.current_project_id or "")
                    project.save_current()
                    self.session = project.document_session
                    self.refresh_pac_tree()
                else:
                    self.session.save()
            except Exception as exc:
                messagebox.showerror("保存失败", str(exc), parent=self.root)
                return False
        else:
            try:
                if self.current_origin == "pac":
                    project = self.workbench.get(self.current_project_id or "")
                    document = project.open_entry(
                        self.current_pac_entry or "",
                        options=self._resolve_options(
                            Path(self.current_pac_entry or ""),
                            for_pac=True,
                        ),
                    )
                    self.session = project.document_session
                    self.current_file = project.workspace.entry_path(
                        self.current_pac_entry or ""
                    )
                else:
                    path = Path(self.current_file or "")
                    document = self.session.open_document(
                        path,
                        options=self._resolve_options(path, for_pac=False),
                    )
                self._load_document_into_single_view(document)
            except Exception as exc:
                messagebox.showerror("放弃修改失败", str(exc), parent=self.root)
                return False
        return True

    def _reload_current_after_external_change(self, affected_files: set[str]) -> None:
        if (
            self.current_origin == "unpacked"
            and self.current_file is not None
            and str(self.current_file) in affected_files
        ):
            try:
                document = self.session.open_document(
                    self.current_file,
                    options=self._resolve_options(self.current_file, for_pac=False),
                )
                self._load_document_into_single_view(document)
            except Exception as exc:
                messagebox.showwarning(
                    "当前编辑器刷新失败",
                    f"批量修改已写入文件，但当前编辑器未能重新载入：{exc}",
                    parent=self.root,
                )
            return
        if (
            not self.current_project_id
            or not self.current_pac_entry
            or not self.current_file
            or str(self.current_file) not in affected_files
        ):
            return
        try:
            project = self.workbench.get(self.current_project_id)
            document = project.open_entry(
                self.current_pac_entry,
                options=self._resolve_options(Path(self.current_pac_entry)),
            )
            self.session = project.document_session
            self.current_file = project.workspace.entry_path(self.current_pac_entry)
            self._load_document_into_single_view(document)
        except Exception as exc:
            messagebox.showwarning(
                "当前编辑器刷新失败",
                f"批量修改已写入工作区，但当前编辑器未能重新载入：{exc}",
                parent=self.root,
            )

    def _refresh_hit_tree(self, tree: ttk.Treeview, source) -> None:
        is_batch_tree = tree is getattr(self, "batch_hit_tree", None)
        if is_batch_tree:
            self._clear_batch_rich_cells()
        tree.delete(*tree.get_children())
        for index, hit in enumerate(source):
            writable = bool(getattr(hit, "writable", True))
            checked = bool(writable and getattr(hit, "checked", True))
            hit.checked = checked
            item = tree.insert(
                "",
                "end",
                iid=str(index),
                text="",
                image=self.checkbox_images[checked],
                values=self._hit_values(hit),
            )
            tag = hit_row_presentation(hit).key if is_batch_tree else ("readonly" if not writable else ("checked" if checked else "unchecked"))
            tree.item(item, tags=(tag,))
        if is_batch_tree:
            for presentation in HIT_STYLES.values():
                tree.tag_configure(presentation.key, foreground=presentation.foreground, background=presentation.background)
        if is_batch_tree and source:
            self._schedule_batch_rich_cells()

    def _hit_values(self, hit) -> tuple[str, ...]:
        target = self.batch_target_by_file.get(str(hit.file))
        if isinstance(target, PacMaterializedEntry):
            logical = f"{target.pac_path.name} :: {target.entry_name}"
        elif isinstance(target, BusinessFileTarget):
            logical = target.logical_path
        else:
            logical = getattr(hit, "logical_file", "") or Path(hit.file).name
        if hasattr(hit, "unit_index"):
            location = hit.location
            old = self._batch_context_preview(
                hit.original_text,
                hit.match_start,
                hit.match_end,
            )
            new_end = hit.match_start + len(hit.pair_new)
            new = self._batch_context_preview(
                hit.new_text,
                hit.match_start,
                new_end,
            )
            write = hit_presentation(hit).label
        else:
            location = f"0x{hit.offset:08X}"
            old = getattr(hit, "_patch_old", "")
            new = hit.new_text
            write = "旧命中"
        return (
            logical,
            str(hit.kind),
            location,
            write,
            old,
            new,
        )

    @staticmethod
    def _batch_context_preview(
        text: str,
        start: int,
        end: int,
        *,
        radius: int = 42,
    ) -> str:
        return "".join(
            RetextTkApp._batch_context_parts(
                text,
                start,
                end,
                radius=radius,
            )
        )

    @staticmethod
    def _batch_context_parts(
        text: str,
        start: int,
        end: int,
        *,
        radius: int = 42,
    ) -> tuple[str, str, str]:
        """Return a one-line local context split around the actual match."""

        return excerpt_parts(text, start, end, radius=radius)

    def _set_batch_scrollbar(
        self,
        scrollbar: ttk.Scrollbar,
        first: str,
        last: str,
    ) -> None:
        scrollbar.set(first, last)
        self._schedule_batch_rich_cells()

    def _schedule_batch_rich_cells(self) -> None:
        tree = getattr(self, "batch_hit_tree", None)
        if tree is None or not tree.winfo_exists():
            return
        if self._batch_rich_after_id is not None:
            return
        self._batch_rich_after_id = tree.after(16, self._render_batch_rich_cells)

    def _clear_batch_rich_cells(self) -> None:
        tree = getattr(self, "batch_hit_tree", None)
        if self._batch_rich_after_id is not None and tree is not None:
            try:
                tree.after_cancel(self._batch_rich_after_id)
            except tk.TclError:
                pass
        self._batch_rich_after_id = None
        for widget in [*self._batch_cell_overlays.values(), *getattr(self, "_batch_cell_pool", [])]:
            try:
                widget.destroy()
            except tk.TclError:
                pass
        self._batch_cell_overlays.clear()
        self._batch_cell_pool = []
        self._batch_excerpt_cache = {}

    def _visible_batch_items(self) -> set[str]:
        tree = self.batch_hit_tree
        height = max(1, tree.winfo_height())
        try:
            rowheight = int(self.style.lookup("Treeview", "rowheight") or 24)
        except (TypeError, ValueError):
            rowheight = 24
        step = max(4, rowheight // 2)
        return {
            item
            for y in range(0, height + step, step)
            if (item := tree.identify_row(y))
        }

    def _render_batch_rich_cells(self) -> None:
        self._batch_rich_after_id = None
        tree = getattr(self, "batch_hit_tree", None)
        if tree is None or not tree.winfo_exists() or not tree.winfo_ismapped():
            for widget in self._batch_cell_overlays.values():
                widget.place_forget()
            return

        if not hasattr(self, "batch_match_font"):
            self.batch_match_font = tkfont.nametofont("TkDefaultFont").copy()
            self.batch_match_font.configure(weight="bold")

        visible_items = self._visible_batch_items()
        if not hasattr(self, "_batch_cell_pool"):
            self._batch_cell_pool = []
            self._batch_excerpt_cache = {}
        pool = self._batch_cell_pool
        for key in list(self._batch_cell_overlays):
            if key[0] not in visible_items:
                widget = self._batch_cell_overlays.pop(key)
                widget.place_forget()
                pool.append(widget)
        normal_font = tkfont.nametofont("TkDefaultFont")
        font_signature = tuple(sorted(normal_font.actual().items()))
        cache = self._batch_excerpt_cache
        visible_keys: set[tuple[str, str]] = set()
        selected = set(tree.selection())
        for item in visible_items:
            try:
                hit = self.batch_hits[int(item)]
            except (IndexError, TypeError, ValueError):
                continue
            if not hasattr(hit, "unit_index"):
                continue
            contexts = {
                "old": (
                    hit.original_text,
                    hit.match_start,
                    hit.match_end,
                ),
                "new": (
                    hit.new_text,
                    hit.match_start,
                    hit.match_start + len(hit.pair_new),
                ),
            }
            for column, context in contexts.items():
                bounds = tree.bbox(item, column)
                if not bounds or bounds[2] <= 0 or bounds[3] <= 0:
                    continue
                signature = (item, column, bounds[2], font_signature)
                parts = cache.get(signature)
                if parts is None:
                    parts = excerpt_parts(
                        *context, width=max(1, bounds[2] - 12),
                        measure=normal_font.measure, measure_match=self.batch_match_font.measure,
                    )
                    if len(cache) >= 1024:
                        cache.clear()
                    cache[signature] = parts
                key = (item, column)
                visible_keys.add(key)
                widget = self._batch_cell_overlays.get(key)
                if widget is None:
                    widget = pool.pop() if pool else self._new_batch_cell_overlay(tree)
                    self._batch_cell_overlays[key] = widget
                widget._batch_iid = item
                selected_row = item in selected
                presentation = hit_row_presentation(hit)
                background = "#dbeafe" if selected_row else presentation.background
                foreground = "#132d50" if selected_row else presentation.foreground
                if getattr(widget, "_content_signature", None) != signature:
                    widget.configure(state="normal")
                    widget.delete("1.0", "end")
                    widget.insert("end", parts[0])
                    widget.insert("end", parts[1], ("match",))
                    widget.insert("end", parts[2])
                    widget.configure(state="disabled")
                    widget._content_signature = signature
                colors = (background, foreground)
                if getattr(widget, "_color_signature", None) != colors:
                    widget.configure(background=background, foreground=foreground)
                    widget.tag_configure("match", font=self.batch_match_font, foreground=foreground)
                    widget._color_signature = colors
                x, y, width, height = bounds
                widget.place(x=x, y=y, width=width, height=height)

        for key in list(self._batch_cell_overlays):
            if key not in visible_keys:
                widget = self._batch_cell_overlays.pop(key)
                widget.place_forget()
                pool.append(widget)

    def _new_batch_cell_overlay(self, parent: ttk.Treeview) -> tk.Text:
        widget = tk.Text(
            parent,
            height=1,
            wrap="none",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=6,
            pady=2,
            cursor="arrow",
            takefocus=False,
            exportselection=False,
            font="TkDefaultFont",
        )
        widget.bind("<Button-1>", self._on_batch_overlay_click)
        widget.bind("<Double-1>", lambda event: self._open_batch_hit(self._batch_overlay_item(event)))
        widget.bind("<MouseWheel>", self._on_batch_overlay_mousewheel)
        widget.bind("<Shift-MouseWheel>", self._on_batch_overlay_shift_mousewheel)
        return widget

    def _on_batch_overlay_click(self, event) -> str:
        item = self._batch_overlay_item(event)
        if item and self.batch_hit_tree.exists(item):
            self.batch_hit_tree.selection_set(item)
            self.batch_hit_tree.focus(item)
            self._schedule_batch_rich_cells()
        return "break"

    def _batch_overlay_item(self, event):
        # The native tree can scroll before the next scheduled paint.
        return self.batch_hit_tree.identify_row(event.widget.winfo_y() + event.y)

    def _on_batch_overlay_mousewheel(self, event) -> str:
        units = -int(event.delta / 120) if event.delta else 0
        if units:
            self.batch_hit_tree.yview_scroll(units, "units")
        self._schedule_batch_rich_cells()
        return "break"

    def _on_batch_overlay_shift_mousewheel(self, event) -> str:
        units = -int(event.delta / 120) if event.delta else 0
        if units:
            self.batch_hit_tree.xview_scroll(units, "units")
        self._schedule_batch_rich_cells()
        return "break"

    def _on_batch_hit_double_click(self, event) -> str | None:
        item = self.batch_hit_tree.identify_row(event.y)
        if not item or self.batch_hit_tree.identify_column(event.x) in ("", "#0"):
            return None
        return self._open_batch_hit(item)

    def _open_batch_hit(self, item: str) -> str:
        if not self._ensure_idle():
            return "break"
        try:
            hit = self.batch_hits[int(item)]
        except (IndexError, TypeError, ValueError):
            return "break"
        target = self.batch_target_by_file.get(str(hit.file))
        return self._open_text_hit(hit, target=target)

    def _open_text_hit(self, hit, *, target=None, edit=True) -> str:
        opened = False
        if isinstance(target, PacMaterializedEntry):
            self._open_pac_entry(
                PacNodeRef(target.workspace_id, "file", target.entry_name)
            )
            opened = (
                self.current_project_id == target.workspace_id
                and self.current_pac_entry == target.entry_name
            )
        elif isinstance(target, BusinessFileTarget):
            self._open_unpacked_file(Path(target.path))
            opened = (
                self.current_origin == "unpacked"
                and self.current_file == Path(target.path).resolve()
            )
        elif self.current_resource_mode() == "pac" and hit.source_id:
            self._open_pac_entry(
                PacNodeRef(hit.source_id, "file", hit.logical_file)
            )
            opened = (
                self.current_project_id == hit.source_id
                and self.current_pac_entry == hit.logical_file
            )
        else:
            self._open_unpacked_file(Path(hit.file))
            opened = (
                self.current_origin == "unpacked"
                and self.current_file == Path(hit.file).resolve()
            )
        if opened:
            if self._select_single_unit(hit.unit_index):
                iid = self.single_tree.selection()[0]
                unit = self.session.document.get_unit(hit.unit_index)
                # A user may have edited the source since scanning. Do not
                # blindly select stale offsets or a different occurrence.
                span = resolve_hit_span(unit.current_text, hit.original_text, hit.match_start, hit.match_end)
                self._show_single_match(iid, span)
                if edit:
                    self._begin_single_edit(iid)
                if span is None:
                    self.status_var.set("已打开全文；内容已变化，无法唯一定位原命中，请重新搜索。")
        return "break"

    def _show_single_match(self, item: str, span: tuple[int, int] | None) -> None:
        unit = self.session.document.get_unit(self.single_row_indices[item])
        if span is None or not 0 <= span[0] <= span[1] <= len(unit.current_text):
            self._single_match = None
            self.single_tree.clear_contexts()
            return
        self._single_match = (item, *span, unit.current_text[span[0]:span[1]])
        self.single_tree.show_context(item, "current", span)

    def _select_single_unit(self, unit_index: int) -> bool:
        item = next(
            (
                iid
                for iid, index in self.single_row_indices.items()
                if index == unit_index
            ),
            None,
        )
        if item is None:
            self.status_var.set(f"已打开文件，但未找到文本索引 {unit_index}。")
            return False
        self.single_tree.selection_set(item)
        self.single_tree.focus(item)
        self.single_tree.see(item)
        self.current_index = unit_index
        self.status_var.set(f"已定位到文本行：{unit_index}。")
        return True

    def _toggle_hit_from_event(self, event, tree: ttk.Treeview, source) -> None:
        item = tree.identify_row(event.y)
        if not item or tree.identify_column(event.x) != "#0":
            return
        hit = source[int(item)]
        if not bool(getattr(hit, "writable", True)):
            note = str(getattr(hit, "write_note", "")).strip()
            self.status_var.set(
                (
                    "替换前后相同：未变更，不需要写回。"
                    if getattr(hit, "write_mode", "") == "search-only"
                    else "该操作在扫描预检中不可安全写回。"
                )
                + (f" {note}" if note else "")
            )
            return "break"
        hit.checked = not bool(getattr(hit, "checked", True))
        checked = bool(hit.checked)
        tree.item(
            item,
            image=self.checkbox_images[checked],
            tags=(hit_row_presentation(hit).key if tree is getattr(self, "batch_hit_tree", None) else ("checked" if checked else "unchecked"),),
        )
        if checked:
            tree.selection_set(item)
        else:
            tree.selection_remove(item)
        if tree is getattr(self, "batch_hit_tree", None):
            self._schedule_batch_rich_cells()
        return "break"

    def _set_hit_checks(
        self,
        tree: ttk.Treeview,
        source,
        checked: bool,
        *,
        selected_only: bool,
    ) -> None:
        indices = (
            [int(item) for item in tree.selection()]
            if selected_only
            else list(range(len(source)))
        )
        selected_items = set(tree.selection())
        deselect = []
        for index in indices:
            writable = bool(getattr(source[index], "writable", True))
            applied = bool(checked and writable)
            source[index].checked = applied
            item = str(index)
            if tree.exists(item):
                tag = (
                    "readonly"
                    if not writable
                    else ("checked" if applied else "unchecked")
                )
                if tree is getattr(self, "batch_hit_tree", None):
                    tag = hit_row_presentation(source[index]).key
                tree.item(
                    item,
                    image=self.checkbox_images[applied],
                    tags=(tag,),
                )
                if not applied and item in selected_items:
                    deselect.append(item)
        if deselect:
            tree.selection_remove(*deselect)
        if tree is getattr(self, "batch_hit_tree", None):
            self._schedule_batch_rich_cells()

    @staticmethod
    def _checked_hits(source):
        return [
            hit
            for hit in source
            if bool(getattr(hit, "writable", True))
            and bool(getattr(hit, "checked", True))
        ]

    def _configure_diff_tags(self) -> None:
        for tree in (
            getattr(self, "diff_file_tree", None),
            getattr(self, "diff_entry_tree", None),
        ):
            if tree is None:
                continue
            for status in (
                "same",
                "modified",
                "only_old",
                "only_new",
                "deleted",
                "added",
                "unknown",
            ):
                tree.tag_configure(status, font=self.diff_normal_font)

    def _reset_diff_detail(self) -> None:
        return

    @staticmethod
    def _status_label(status: str) -> str:
        return {
            "same": "相同",
            "modified": "已修改",
            "only_old": "仅旧版",
            "only_new": "仅新版",
            "deleted": "已删除",
            "added": "已新增",
            "unknown": "未知",
        }.get(status, status)

    @staticmethod
    def _pac_state_label(state: str) -> str:
        return {
            "clean": "干净",
            "dirty": "有修改",
            "exported": "已构建",
            "stale": "源已变化",
            "missing-source": "源丢失",
            "locked": "使用中",
            "damaged": "损坏",
        }.get(state, state)

    @staticmethod
    def _pac_entry_state_label(state: str) -> str:
        return {
            "added": "新增",
            "source": "源 PAC",
            "cached": "已缓存",
            "modified": "已修改",
        }.get(state, state)

    @staticmethod
    def _format_bytes(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} B"

    @staticmethod
    def _format_media_duration(seconds: float) -> str:
        total = max(0, round(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, whole_seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{whole_seconds:02d}"
        return f"{minutes}:{whole_seconds:02d}"

    @staticmethod
    def _format_model_animation_time(seconds: float) -> str:
        total = max(float(seconds), 0.0)
        minutes, remainder = divmod(total, 60.0)
        if minutes >= 60:
            hours, minutes = divmod(int(minutes), 60)
            return f"{hours}:{minutes:02d}:{remainder:06.3f}"
        return f"{int(minutes)}:{remainder:06.3f}"

    @staticmethod
    def _is_previewable_file(path: str | Path) -> bool:
        candidate = Path(path)
        return (
            is_text_document_path(candidate)
            or candidate.suffix.lower() in MEDIA_SUFFIXES
        )

    @staticmethod
    def _document_coverage_label(document) -> str:
        metadata = getattr(document, "metadata", {})
        header_count = int(metadata.get("header_count", 0) or 0)
        if not header_count:
            if not metadata.get("full_pool_scan"):
                return ""
            entry_count = int(metadata.get("entry_count", 0) or 0)
            unreferenced = int(metadata.get("unreferenced_text_count", 0) or 0)
            invalid = int(metadata.get("invalid_pointer_count", 0) or 0)
            coverage = f"　|　DAT 物理文本池 {entry_count} 项"
            if unreferenced:
                coverage += f"　|　未被有效指针引用 {unreferenced} 项"
            if invalid:
                coverage += f"　|　警告：显式无效指针 {invalid} 个（需参考 PAC 修复）"
            return coverage
        exact = int(metadata.get("exact_header_count", 0) or 0)
        compatible = int(metadata.get("compatible_header_count", 0) or 0)
        unknown = max(0, header_count - exact - compatible)
        fallback = int(metadata.get("fallback_text_count", 0) or 0)
        resolved_game = str(metadata.get("resolved_game", ""))
        layout_game = str(metadata.get("layout_game", "") or "")
        game_label = {
            GameVersion.SORA1.value: "the 1st",
            GameVersion.SORA2.value: "the 2nd",
        }.get(resolved_game, resolved_game)
        detection = str(metadata.get("game_detection", ""))
        detection_label = {
            "layout": "结构识别",
            "manual": "手动指定",
            "fallback": "自动回退",
        }.get(detection, detection)
        coverage = (
            f"　|　版本 {game_label}（{detection_label}）"
            f"　|　Header 精确 {exact} / 兼容 {compatible} / 未知 {unknown}"
        )
        if fallback:
            coverage += f"　|　结构外文本 {fallback}"
        if metadata.get("roundtrip_certified"):
            coverage += "　|　重建回环已认证"
        if detection == "manual" and layout_game and layout_game != resolved_game:
            layout_label = {
                GameVersion.SORA1.value: "the 1st",
                GameVersion.SORA2.value: "the 2nd",
            }.get(layout_game, layout_game)
            coverage += f"　|　注意：主布局更接近 {layout_label}"
        return coverage

    @staticmethod
    def _preview(value: str, limit: int = 96) -> str:
        compact = value.replace("\r", "\\r").replace("\n", "\\n")
        return compact if len(compact) <= limit else compact[: limit - 1] + "…"

    @staticmethod
    def _split_globs(raw: str) -> list[str]:
        return [item.strip() for item in (raw or "*.tbl,*.dat").split(",") if item.strip()]

    @staticmethod
    def _logical_matches(logical_path: str, patterns: list[str]) -> bool:
        normalized = logical_path.replace("\\", "/")
        name = Path(normalized).name
        return any(
            fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(name, pattern)
            for pattern in patterns
        )

    def _set_text(self, widget: tk.Text, value: str, *, readonly: bool) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled" if readonly else "normal")

    def _append(self, widget: tk.Text, lines: list[str]) -> None:
        widget.configure(state="normal")
        if widget.get("1.0", "end-1c"):
            widget.insert("end", "\n")
        widget.insert("end", "\n".join(lines))
        widget.see("end")
        widget.configure(state="disabled")

    def _route_message(
        self,
        path: Path | None = None,
        actual_engine: str | None = None,
    ) -> str:
        tbl_engine, dat_engine = self.current_engine_preferences()
        selected = (
            tbl_engine
            if path and path.suffix.lower() == ".tbl"
            else dat_engine
            if path and path.suffix.lower() == ".dat"
            else None
        )
        base = f"全局文本引擎：TBL={tbl_engine}，DAT={dat_engine}。"
        if selected:
            base += f" 当前文件将使用 {selected}。"
        if actual_engine:
            base += f" 实际引擎：{actual_engine}。"
        if self.allow_risky_repack_var.get():
            base += (
                " 实验性回退写入已开启：仅在无法建立结构化引用关系时使用；"
                "暂存产物仍必须通过全文与结构校验。"
            )
        else:
            base += " 布局保持型 TBL/DAT 变长写入无需实验开关；启发式回退默认关闭。"
        selected_game = self.current_game_version()
        if selected_game is GameVersion.AUTO:
            base += " 游戏版本：按每个 TBL 的记录布局自动识别 1st/2nd；无法区分时回退 1st。"
        elif selected_game is GameVersion.SORA1:
            base += " 游戏版本：未识别布局时按 the 1st；已识别的包内混合布局仍以文件结构为准。"
        else:
            base += " 游戏版本：未识别布局时按 the 2nd；已识别的包内混合布局仍以文件结构为准。"
        return base

    def _ensure_idle(self) -> bool:
        if (getattr(self, "_content_running", False)
                and not getattr(self, "_content_navigating", False)):
            self._cancel_content_search()
        if self._busy:
            self.status_var.set("当前仍有任务在运行，请稍候。")
            return False
        return True

    def _run_background(
        self,
        label: str,
        worker,
        completed,
        *,
        progressbar: ttk.Progressbar | None = None,
        progress_text_var: tk.StringVar | None = None,
    ) -> None:
        if getattr(self, "_content_running", False):
            self._cancel_content_search()
        self._busy = True
        self.status_var.set(label)
        self._active_progressbar = progressbar
        self._active_progress_text_var = progress_text_var
        if progress_text_var is not None:
            progress_text_var.set(label)
        if progressbar is not None:
            progressbar.stop()
            progressbar.configure(mode="indeterminate", maximum=100, value=0)
            progressbar.start(12)

        def run() -> None:
            try:
                self._background_queue.put(("ok", completed, worker()))
            except Exception as exc:
                self._background_queue.put(("error", completed, exc))

        threading.Thread(target=run, daemon=True).start()
        self.root.after(80, self._poll_background)

    def _post_background_progress(self, done: int, total: int, name: str) -> None:
        self._background_queue.put(("progress", None, (done, total, name)))

    def _poll_background(self) -> None:
        latest_progress = None
        while True:
            try:
                kind, completed, payload = self._background_queue.get_nowait()
            except queue.Empty:
                if latest_progress is not None:
                    done, total, name = latest_progress
                    if self._active_progressbar is not None:
                        self._active_progressbar.stop()
                        self._active_progressbar.configure(
                            mode="determinate", maximum=max(1, total), value=done,
                        )
                    if self._active_progress_text_var is not None:
                        self._active_progress_text_var.set(f"已处理 {done}/{total}：{name}")
                self.root.after(80, self._poll_background)
                return
            if kind != "progress":
                break
            latest_progress = payload
        self._busy = False
        if kind == "error":
            self._finish_active_progress("任务失败。", success=False)
            messagebox.showerror("操作失败", str(payload), parent=self.root)
            self.status_var.set("操作失败。")
            self._clear_active_progress()
            return
        self._finish_active_progress("任务完成。", success=True)
        try:
            completed(payload)
        except Exception as exc:
            if self._active_progress_text_var is not None:
                self._active_progress_text_var.set("任务完成，但界面更新失败。")
            messagebox.showerror("更新界面失败", str(exc), parent=self.root)
            self.status_var.set("操作完成，但界面更新失败。")
        finally:
            self._clear_active_progress()

    def _finish_active_progress(self, message: str, *, success: bool) -> None:
        if self._active_progressbar is not None:
            self._active_progressbar.stop()
            self._active_progressbar.configure(
                mode="determinate",
                maximum=100,
                value=100 if success else 0,
            )
        if self._active_progress_text_var is not None:
            self._active_progress_text_var.set(message)

    def _clear_active_progress(self) -> None:
        self._active_progressbar = None
        self._active_progress_text_var = None

    @staticmethod
    def _reset_task_progress(
        progressbar: ttk.Progressbar,
        progress_text_var: tk.StringVar,
        message: str,
    ) -> None:
        progressbar.stop()
        progressbar.configure(mode="determinate", maximum=100, value=0)
        progress_text_var.set(message)

    def on_close(self) -> None:
        self._cancel_content_search()
        if self._busy:
            messagebox.showinfo(
                "任务仍在运行",
                "请等待当前解析、扫描或构建任务结束后再关闭程序。",
                parent=self.root,
            )
            return
        if not self._confirm_pending_changes():
            return
        dirty_projects = [
            project.workspace.archive.source_path.name
            for project in self.workbench.projects()
            if project.workspace.unbuilt_entry_names()
        ]
        if dirty_projects and not messagebox.askyesno(
            "退出并销毁会话工作区",
            (
                "以下 PAC 含有尚未构建回包的修改：\n"
                + "\n".join(dirty_projects)
                + "\n\n退出程序会永久销毁这些会话工作区及其修改。是否继续？"
            ),
            parent=self.root,
        ):
            return
        self._cancel_preview_model_redraw()
        self._close_preview_playback()
        if not self._stop_preview_model_worker():
            self.status_var.set("GPU 预览资源仍在释放，请稍后再次关闭。")
            messagebox.showinfo(
                "正在释放预览资源",
                "GPU 预览线程仍在结束当前驱动调用。为避免在 OpenGL 上下文仍在使用时退出，"
                "本次关闭已暂缓；请稍后再次关闭。",
                parent=self.root,
            )
            return
        try:
            self.workbench.close_all()
        finally:
            try:
                cleanup_runtime_dir(self.pac_session_root)
            finally:
                try:
                    self.pac_compare.close()
                finally:
                    self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def launch() -> None:
    _apply_process_dpi_awareness()
    root = tk.Tk()
    RetextTkApp(root).run()


if __name__ == "__main__":
    launch()
