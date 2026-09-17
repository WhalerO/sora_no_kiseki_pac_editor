"""Short Windows critical sections for publishing/reclaiming workspace locks."""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def workspace_lock_guard(root: Path):
    # Windows is the supported desktop target. Keep the existing POSIX lock
    # publication path separate: os.rename overwrites files on POSIX.
    if os.name != "nt":
        yield
        return

    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL

    # Canonicalize aliases/case. This digest names a kernel object; it is not
    # a fingerprint of game data and imposes no game-version requirement.
    key = os.path.normcase(str(root.resolve())).encode("utf-8")
    name = "Global\\TIS_Retext_Lock_" + hashlib.sha256(key).hexdigest()
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, 2000)
        # An abandoned mutex also grants ownership. Re-read the JSON under
        # this guard; never assume the previous process completed its update.
        if result in (0, 0x80):
            acquired = True
        elif result == 0x102:
            raise TimeoutError("其他进程正在更新工作区锁，请稍后重试。")
        else:
            raise ctypes.WinError(ctypes.get_last_error())
        yield
    finally:
        try:
            if acquired:
                kernel.ReleaseMutex(handle)
        finally:
            kernel.CloseHandle(handle)
