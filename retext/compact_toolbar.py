"""Compact actions that wrap at the available width instead of filling tall grids."""
import tkinter as tk
from tkinter import ttk


class CompactToolbar(ttk.Frame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.buttons = []
        self._pending = None
        self._layout_key = None
        self.bind("<Configure>", self._schedule)
        self.bind("<Map>", self._schedule)

    def add(self, text, command, **kwargs):
        button = ttk.Button(self, text=text, command=command, style=kwargs.pop("style", "Compact.TButton"), **kwargs)
        self.buttons.append(button)
        self._schedule()
        return button

    def _schedule(self, event=None):
        if self._pending is None:
            self._pending = self.after_idle(self._layout)

    def _layout(self):
        self._pending = None
        key = (self.winfo_width(), tuple(b.winfo_reqwidth() for b in self.buttons), tuple(b.winfo_reqheight() for b in self.buttons))
        if key == self._layout_key:
            return
        self._layout_key = key
        available = max(1, key[0])
        top = used = 0
        height = max((button.winfo_reqheight() for button in self.buttons), default=1)
        for button, width in zip(self.buttons, key[1]):
            if used and used + width > available:
                top, used = top + height + 3, 0
            button.place(x=used, y=top, width=min(width, available), height=height)
            used += width + 4
        self.configure(height=top + height)

    def destroy(self):
        if self._pending is not None:
            self.after_cancel(self._pending)
        super().destroy()
