"""Enable native Windows rendering before creating any Tk window."""
from __future__ import annotations

import ctypes
import sys


def configure_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        setter = user32.SetProcessDpiAwarenessContext
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_int
        if setter(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
        # A manifest can already have selected the correct context.
        if ctypes.get_last_error() == 5:
            return
    except (AttributeError, OSError):
        pass
    try:
        setter = ctypes.WinDLL("shcore").SetProcessDpiAwareness
        setter.argtypes = [ctypes.c_int]
        setter.restype = ctypes.c_long
        if setter(2) == 0:  # PER_MONITOR_DPI_AWARE
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.WinDLL("user32").SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def window_tk_scaling(root) -> float:
    """Return physical pixels per point for this window's current monitor."""
    if sys.platform == "win32":
        try:
            getter = ctypes.WinDLL("user32").GetDpiForWindow
            getter.argtypes = [ctypes.c_void_p]
            getter.restype = ctypes.c_uint
            dpi = getter(ctypes.c_void_p(root.winfo_id()))
            if dpi:
                return dpi / 72.0
        except (AttributeError, OSError):
            pass
    return float(root.tk.call("tk", "scaling"))


def current_dpi_awareness() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.WinDLL("user32")
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        return int(user32.GetAwarenessFromDpiAwarenessContext(
            user32.GetThreadDpiAwarenessContext()))
    except (AttributeError, OSError):
        return None
