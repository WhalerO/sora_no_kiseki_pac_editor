"""Pooled visible-cell bold highlighting for the version comparison tree."""
import tkinter as tk
from tkinter import font as tkfont, ttk

from .diff_presentation import difference_spans
from .text_presentation import excerpt_parts


class DiffTextCells:
    def __init__(self, tree, get_row):
        self.tree, self.get_row = tree, get_row
        self.pending = None
        self.cells = {}
        self.pool = []
        self.cache = {}
        self.bold = tkfont.nametofont("TkDefaultFont").copy()
        for event in ("<Configure>", "<Map>", "<Unmap>", "<B1-Motion>",
                      "<ButtonRelease-1>", "<<TreeviewSelect>>", "<KeyRelease>"):
            tree.bind(event, lambda _event: self.schedule(), add="+")
        tree.configure(
            yscrollcommand=lambda a, b: self.scrollbar(tree._tis_ybar, a, b),
            xscrollcommand=lambda a, b: self.scrollbar(tree._tis_xbar, a, b),
        )
        tree.bind("<Destroy>", self.destroy, add="+")

    def scrollbar(self, bar, first, last):
        bar.set(first, last)
        self.schedule()

    def schedule(self):
        if self.pending is None and self.tree.winfo_exists():
            self.pending = self.tree.after(16, self.render)

    def reset(self):
        self.cache.clear()
        for cell in self.cells.values():
            cell.place_forget()
            self.pool.append(cell)
        self.cells.clear()
        self.schedule()

    def destroy(self, _event=None):
        if self.pending is not None:
            self.tree.after_cancel(self.pending)
            self.pending = None

    def make_cell(self):
        cell = tk.Text(
            self.tree, wrap="none", height=1, relief="flat", borderwidth=0,
            highlightthickness=0, padx=6, pady=2, takefocus=False,
            cursor="arrow", exportselection=False, font="TkDefaultFont",
        )
        cell.bind("<Button-1>", self.select)
        cell.bind("<MouseWheel>", self.wheel)
        cell.bind("<Shift-MouseWheel>", lambda event: self.wheel(event, horizontal=True))
        return cell

    def select(self, event):
        item = self.tree.identify_row(event.widget.winfo_y() + event.y)
        if item:
            self.tree.selection_set(item)
            self.tree.focus(item)
        return "break"

    def wheel(self, event, horizontal=False):
        command = self.tree.xview_scroll if horizontal else self.tree.yview_scroll
        command(-int(event.delta / 120), "units")
        self.schedule()
        return "break"

    def render(self):
        self.pending = None
        tree = self.tree
        if not tree.winfo_exists():
            return
        if not tree.winfo_ismapped():
            for cell in self.cells.values():
                cell.place_forget()
            return
        font = tkfont.nametofont("TkDefaultFont")
        properties = font.actual()
        font_key = tuple(sorted(properties.items()))
        self.bold.configure(**{**properties, "weight": "bold"})
        step = max(4, int(ttk.Style(tree).lookup("Treeview", "rowheight") or 24) // 2)
        visible = {item for y in range(0, tree.winfo_height() + step, step)
                   if (item := tree.identify_row(y))}
        for key in list(self.cells):
            if key[0] not in visible:
                cell = self.cells.pop(key)
                cell.place_forget()
                self.pool.append(cell)
        selected = set(tree.selection())
        displayed = set()
        for item in visible:
            try:
                row = self.get_row(item)
            except (IndexError, ValueError, KeyError):
                continue
            old, new = row.text_old or "", row.text_new or ""
            spans_key = (old, new)
            spans = self.cache.get(spans_key)
            if spans is None:
                if len(self.cache) > 1024:
                    self.cache.clear()
                self.cache[spans_key] = spans = difference_spans(old, new)
            for column, text, span in (("old", old, spans[0]), ("new", new, spans[1])):
                bounds = tree.bbox(item, column)
                if not bounds:
                    continue
                key = (item, column)
                displayed.add(key)
                if key not in self.cells:
                    self.cells[key] = self.pool.pop() if self.pool else self.make_cell()
                cell = self.cells[key]
                signature = (text, span, bounds[2], font_key)
                if getattr(cell, "_signature", None) != signature:
                    parts = excerpt_parts(text, *span, width=max(1, bounds[2] - 12),
                                          measure=font.measure, measure_match=self.bold.measure)
                    cell.configure(state="normal")
                    cell.delete("1.0", "end")
                    cell.insert("end", parts[0])
                    cell.insert("end", parts[1], ("changed",))
                    cell.insert("end", parts[2])
                    cell.configure(state="disabled")
                    cell._signature = signature
                cell.configure(background="#dbeafe" if item in selected else "#ffffff",
                               foreground="#132d50" if item in selected else "#202b3b")
                cell.tag_configure("changed", font=self.bold)
                x, y, width, height = bounds
                cell.place(x=x, y=y, width=width, height=height)
        for key in list(self.cells):
            if key not in displayed:
                cell = self.cells.pop(key)
                cell.place_forget()
                self.pool.append(cell)
