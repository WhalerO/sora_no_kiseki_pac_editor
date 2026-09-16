$ErrorActionPreference = "Stop"

Write-Host "=== Creating virtual environment ==="

if (!(Test-Path ".venv")) {
    py -3.14 -m venv .venv
}

$Python = ".\.venv\Scripts\python.exe"

Write-Host "=== Installing dependencies ==="

& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements-release.txt


Write-Host "=== Building executable ==="

powershell `
    -ExecutionPolicy Bypass `
    -File scripts\build_exe.ps1 `
    -PythonExecutable $Python


Write-Host "=== Build finished ==="

pause
