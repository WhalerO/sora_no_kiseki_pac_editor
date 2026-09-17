[CmdletBinding()]
param(
    [string]$PythonExecutable,
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$PreviousConsoleEncoding = [Console]::OutputEncoding
$PreviousOutputEncoding = $OutputEncoding
$PreviousPythonIoEncoding = $env:PYTHONIOENCODING
$PreviousPythonUtf8 = $env:PYTHONUTF8

Push-Location -LiteralPath $ProjectRoot
try {
    # Native Python output and the receiving PowerShell must use the same
    # encoding, including when logs are redirected instead of shown on screen.
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    [Console]::OutputEncoding = $OutputEncoding
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
    if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
        $Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
            Write-Host "=== Creating virtual environment ==="
            & py -3.14 -m venv (Join-Path $ProjectRoot ".venv")
            if ($LASTEXITCODE -ne 0) {
                throw "Virtual environment creation failed with exit code $LASTEXITCODE."
            }
        }
    }
    elseif (Test-Path -LiteralPath $PythonExecutable -PathType Leaf) {
        $Python = (Resolve-Path -LiteralPath $PythonExecutable).Path
    }
    else {
        $Python = (Get-Command -Name $PythonExecutable -CommandType Application -ErrorAction Stop).Source
    }

    if (-not $SkipDependencyInstall) {
        Write-Host "=== Installing dependencies ==="
        & $Python -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) {
            throw "pip upgrade failed with exit code $LASTEXITCODE."
        }
        & $Python -m pip install -r (Join-Path $ProjectRoot "requirements-release.txt")
        if ($LASTEXITCODE -ne 0) {
            throw "Dependency installation failed with exit code $LASTEXITCODE."
        }
    }

    Write-Host "=== Building executable ==="
    # Keep the caller's PowerShell edition, without profiles or leaking build
    # environment variables back into the interactive terminal.
    $BuildShellName = if ($PSEdition -eq "Core") { "pwsh.exe" } else { "powershell.exe" }
    $BuildShell = Join-Path $PSHOME $BuildShellName
    & $BuildShell -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $ProjectRoot "scripts\build_exe.ps1") `
        -PythonExecutable $Python
    if ($LASTEXITCODE -ne 0) {
        throw "Executable build failed with exit code $LASTEXITCODE."
    }

    Write-Host "=== Build finished ==="
}
finally {
    $env:PYTHONIOENCODING = $PreviousPythonIoEncoding
    $env:PYTHONUTF8 = $PreviousPythonUtf8
    $OutputEncoding = $PreviousOutputEncoding
    [Console]::OutputEncoding = $PreviousConsoleEncoding
    Pop-Location
}
