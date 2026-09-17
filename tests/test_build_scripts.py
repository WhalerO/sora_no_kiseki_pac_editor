from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from retext.paths import REPO_ROOT


SHELLS = [path for name in ("powershell.exe", "pwsh.exe") if (path := shutil.which(name))]


def _quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _run(shell, code, cwd, env=None):
    encoded = base64.b64encode(code.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", timeout=30,
    )


@unittest.skipUnless(os.name == "nt" and SHELLS, "Windows PowerShell build entry")
class BuildScriptTests(unittest.TestCase):
    def _project(self, root, child):
        project = root / "中文 构建项目"
        (project / "scripts").mkdir(parents=True)
        shutil.copyfile(REPO_ROOT / "build.ps1", project / "build.ps1")
        (project / "scripts/build_exe.ps1").write_text(
            'param([string]$PythonExecutable)\n' + child, encoding="utf-8-sig",
        )
        python = project / "test-python.cmd"
        python.write_text("@echo off\nexit /b 0\n", encoding="ascii")
        return project, python

    def test_entry_keeps_shell_edition_resolves_project_and_restores_cwd(self):
        for shell in SHELLS:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                project, python = self._project(root, """
[pscustomobject]@{
    edition = $PSEdition
    cwd = (Get-Location).Path
    python = $PythonExecutable
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot '..\\child.json') -Encoding UTF8
""")
                call = (f"& {_quote(project / 'build.ps1')} -PythonExecutable {_quote(python)} "
                        "-SkipDependencyInstall")
                result = _run(shell, "$ErrorActionPreference = 'Stop'\n" + call + "\n"
                              + "[pscustomobject]@{edition=$PSEdition; cwd=(Get-Location).Path} "
                              + f"| ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath {_quote(root / 'caller.json')}", root)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn("=== Build finished ===", result.stdout)
                child = json.loads((project / "child.json").read_text(encoding="utf-8-sig"))
                caller = json.loads((root / "caller.json").read_text(encoding="utf-8-sig"))
                self.assertEqual(child["edition"], caller["edition"])
                self.assertEqual(Path(child["cwd"]), project)
                self.assertEqual(Path(caller["cwd"]), root)
                self.assertEqual(Path(child["python"]), python)

    def test_child_failure_never_prints_build_finished(self):
        for shell in SHELLS:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                project, python = self._project(root, "exit 23\n")
                result = _run(shell, f"& {_quote(project / 'build.ps1')} "
                              f"-PythonExecutable {_quote(python)} -SkipDependencyInstall", root)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("Executable build failed with exit code 23", result.stdout)
                self.assertNotIn("=== Build finished ===", result.stdout)

    def test_native_unicode_output_and_callers_encoding_settings_are_preserved(self):
        for shell in SHELLS:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                project, _python = self._project(root, """
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $OutputEncoding
& $PythonExecutable -c "print('中文构建输出')"
if ($LASTEXITCODE -ne 0) { throw 'Native print failed' }
""")
                result_file = root / "after.json"
                code = (f"& {_quote(project / 'build.ps1')} -PythonExecutable {_quote(sys.executable)} "
                        "-SkipDependencyInstall\n"
                        + "[pscustomobject]@{io=$env:PYTHONIOENCODING; utf8=$env:PYTHONUTF8} "
                        + f"| ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath {_quote(result_file)}")
                env = {**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}
                result = _run(shell, code, root, env=env)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn("中文构建输出", result.stdout)
                self.assertEqual(json.loads(result_file.read_text(encoding="utf-8-sig")), {"io": "ascii", "utf8": "0"})

    def test_each_dependency_failure_stops_before_build(self):
        for shell in SHELLS:
            for stage, body in (
                ("pip upgrade", "@echo off\nexit /b 7\n"),
                ("Dependency installation", '@echo off\nif "%~4"=="-r" exit /b 8\nexit /b 0\n'),
            ):
                with self.subTest(shell=shell, stage=stage), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp).resolve()
                    project, python = self._project(root, "throw 'Build must not be started'\n")
                    python.write_text(body, encoding="ascii")
                    result = _run(shell, f"& {_quote(project / 'build.ps1')} "
                                  f"-PythonExecutable {_quote(python)}", root)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(stage + " failed with exit code", result.stdout)
                    self.assertNotIn("=== Building executable ===", result.stdout)
                    self.assertNotIn("=== Build finished ===", result.stdout)

    def test_venv_failure_stops_before_dependency_install(self):
        for shell in SHELLS:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                project, _python = self._project(root, "throw 'Build must not be started'\n")
                launchers = root / "launchers"
                launchers.mkdir()
                (launchers / "py.cmd").write_text("@echo off\nexit /b 9\n", encoding="ascii")
                env = {**os.environ, "PATH": str(launchers) + os.pathsep + os.environ.get("PATH", "")}
                result = _run(shell, f"& {_quote(project / 'build.ps1')}", root, env=env)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("Virtual environment creation failed with exit code 9", result.stdout)
                self.assertNotIn("=== Installing dependencies ===", result.stdout)
                self.assertNotIn("=== Build finished ===", result.stdout)

    def test_production_probe_reader_preserves_utf8_chinese_paths_in_both_shells(self):
        script = (REPO_ROOT / "scripts/build_exe.ps1").read_text(encoding="utf-8-sig")
        assignment = next(line for line in script.splitlines() if line.startswith("$probeResult = "))
        expected = {"ok": True, "data_root": r"F:\空之轨迹pac分析\开发项目\TIS_Retext\build", "dpi_awareness": 2}
        for shell in SHELLS:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                source, output = root / "probe.json", root / "read-back.json"
                source.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
                code = ("$ErrorActionPreference = 'Stop'\n"
                        + f"$probeOutput = {_quote(source)}\n" + assignment + "\n"
                        + f"$probeResult | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath {_quote(output)}")
                result = _run(shell, code, root)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(json.loads(output.read_text(encoding="utf-8-sig")), expected)


if __name__ == "__main__":
    unittest.main()
