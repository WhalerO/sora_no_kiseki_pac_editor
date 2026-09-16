import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from retext import dpi


class DpiTests(unittest.TestCase):
    def test_failed_modern_api_uses_per_monitor_fallback(self):
        user32 = SimpleNamespace(SetProcessDpiAwarenessContext=Mock(return_value=0))
        shcore = SimpleNamespace(SetProcessDpiAwareness=Mock(return_value=0))
        with patch.object(dpi.sys, "platform", "win32"), \
             patch.object(dpi.ctypes, "WinDLL", side_effect=[user32, shcore]), \
             patch.object(dpi.ctypes, "get_last_error", return_value=87):
            dpi.configure_dpi_awareness()
        shcore.SetProcessDpiAwareness.assert_called_once_with(2)

    def test_window_dpi_change_preserves_user_scale(self):
        from tk_gui import RetextTkApp
        app = SimpleNamespace(_dpi_refresh_pending="pending", root=object(),
                              _native_tk_scaling=96/72,
                              ui_scale_var=SimpleNamespace(get=lambda: "85%"),
                              _parse_scale=RetextTkApp._parse_scale,
                              _apply_ui_scale=Mock())
        with patch("tk_gui.window_tk_scaling", return_value=144/72):
            RetextTkApp._refresh_window_dpi(app)
            RetextTkApp._refresh_window_dpi(app)
        self.assertEqual(app._native_tk_scaling, 2.0)
        app._apply_ui_scale.assert_called_once_with(0.85)

    def test_packaging_declares_per_monitor_dpi_before_python_starts(self):
        root = Path(__file__).resolve().parents[1]
        manifest = ET.parse(root / "scripts/windows_app.manifest")
        setting = manifest.find(".//{http://schemas.microsoft.com/SMI/2016/WindowsSettings}dpiAwareness")
        self.assertEqual(setting.text, "PerMonitorV2, PerMonitor")
        build = (root / "scripts/build_exe.ps1").read_text(encoding="utf-8")
        self.assertIn('"--manifest", (Join-Path $ScriptDir "windows_app.manifest")', build)
