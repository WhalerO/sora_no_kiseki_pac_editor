from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from retext.vlc_runtime import (
    VLC_RUNTIME_ENV,
    VlcRuntime,
    configure_vlc_runtime,
    find_vlc_runtime,
)


class VlcRuntimeTests(unittest.TestCase):
    def _make_runtime(self, root: Path) -> VlcRuntime:
        root.mkdir(parents=True)
        (root / "libvlc.dll").write_bytes(b"test")
        (root / "libvlccore.dll").write_bytes(b"test")
        plugins = root / "plugins"
        plugins.mkdir()
        (plugins / "test_plugin.dll").write_bytes(b"test")
        return VlcRuntime(
            root=root,
            libvlc=root / "libvlc.dll",
            plugins=plugins,
            source="test",
        )

    def test_finder_uses_first_complete_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            incomplete = base / "incomplete"
            incomplete.mkdir()
            complete = self._make_runtime(base / "complete")
            candidates = iter(
                ((incomplete, "bad"), (complete.root, complete.source))
            )

            with patch("retext.vlc_runtime._runtime_candidates", return_value=candidates):
                found = find_vlc_runtime()

            self.assertEqual(found, complete)

    def test_configure_sets_plugin_and_dll_search_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._make_runtime(Path(temporary) / "vlc")
            add_dll_directory = Mock(return_value=object())
            clean_path = os.pathsep.join(("C:\\first", "C:\\second"))

            with (
                patch(
                    "retext.vlc_runtime.find_vlc_runtime",
                    return_value=runtime,
                ),
                patch(
                    "retext.vlc_runtime.os.add_dll_directory",
                    add_dll_directory,
                    create=True,
                ),
                patch.dict(
                    os.environ,
                    {"PATH": clean_path, VLC_RUNTIME_ENV: str(runtime.root)},
                    clear=True,
                ),
            ):
                configured = configure_vlc_runtime()
                self.assertEqual(configured, runtime)
                self.assertEqual(
                    os.environ["PYTHON_VLC_LIB_PATH"],
                    str(runtime.libvlc),
                )
                self.assertEqual(
                    os.environ["PYTHON_VLC_MODULE_PATH"],
                    str(runtime.plugins),
                )
                self.assertEqual(os.environ["VLC_PLUGIN_PATH"], str(runtime.plugins))
                self.assertEqual(
                    os.environ["PATH"].split(os.pathsep)[0],
                    str(runtime.root),
                )
                add_dll_directory.assert_called_once_with(str(runtime.root))

    def test_configure_without_runtime_is_a_noop(self) -> None:
        with patch("retext.vlc_runtime.find_vlc_runtime", return_value=None):
            self.assertIsNone(configure_vlc_runtime())


if __name__ == "__main__":
    unittest.main()
