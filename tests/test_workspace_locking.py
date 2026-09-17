from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from retext.archive import PacWorkspaceError
from retext.archive.lock_guard import workspace_lock_guard
from retext.archive.workspace import _acquire_workspace_lock, _release_workspace_lock


def _lock_contender(root, start, release, results):
    start.wait(15)
    token = str(os.getpid())
    try:
        lock = _acquire_workspace_lock(Path(root), token)
    except PacWorkspaceError as exc:
        results.put(("blocked", str(exc)))
        return
    results.put(("acquired", token))
    release.wait(20)
    _release_workspace_lock(lock, token)


def _exit_with_lock(root):
    _acquire_workspace_lock(Path(root), "abandoned")
    # No explicit lock release: model a crashed application, not a clean exit.
    os._exit(0)


class WorkspaceLockTests(unittest.TestCase):
    def test_other_owner_is_never_overwritten_or_released(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = _acquire_workspace_lock(root, "owner")
            try:
                before = lock.read_bytes()
                with self.assertRaises(PacWorkspaceError):
                    _acquire_workspace_lock(root, "contender")
                _release_workspace_lock(lock, "wrong-owner")
                self.assertEqual(lock.read_bytes(), before)
                self.assertEqual(list(root.glob("*.tmp")), [])
            finally:
                _release_workspace_lock(lock, "owner")
            self.assertFalse(lock.exists())

    @unittest.skipUnless(os.name == "nt", "Windows publication contract")
    def test_windows_lock_does_not_call_hardlink_even_if_unsupported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "BaiduNetdiskDownload" / "中文缓存"
            rejected = OSError(0, "函数不正确。", "prepared", 1, "lock.json")
            with patch("os.link", side_effect=rejected) as link:
                lock = _acquire_workspace_lock(root, "owner")
                self.assertEqual(json.loads(lock.read_text())["token"], "owner")
                _release_workspace_lock(lock, "owner")
            link.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows publication contract")
    def test_published_lock_is_complete_and_rename_is_non_overwriting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original_rename = os.rename

            def inspect_publish(source, target):
                self.assertFalse(Path(target).exists())
                self.assertEqual(json.loads(Path(source).read_text())["token"], "owner")
                self.assertLessEqual(len(Path(source).name), 16)
                return original_rename(source, target)

            with patch("os.rename", side_effect=inspect_publish) as publish:
                lock = _acquire_workspace_lock(root, "owner")
            publish.assert_called_once()
            _release_workspace_lock(lock, "owner")

    def test_unknown_or_corrupt_lock_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / "lock.json"
            for contents in (b"", b"{", b'{"pid": "unknown"}'):
                lock.write_bytes(contents)
                with self.assertRaisesRegex(PacWorkspaceError, "无法确认.*归属"):
                    _acquire_workspace_lock(root, "contender")
                self.assertEqual(lock.read_bytes(), contents)
                self.assertEqual(list(root.glob("*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows path budget")
    def test_short_lock_name_fits_former_269_character_path(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / ("中" * (225 - len(str(base))))
            self.assertEqual(len(str(root / (".lock-" + "a" * 32 + ".tmp"))), 269)
            lock = _acquire_workspace_lock(root, "owner")
            _release_workspace_lock(lock, "owner")
            self.assertEqual(list(root.iterdir()), [])

    def test_write_failure_does_not_publish_or_leave_temporary_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("os.fsync", side_effect=OSError("disk write failed")):
                with self.assertRaisesRegex(PacWorkspaceError, "缓存工作区") as raised:
                    _acquire_workspace_lock(root, "owner")
            self.assertIn(str(root), str(raised.exception))
            self.assertIn("TIS_RETEXT_DATA_DIR", str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertEqual(list(root.iterdir()), [])

    def test_unwritable_directory_error_identifies_cache_not_pac(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("tempfile.mkstemp", side_effect=PermissionError("access denied")):
                with self.assertRaisesRegex(PacWorkspaceError, "并非 PAC 内容解析错误"):
                    _acquire_workspace_lock(root, "owner")
            self.assertEqual(list(root.iterdir()), [])

    def test_cleanup_does_not_recreate_missing_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "missing"
            with self.assertRaisesRegex(PacWorkspaceError, "已被其他操作移走"):
                _acquire_workspace_lock(root, "owner", create_root=False)
            self.assertFalse(root.exists())

    def test_crashed_process_lock_can_be_reclaimed(self):
        with tempfile.TemporaryDirectory() as temp:
            context = multiprocessing.get_context("spawn")
            process = context.Process(target=_exit_with_lock, args=(temp,))
            process.start()
            try:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
                root = Path(temp)
                self.assertEqual(json.loads((root / "lock.json").read_text())["pid"], process.pid)
                lock = _acquire_workspace_lock(root, "new-owner")
                self.assertEqual(json.loads(lock.read_text())["token"], "new-owner")
                _release_workspace_lock(lock, "new-owner")
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                process.close()

    @unittest.skipUnless(os.name == "nt", "Windows process synchronization")
    def test_simultaneous_claims_have_exactly_one_owner(self):
        self._race(stale=False)

    @unittest.skipUnless(os.name == "nt", "Windows process synchronization")
    def test_simultaneous_stale_reclamation_has_exactly_one_owner(self):
        self._race(stale=True)

    def _race(self, *, stale):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            if stale:
                (root / "lock.json").write_text(json.dumps({"pid": 0x3FFFFFFF, "token": "old"}))
            context = multiprocessing.get_context("spawn")
            start, release = context.Event(), context.Event()
            results = context.Queue()
            children = [context.Process(target=_lock_contender, args=(temp, start, release, results))
                        for _ in range(4)]
            for child in children:
                child.start()
            try:
                start.set()
                records = [results.get(timeout=20) for _ in children]
                owners = [token for status, token in records if status == "acquired"]
                self.assertEqual(len(owners), 1, records)
                self.assertEqual(json.loads((root / "lock.json").read_text())["token"], owners[0])
            finally:
                release.set()
                for child in children:
                    child.join(10)
                    if child.is_alive():
                        child.terminate()
                        child.join(5)
                    child.close()
                results.close()
                results.join_thread()
            self.assertFalse((root / "lock.json").exists())
            self.assertEqual(list(root.glob("*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows process synchronization")
    def test_guard_is_released_after_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                with workspace_lock_guard(root):
                    raise ValueError("interrupted")
            # A different thread must not inherit ownership or time out.
            from concurrent.futures import ThreadPoolExecutor

            def claim():
                lock = _acquire_workspace_lock(root, "owner")
                _release_workspace_lock(lock, "owner")

            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(claim).result(timeout=5)


if __name__ == "__main__":
    unittest.main()
