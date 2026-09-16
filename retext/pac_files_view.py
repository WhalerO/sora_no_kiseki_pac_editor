"""Explorer-style PAC view; container mutations remain in the workbench."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import tkinter as tk
from tkinter import ttk

from .archive.collection import PacNodeRef
from .compact_toolbar import CompactToolbar


@dataclass(frozen=True)
class DirectoryItem:
    path: str
    folder: bool
    size: int
    state: str = ""


def directory_index(entries):
    """Index names/sizes once without extracting any payloads."""
    items = {}
    for name, size, state in entries:
        parts = name.split("/")
        items[name] = DirectoryItem(name, False, size, state)
        for count in range(1, len(parts)):
            folder = "/".join(parts[:count])
            old = items.get(folder)
            items[folder] = DirectoryItem(folder, True, size + (old.size if old else 0))
    result = {"": []}
    for item in items.values():
        parent = str(PurePosixPath(item.path).parent)
        result.setdefault("" if parent == "." else parent, []).append(item)
        if item.folder:
            result.setdefault(item.path, [])
    for children in result.values():
        children.sort(key=lambda item: (not item.folder, item.path.casefold()))
    return result


class PacFilesView(ttk.Frame):
    def __init__(self, master, *, workbench, open_pacs, extract, replace, insert, export,
                 open_file, format_size, state_label, ensure_idle):
        super().__init__(master, padding=12)
        self.workbench, self.open_file = workbench, open_file
        self.format_size, self.state_label = format_size, state_label
        self.ensure_idle = ensure_idle
        self.workspace_id = ""
        self.folder = ""
        self.index = {"": []}
        self.ids = []
        self.refs = {}
        self.pac_var = tk.StringVar(self)
        self.path_var = tk.StringVar(self, "尚未打开 PAC")
        self.filter_var = tk.StringVar(self)
        selector = ttk.Frame(self)
        selector.pack(fill="x")
        ttk.Label(selector, text="当前 PAC").pack(side="left", padx=(0, 8))
        self.selector = ttk.Combobox(selector, textvariable=self.pac_var, state="readonly")
        self.selector.pack(side="left", fill="x", expand=True)
        self.selector.bind("<<ComboboxSelected>>", self._choose)
        ttk.Button(selector, text="打开 PAC…", command=open_pacs, style="Compact.TButton").pack(side="left", padx=(8, 0))
        toolbar = CompactToolbar(self)
        toolbar.pack(fill="x", pady=(8, 4))
        toolbar.add("上一级", self.up)
        toolbar.add("提取所选…", lambda: extract(self.selected_refs()))
        toolbar.add("替换文件…", lambda: replace(self.selected_refs()))
        toolbar.add("插入文件…", lambda: insert(self.folder_refs()))
        toolbar.add("导出 PAC…", lambda: export(self.workspace_id or None))
        ttk.Entry(self, textvariable=self.path_var, state="readonly").pack(fill="x", pady=(0, 6))
        filters = ttk.Frame(self)
        filters.pack(fill="x", pady=(0, 6))
        ttk.Label(filters, text="筛选当前文件夹").pack(side="left", padx=(0, 6))
        ttk.Entry(filters, textvariable=self.filter_var).pack(side="left", fill="x", expand=True)
        self.filter_var.trace_add("write", lambda *_: self.render())
        host = ttk.Frame(self)
        host.pack(fill="both", expand=True)
        host.columnconfigure(0, weight=1)
        host.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(host, columns=("type", "size", "state"), selectmode="extended")
        self.tree.heading("#0", text="名称")
        self.tree.column("#0", width=360, minwidth=140)
        for key, label, width in (("type", "类型", 100), ("size", "大小", 120), ("state", "状态", 110)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=60, stretch=False)
        bar = ttk.Scrollbar(host, command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", self._activate)
        self.tree.bind("<Return>", self._activate)
        self.tree.bind("<Alt-Up>", lambda _e: self.up())
        hint = ttk.Label(self, text="双击文件夹进入，双击文件查看；插入到当前目录。修改先保存在工作区，最后导出新 PAC。", style="Muted.TLabel", wraplength=900)
        hint.pack(fill="x", pady=(7, 0))
        hint.bind("<Configure>", lambda event: hint.configure(wraplength=max(120, event.width)))

    def refresh(self):
        projects = self.workbench.projects()
        self.ids = [p.workspace.workspace_id for p in projects]
        self.selector.configure(values=[str(p.workspace.archive.source_path) for p in projects])
        if self.workspace_id not in self.ids:
            self.workspace_id = self.ids[0] if self.ids else ""
            self.folder = ""
        if self.workspace_id:
            self.selector.current(self.ids.index(self.workspace_id))
            workspace = self.workbench.get(self.workspace_id).workspace
            self.index = directory_index((e.name, workspace.current_size(e.name), self.state_label(workspace.entry_state(e.name))) for e in workspace.entries())
            if self.folder not in self.index:
                self.folder = ""
        else:
            self.pac_var.set("")
            self.index = {"": []}
        self.render()

    def _choose(self, _event=None):
        if not self.ensure_idle():
            if self.workspace_id in self.ids:
                self.selector.current(self.ids.index(self.workspace_id))
            return
        index = self.selector.current()
        if 0 <= index < len(self.ids):
            self.workspace_id, self.folder = self.ids[index], ""
            self.filter_var.set("")
            self.refresh()

    def show_ref(self, ref):
        self.workspace_id = ref.workspace_id
        self.folder = ref.path if ref.kind == "folder" else (str(PurePosixPath(ref.path).parent) if ref.kind == "file" else "")
        if self.folder == ".":
            self.folder = ""
        self.filter_var.set("")
        self.refresh()
        for iid, candidate in self.refs.items():
            if candidate == ref:
                self.tree.selection_set(iid)
                self.tree.see(iid)

    def render(self):
        selection = {self.refs[iid].key for iid in self.tree.selection() if iid in self.refs}
        self.tree.delete(*self.tree.get_children())
        self.refs.clear()
        self.path_var.set("/" + self.folder if self.workspace_id else "尚未打开 PAC")
        query = self.filter_var.get().casefold()
        for item in self.index.get(self.folder, []):
            name = PurePosixPath(item.path).name
            if query and query not in name.casefold():
                continue
            ref = PacNodeRef(self.workspace_id, "folder" if item.folder else "file", item.path)
            iid = self.tree.insert("", "end", text=name, values=("文件夹" if item.folder else PurePosixPath(name).suffix.lstrip(".").upper() or "文件", self.format_size(item.size), item.state))
            self.refs[iid] = ref
            if ref.key in selection:
                self.tree.selection_add(iid)

    def folder_refs(self):
        return [PacNodeRef(self.workspace_id, "folder" if self.folder else "pac", self.folder)] if self.workspace_id else []

    def selected_refs(self):
        return [self.refs[i] for i in self.tree.selection() if i in self.refs]

    def up(self):
        if self.ensure_idle() and self.folder:
            parent = str(PurePosixPath(self.folder).parent)
            self.folder = "" if parent == "." else parent
            self.filter_var.set("")
            self.render()
        return "break"

    def _activate(self, event=None):
        if not self.ensure_idle():
            return "break"
        if event is not None and getattr(event, "num", None) == 1:
            iid = self.tree.identify_row(event.y)
            if not iid:
                return "break"
            self.tree.selection_set(iid)
        refs = self.selected_refs()
        if len(refs) == 1:
            ref = refs[0]
            if ref.kind == "folder":
                self.folder = ref.path
                self.filter_var.set("")
                self.render()
            else:
                self.open_file(ref)
        return "break"
