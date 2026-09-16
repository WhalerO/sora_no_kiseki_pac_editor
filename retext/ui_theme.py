"""Shared DPI-aware typography and spacing for the desktop editor."""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, ttk


def apply_theme(root: tk.Tk, style: ttk.Style, native_scaling: float, scale: float) -> None:
    root.tk.call("tk", "scaling", native_scaling)
    family = "Microsoft YaHei UI" if "Microsoft YaHei UI" in tkfont.families(root) else "TkDefaultFont"
    size = max(7, round(11 * scale))
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkIconFont", "TkCaptionFont", "TkTooltipFont"):
        tkfont.nametofont(name).configure(family=family, size=size)
    dpi = native_scaling / (96 / 72)
    gap = max(3, round(5 * dpi * scale))
    line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    root.option_add("*Text.font", "TkTextFont")
    root.option_add("*Listbox.font", "TkDefaultFont")
    root.option_add("*TCombobox*Listbox.font", "TkDefaultFont")
    style.configure(".", font="TkDefaultFont")
    for name, color in (("TFrame", "#f3f5f8"), ("Panel.TFrame", "#f3f5f8"),
                        ("Card.TFrame", "#ffffff"), ("Toolbar.TFrame", "#ffffff")):
        style.configure(name, background=color, borderwidth=0)
    for name, color, background in (("TLabel", "#202b3b", "#ffffff"),
                                    ("Toolbar.TLabel", "#37465a", "#ffffff"),
                                    ("Subtitle.TLabel", "#526176", "#f3f5f8"),
                                    ("Muted.TLabel", "#526176", "#ffffff")):
        style.configure(name, font="TkDefaultFont", foreground=color, background=background)
    style.configure("Title.TLabel", font=(family, round(16 * scale), "bold"), foreground="#192d48", background="#f3f5f8")
    style.configure("Section.TLabel", font=(family, size, "bold"), foreground="#263c59", background="#ffffff")
    style.configure("TButton", padding=(gap * 2, gap), foreground="#253750", background="#edf2f8", borderwidth=1)
    style.map("TButton", background=[("pressed", "#d1e0f1"), ("active", "#e0eafa")])
    style.configure("Compact.TButton", padding=(gap, max(2, gap // 2)), width=0)
    style.configure("Accent.TButton", background="#245fa8", foreground="#ffffff")
    style.map("Accent.TButton", background=[("pressed", "#184578"), ("active", "#3275c4")], foreground=[("!disabled", "#ffffff")])
    style.configure("TCheckbutton", background="#ffffff", foreground="#253750", padding=(2, gap // 2))
    style.configure("TEntry", padding=(gap, gap // 2), fieldbackground="#ffffff")
    style.configure("TCombobox", padding=(gap, gap // 2))
    style.configure("Treeview", font="TkDefaultFont", rowheight=line + 2 * gap,
                    background="#ffffff", fieldbackground="#ffffff", foreground="#202b3b", borderwidth=0)
    style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", "#132d50")])
    style.configure("Treeview.Heading", font="TkHeadingFont", background="#e9eef5", foreground="#30445f", padding=(gap, gap), relief="flat")
    style.configure("TNotebook", background="#f3f5f8", borderwidth=0)
    style.configure("TNotebook.Tab", padding=(gap * 2, gap))
    style.map("TNotebook.Tab", background=[("selected", "#ffffff"), ("active", "#e5edf8")], foreground=[("selected", "#174f91")])
    style.configure("TPanedwindow", background="#e0e6ee", sashwidth=max(6, round(6 * dpi)))
    root.configure(background="#f3f5f8")
