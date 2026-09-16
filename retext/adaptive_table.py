"""Virtual, variable-height text table for Tk 8.6.

Rows keep their full values. Only visible cells are wrapped/painted; the capped
display is never used as an editing or saving source. The small Treeview-like
interface lets preview and inline editing share the same presentation widget.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
import tkinter as tk
from tkinter import font as tkfont, ttk

from .text_presentation import excerpt_parts, wrap_bounded


@dataclass
class _Row:
    values: tuple
    tags: tuple = ()
    lines: dict[str, list[str]] = field(default_factory=dict)
    height: int = 0
    contexts: dict[str, tuple[int, int]] = field(default_factory=dict)
    highlights: dict[str, tuple[int, int]] = field(default_factory=dict)


class AdaptiveTextTable(tk.Canvas):
    """Flat, single-selection table with capped, lazily measured row heights."""

    def __init__(self, master, *, columns, **kwargs):
        self._columns = tuple(columns)
        self._display = self._columns
        self._column_options = {c: dict(id=c, width=160, minwidth=50, stretch=True, anchor="w") for c in columns}
        self._headings = {c: c for c in columns}
        self._rows: dict[str, _Row] = {}
        self._order: list[str] = []
        self._positions: dict[str, int] = {}
        self._prefix = [0]
        self._prefix_dirty = False
        self._serial = 0
        self._selected = ""
        self._focused = ""
        self._tags: dict[str, dict] = {}
        self._top = self._left = 0
        self._widths: dict[str, int] = {}
        self._pending = None
        self._drag = None
        self._reveal = None
        self._ycommand = self._xcommand = None
        super().__init__(master, background="#ffffff", highlightthickness=0,
                         borderwidth=0, takefocus=True, **kwargs)
        self.refresh_metrics()
        self.bind("<Configure>", self._on_resize)
        self.bind("<Button-1>", self._on_click)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Motion>", self._on_motion)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Shift-MouseWheel>", lambda e: self._on_wheel(e, horizontal=True))
        self.bind("<Button-4>", lambda e: self.yview_scroll(-3, "units"))
        self.bind("<Button-5>", lambda e: self.yview_scroll(3, "units"))
        for key in ("Up", "Down", "Home", "End", "Prior", "Next"):
            self.bind(f"<{key}>", self._on_key)

    def refresh_metrics(self):
        self._font = tkfont.nametofont("TkDefaultFont")
        self._heading_font = tkfont.nametofont("TkHeadingFont")
        self.line_height = self._font.metrics("linespace")
        self.base_rowheight = int(ttk.Style(self).lookup("Treeview", "rowheight") or self.line_height + 10)
        self.base_rowheight = max(self.line_height + 8, self.base_rowheight)
        self.padding = max(4, (self.base_rowheight - self.line_height) // 2)
        self.max_rowheight = self.base_rowheight * 5
        self.header_height = self.base_rowheight
        self._invalidate()

    def _schedule(self):
        if self._pending is None:
            self._pending = self.after_idle(self._paint)

    def _ensure_prefix(self):
        if self._prefix_dirty:
            self._prefix = [0]
            self._positions = {}
            for index, iid in enumerate(self._order):
                self._positions[iid] = index
                self._prefix.append(self._prefix[-1] + (self._rows[iid].height or self.base_rowheight))
            self._prefix_dirty = False

    def _anchor(self):
        self._ensure_prefix()
        index = min(len(self._order) - 1, max(0, bisect_right(self._prefix, self._top) - 1))
        return (self._order[index], self._top - self._prefix[index]) if self._order else ("", 0)

    def _restore_anchor(self, anchor):
        self._ensure_prefix()
        iid, offset = anchor
        if iid in self._positions:
            self._top = self._prefix[self._positions[iid]] + min(offset, (self._rows[iid].height or self.base_rowheight) - 1)

    def _invalidate(self):
        anchor = self._anchor()
        for row in self._rows.values():
            row.lines.clear()
            row.height = 0
        self._prefix_dirty = True
        self._restore_anchor(anchor)
        self._schedule()

    def _update_widths(self):
        visible = self._display
        widths = {c: max(int(self._column_options[c]["minwidth"]), int(self._column_options[c]["width"])) for c in visible}
        extra = max(0, self.winfo_width() - sum(widths.values()))
        stretch = [c for c in visible if self._column_options[c]["stretch"]]
        for index, c in enumerate(stretch):
            widths[c] += extra // len(stretch) + (index < extra % len(stretch))
        deficit = sum(widths.values()) - self.winfo_width()
        while deficit > 0:
            shrink = [c for c in stretch if widths[c] > self._column_options[c]["minwidth"]]
            if not shrink:
                break
            for c in shrink:
                take = min(deficit, max(1, deficit // len(shrink)), widths[c] - self._column_options[c]["minwidth"])
                widths[c] -= take
                deficit -= take
        if widths != self._widths:
            self._widths = widths
            self._invalidate()

    def _layout_row(self, iid):
        row = self._rows[iid]
        if row.height:
            return
        max_lines = max(1, (self.max_rowheight - 2 * self.padding) // self.line_height)
        for col in self._display:
            i = self._columns.index(col)
            value = str(row.values[i]) if i < len(row.values) else ""
            width = max(1, self._widths.get(col, 160) - 2 * self.padding)
            if col in row.contexts:
                parts = excerpt_parts(value, *row.contexts[col], width=width, measure=self._font.measure)
                value = "".join(parts)
                row.highlights[col] = (self._font.measure(parts[0]), self._font.measure(parts[1]))
            row.lines[col] = wrap_bounded(value, width, self._font.measure, max_lines)
        row.height = min(self.max_rowheight, max(self.base_rowheight,
                         max((len(v) for v in row.lines.values()), default=1) * self.line_height + 2 * self.padding))
        self._prefix_dirty = True

    def _body_height(self):
        return max(1, self.winfo_height() - self.header_height)

    def _clamp(self):
        self._ensure_prefix()
        self._top = max(0, min(self._top, max(0, self._prefix[-1] - self._body_height())))
        self._left = max(0, min(self._left, max(0, sum(self._widths.values()) - self.winfo_width())))

    def _prepare_view(self):
        self._update_widths()
        self._clamp()
        while True:
            anchor = self._anchor()
            start = self._positions.get(anchor[0], 0)
            # Measure only enough rows to fill this viewport (not the whole book).
            space = -anchor[1]
            measured = False
            for index in range(start, len(self._order)):
                iid = self._order[index]
                measured |= not bool(self._rows[iid].height)
                self._layout_row(iid)
                space += self._rows[iid].height
                if space >= self._body_height():
                    break
            self._restore_anchor(anchor)
            if self._reveal in self._positions:
                index = self._positions[self._reveal]
                top, bottom = self._prefix[index:index + 2]
                if top < self._top or bottom - top > self._body_height():
                    self._top = top
                elif bottom > self._top + self._body_height():
                    self._top = bottom - self._body_height()
            self._clamp()
            if not measured:
                break
        self._reveal = None

    def _paint(self):
        self._pending = None
        if not self.winfo_exists():
            return
        self._prepare_view()
        super().delete("all")
        start = max(0, bisect_right(self._prefix, self._top) - 1)
        for index in range(start, len(self._order)):
            iid = self._order[index]
            row = self._rows[iid]
            y = self.header_height + self._prefix[index] - self._top
            if y >= self.winfo_height():
                break
            self._layout_row(iid)
            color = "#ffffff" if index % 2 == 0 else "#f8fafc"
            for tag in row.tags:
                color = self._tags.get(tag, {}).get("background", color)
            selected = iid == self._selected
            self.create_rectangle(0, max(y, self.header_height), self.winfo_width(), y + row.height,
                                  fill="#dbeafe" if selected else color, outline="")
            x = -self._left
            for col in self._display:
                width = self._widths[col]
                if x + width > 0 and x < self.winfo_width():
                    lines = row.lines[col]
                    for line_index, line in enumerate(lines):
                        line_y = y + self.padding + line_index * self.line_height
                        if line_y >= self.header_height and line_y + self.line_height <= self.winfo_height():
                            if col in row.contexts and line_index == 0:
                                offset, length = row.highlights[col]
                                self.create_rectangle(x + self.padding + offset, line_y,
                                                      x + self.padding + offset + length, line_y + self.line_height,
                                                      fill="#ffe39a", outline="")
                            self.create_text(x + self.padding, line_y, text=line, font=self._font,
                                             anchor="nw", fill="#132d50" if selected else "#202b3b")
                    self.create_line(x + width, max(y, self.header_height), x + width, y + row.height, fill="#e6ebf1")
                x += width
            self.create_line(0, y + row.height, self.winfo_width(), y + row.height, fill="#e6ebf1")
        self.create_rectangle(0, 0, self.winfo_width(), self.header_height, fill="#e9eef5", outline="")
        x = -self._left
        for col in self._display:
            width = self._widths[col]
            heading = wrap_bounded(self._headings[col], width - 2 * self.padding, self._heading_font.measure, 1)[0]
            self.create_text(x + self.padding, self.padding, text=heading, anchor="nw", font=self._heading_font, fill="#30445f")
            x += width
            self.create_line(x, 0, x, self.header_height, fill="#cdd7e5")
        self._ensure_prefix()
        for callback, fractions in ((self._ycommand, self.yview()), (self._xcommand, self.xview())):
            if callback:
                callback(*fractions)

    def configure(self, cnf=None, **kwargs):
        if cnf is not None:
            return super().configure(cnf, **kwargs)
        changed = False
        if "displaycolumns" in kwargs:
            display = kwargs.pop("displaycolumns")
            self._display = self._columns if display == "#all" else tuple(display)
            changed = True
        for option, attr in (("yscrollcommand", "_ycommand"), ("xscrollcommand", "_xcommand")):
            if option in kwargs:
                setattr(self, attr, kwargs.pop(option))
        result = super().configure(**kwargs) if kwargs else None
        if changed:
            self._update_widths()
        return result

    config = configure

    def column(self, column, option=None, **kwargs):
        if str(column).startswith("#"):
            column = self._display[int(column[1:]) - 1]
        options = self._column_options[column]
        if kwargs:
            options.update(kwargs)
            self._update_widths()
            self._schedule()
        return options.get(option) if option else dict(options)

    def heading(self, column, **kwargs):
        if "text" in kwargs:
            self._headings[column] = kwargs["text"]
            self._schedule()
        return {"text": self._headings[column]}

    def insert(self, parent, index, iid=None, *, values=(), tags=(), **kwargs):
        if parent:
            raise ValueError("Text tables contain flat rows only")
        self._serial += 1
        iid = str(iid) if iid is not None else f"R{self._serial}"
        if iid in self._rows:
            raise ValueError(f"Duplicate row: {iid}")
        self._rows[iid] = _Row(tuple(values), tuple(tags))
        self._order.insert(len(self._order) if index == "end" else int(index), iid)
        self._prefix_dirty = True
        self._schedule()
        return iid

    def delete(self, *items):
        removed = set(items)
        for iid in removed:
            self._rows.pop(iid, None)
        self._order = [iid for iid in self._order if iid not in removed]
        if self._selected in removed:
            self._selected = ""
        if self._focused in removed:
            self._focused = ""
        if not self._order:
            self._top = 0
        self._prefix_dirty = True
        self._schedule()

    def get_children(self, item=None):
        return tuple(self._order) if not item else ()

    def exists(self, item):
        return item in self._rows

    def item(self, item, option=None, **kwargs):
        row = self._rows[item]
        if "values" in kwargs:
            row.values = tuple(kwargs["values"])
            row.lines.clear()
            row.contexts.clear()
            row.height = 0
            self._prefix_dirty = True
        if "tags" in kwargs:
            row.tags = tuple(kwargs["tags"])
        if kwargs:
            self._schedule()
        result = dict(values=row.values, tags=row.tags)
        return result[option] if option else result

    def tag_configure(self, tag, **kwargs):
        self._tags[tag] = kwargs
        self._schedule()

    def selection(self):
        return (self._selected,) if self._selected else ()

    def selection_set(self, item):
        item = item[0] if isinstance(item, (list, tuple)) and item else item
        self._selected = item if item in self._rows else ""
        self._schedule()
        self.event_generate("<<TreeviewSelect>>")

    def focus(self, item=None):
        if item is not None:
            self._focused = item
        return self._focused

    def show_context(self, item, column, span):
        self.clear_contexts()
        if span is not None:
            row = self._rows[item]
            row.contexts[column] = span
            row.height = 0
            self._prefix_dirty = True
            self._schedule()

    def clear_contexts(self):
        for row in self._rows.values():
            if row.contexts:
                row.contexts.clear()
                row.height = 0
                self._prefix_dirty = True
        self._schedule()

    def see(self, item):
        self._update_widths()
        self._layout_row(item)
        self._ensure_prefix()
        index = self._positions[item]
        top, bottom = self._prefix[index:index + 2]
        target = self._top
        if top < target or bottom - top > self._body_height():
            target = top
        elif bottom > target + self._body_height():
            target = bottom - self._body_height()
        self._scroll_to(top=target)
        self._reveal = item
        self._prepare_view()

    def bbox(self, item, column=None):
        if item not in self._rows:
            return ()
        self._update_widths()
        self._layout_row(item)
        self._ensure_prefix()
        y = self.header_height + self._prefix[self._positions[item]] - self._top
        height = self._rows[item].height
        if y + height <= self.header_height or y >= self.winfo_height():
            return ()
        x = -self._left
        if column is None:
            return (x, y, sum(self._widths.values()), height)
        if str(column).startswith("#"):
            column = self._display[int(column[1:]) - 1]
        for col in self._display:
            if col == column:
                return (x, y, self._widths[col], height)
            x += self._widths[col]
        return ()

    def identify_row(self, y):
        self._ensure_prefix()
        if y < self.header_height:
            return ""
        index = bisect_right(self._prefix, y - self.header_height + self._top) - 1
        return self._order[index] if 0 <= index < len(self._order) else ""

    def identify_column(self, x):
        right = -self._left
        for index, col in enumerate(self._display):
            right += self._widths.get(col, 0)
            if 0 <= x < right:
                return f"#{index + 1}"
        return ""

    def _geometry_changing(self):
        self.event_generate("<<TableGeometryChanged>>")

    def _scroll_to(self, *, top=None, left=None):
        if (top is not None and top != self._top) or (left is not None and left != self._left):
            self._geometry_changing()
        if top is not None:
            self._top = int(top)
        if left is not None:
            self._left = int(left)
        self._clamp()
        self._schedule()

    def yview(self, *args):
        self._ensure_prefix()
        if args:
            if args[0] == "moveto":
                self.yview_moveto(float(args[1]))
            else:
                self.yview_scroll(int(args[1]), args[2])
            return
        total = max(1, self._prefix[-1])
        return (self._top / total, min(1.0, (self._top + self._body_height()) / total))

    def yview_moveto(self, fraction):
        self._ensure_prefix()
        self._scroll_to(top=float(fraction) * self._prefix[-1])

    def yview_scroll(self, number, what):
        self._scroll_to(top=self._top + int(number) * (self._body_height() * .9 if what == "pages" else self.base_rowheight))

    def xview(self, *args):
        if args:
            if args[0] == "moveto":
                self.xview_moveto(float(args[1]))
            else:
                self.xview_scroll(int(args[1]), args[2])
            return
        total = max(1, sum(self._widths.values()))
        return (self._left / total, min(1.0, (self._left + self.winfo_width()) / total))

    def xview_moveto(self, fraction):
        self._scroll_to(left=float(fraction) * sum(self._widths.values()))

    def xview_scroll(self, number, what):
        self._scroll_to(left=self._left + int(number) * (self.winfo_width() * .9 if what == "pages" else 30))

    def _on_resize(self, event):
        self._geometry_changing()
        self._update_widths()
        self._schedule()

    def _resize_column(self, x, y):
        if y >= self.header_height:
            return None
        right = -self._left
        for col in self._display:
            right += self._widths.get(col, 0)
            if abs(x - right) <= 6:
                return col
        return None

    def _on_motion(self, event):
        self.configure(cursor="sb_h_double_arrow" if self._resize_column(event.x, event.y) else "arrow")

    def _on_click(self, event):
        self.focus_set()
        col = self._resize_column(event.x, event.y)
        if col:
            self._geometry_changing()
            self._drag = (col, event.x, self._widths[col])
            # Freeze the visible widths before resizing a heading boundary.
            for c, width in self._widths.items():
                self._column_options[c]["width"] = width
        else:
            iid = self.identify_row(event.y)
            if iid:
                self.selection_set(iid)
                self.focus(iid)

    def _on_drag(self, event):
        if self._drag:
            col, start, width = self._drag
            self.column(col, width=max(self._column_options[col]["minwidth"], width + event.x - start), stretch=False)

    def _on_release(self, event):
        self._drag = None

    def _on_wheel(self, event, horizontal=False):
        amount = -int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
        (self.xview_scroll if horizontal else self.yview_scroll)(amount * 3, "units")
        return "break"

    def _on_key(self, event):
        if not self._order:
            return "break"
        self._ensure_prefix()
        current = self._positions.get(self._selected, 0)
        step = max(1, self._body_height() // self.base_rowheight)
        target = {"Up": current - 1, "Down": current + 1, "Home": 0, "End": len(self._order) - 1,
                  "Prior": current - step, "Next": current + step}[event.keysym]
        iid = self._order[max(0, min(len(self._order) - 1, target))]
        self.selection_set(iid)
        self.focus(iid)
        self.see(iid)
        return "break"

    def destroy(self):
        if self._pending is not None:
            self.after_cancel(self._pending)
            self._pending = None
        super().destroy()
