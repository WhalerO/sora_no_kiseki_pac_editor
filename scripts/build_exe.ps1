param(
    [switch]$OneFile,
    [switch]$Release,
    [string]$VlcRuntimeDir,
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackageRoot = Split-Path -Parent $ScriptDir
$BuildTempRoot = Join-Path $PackageRoot ".runtime\build-temp"
$PyInstallerConfigRoot = Join-Path $PackageRoot ".runtime\pyinstaller"
New-Item -ItemType Directory -Path $BuildTempRoot -Force | Out-Null
New-Item -ItemType Directory -Path $PyInstallerConfigRoot -Force | Out-Null
$env:TEMP = $BuildTempRoot
$env:TMP = $BuildTempRoot
$env:PYINSTALLER_CONFIG_DIR = $PyInstallerConfigRoot
$env:PYTHONPATH = $PackageRoot
$env:PYTHONDONTWRITEBYTECODE = "1"
$VendorRoot = Join-Path $PackageRoot "_vendor"
$LaunchScript = Join-Path $PackageRoot "scripts\launch_gui.py"
$KuroToolsDir = Join-Path $PackageRoot "retext\engines\kuro"
$LegacyDir = Join-Path $PackageRoot "retext\engines\legacy"
$PacToolsDir = Join-Path $PackageRoot "pac_tools"
$HookDir = Join-Path $ScriptDir "pyinstaller_hooks"
$NumpyRuntimeHook = Join-Path $HookDir "pyi_rth_tis_numpy.py"
$VersionFile = Join-Path $PackageRoot "build\release-metadata\version_info.txt"
$ThirdPartyNotice = Join-Path $PackageRoot "THIRD_PARTY_NOTICES.md"
$LicensesDir = Join-Path $PackageRoot "licenses"
$ProjectLicense = Join-Path $PackageRoot "LICENSE"
$CollectedLicensesDir = Join-Path $PackageRoot "build\third-party-licenses"

$mode = if ($OneFile) { "--onefile" } else { "--onedir" }

if (-not [Environment]::Is64BitProcess) {
    throw "The Windows release build requires a 64-bit Python interpreter and 64-bit LibVLC."
}
if ($Release -and $OneFile) {
    throw "Strict release builds support onedir only; -OneFile is an experimental development mode."
}

$pythonVersion = (& $PythonExecutable -c "import platform; print(platform.python_version())").Trim()
if (($LASTEXITCODE -ne 0) -or [string]::IsNullOrWhiteSpace($pythonVersion)) {
    throw "Unable to run the selected Python executable: $PythonExecutable"
}
if ($Release -and -not $pythonVersion.StartsWith("3.11.")) {
    throw "Release builds require CPython 3.11.x; selected interpreter is $pythonVersion."
}
if ($Release -and -not (Test-Path -LiteralPath (Join-Path $PackageRoot "LICENSE") -PathType Leaf)) {
    throw "Release build is blocked: the project owner must choose and add the root LICENSE first."
}

& $PythonExecutable (Join-Path $ScriptDir "write_version_info.py") $VersionFile
if ($LASTEXITCODE -ne 0) {
    throw "Unable to generate Windows version metadata."
}

function Test-X64PortableExecutable {
    param([string]$Path)

    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
        if (($bytes.Length -lt 64) -or ($bytes[0] -ne 0x4D) -or ($bytes[1] -ne 0x5A)) {
            return $false
        }
        $peOffset = [System.BitConverter]::ToInt32($bytes, 0x3C)
        if (($peOffset -lt 0) -or (($peOffset + 6) -gt $bytes.Length)) {
            return $false
        }
        if (
            ($bytes[$peOffset] -ne 0x50) -or
            ($bytes[$peOffset + 1] -ne 0x45) -or
            ($bytes[$peOffset + 2] -ne 0x00) -or
            ($bytes[$peOffset + 3] -ne 0x00)
        ) {
            return $false
        }
        # IMAGE_FILE_MACHINE_AMD64
        return [System.BitConverter]::ToUInt16($bytes, $peOffset + 4) -eq 0x8664
    }
    catch {
        return $false
    }
}

function Test-VlcRuntime {
    param([string]$Candidate)

    if ([string]::IsNullOrWhiteSpace($Candidate)) {
        return $false
    }

    try {
        if (-not (Test-Path -LiteralPath $Candidate -PathType Container)) {
            return $false
        }
        $libvlcPath = Join-Path $Candidate "libvlc.dll"
        $corePath = Join-Path $Candidate "libvlccore.dll"
        $licensePath = Join-Path $Candidate "COPYING.txt"
        return (
            (Test-Path -LiteralPath $libvlcPath -PathType Leaf) -and
            (Test-Path -LiteralPath $corePath -PathType Leaf) -and
            (Test-Path -LiteralPath $licensePath -PathType Leaf) -and
            (Test-X64PortableExecutable $libvlcPath) -and
            (Test-X64PortableExecutable $corePath) -and
            (Test-Path -LiteralPath (Join-Path $Candidate "plugins") -PathType Container) -and
            ($null -ne (Get-ChildItem -LiteralPath (Join-Path $Candidate "plugins") -Filter "*.dll" -File -Recurse | Select-Object -First 1))
        )
    }
    catch {
        return $false
    }
}

function Get-VlcPayloadFiles {
    param([string]$RuntimeRoot)

    @(
        Get-Item -LiteralPath (Join-Path $RuntimeRoot "libvlc.dll")
        Get-Item -LiteralPath (Join-Path $RuntimeRoot "libvlccore.dll")
        Get-Item -LiteralPath (Join-Path $RuntimeRoot "COPYING.txt")
        Get-ChildItem -LiteralPath (Join-Path $RuntimeRoot "plugins") -File -Recurse -Force
    )
}

function Assert-VlcRuntimeManifest {
    param(
        [string]$RuntimeRoot,
        [object]$Manifest
    )

    $records = @($Manifest.files)
    if ($records.Count -eq 0) {
        throw "Pinned VLC runtime manifest has no per-file integrity records."
    }
    $expected = @{}
    foreach ($record in $records) {
        $relativePath = [string]$record.path
        $normalized = $relativePath.Replace("\", "/")
        if (
            [string]::IsNullOrWhiteSpace($normalized) -or
            $normalized.StartsWith("/") -or
            $normalized.Contains("../") -or
            $expected.ContainsKey($normalized)
        ) {
            throw "Pinned VLC runtime manifest contains an invalid path: $relativePath"
        }
        $expected[$normalized] = $record
    }

    $runtimePrefixLength = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ).Length + 1
    $actualFiles = @(Get-VlcPayloadFiles $RuntimeRoot)
    if ($actualFiles.Count -ne $expected.Count) {
        throw "Pinned VLC runtime file count differs from its integrity manifest."
    }
    foreach ($actualFile in $actualFiles) {
        $relativePath = $actualFile.FullName.Substring(
            $runtimePrefixLength
        ).Replace("\", "/")
        if (-not $expected.ContainsKey($relativePath)) {
            throw "Pinned VLC runtime contains an unrecorded payload file: $relativePath"
        }
        $record = $expected[$relativePath]
        if ([long]$record.size -ne $actualFile.Length) {
            throw "Pinned VLC runtime file size mismatch: $relativePath"
        }
        $actualHash = (
            Get-FileHash -LiteralPath $actualFile.FullName -Algorithm SHA256
        ).Hash
        if (-not $actualHash.Equals(
            [string]$record.sha256,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Pinned VLC runtime SHA-256 mismatch: $relativePath"
        }
    }
}

function Resolve-VlcRuntime {
    $candidates = [System.Collections.Generic.List[object]]::new()

    if (-not [string]::IsNullOrWhiteSpace($VlcRuntimeDir)) {
        $candidates.Add([PSCustomObject]@{ Source = "-VlcRuntimeDir"; Path = $VlcRuntimeDir })
    }
    if (-not [string]::IsNullOrWhiteSpace($env:TIS_RETEXT_VLC_DIR)) {
        $candidates.Add([PSCustomObject]@{ Source = "TIS_RETEXT_VLC_DIR"; Path = $env:TIS_RETEXT_VLC_DIR })
    }

    $candidates.Add([PSCustomObject]@{
        Source = "vendored runtime"
        Path = (Join-Path $VendorRoot "libvlc\win-x64")
    })

    foreach ($registryPath in @(
        "HKLM:\SOFTWARE\VideoLAN\VLC",
        "HKLM:\SOFTWARE\WOW6432Node\VideoLAN\VLC",
        "HKCU:\SOFTWARE\VideoLAN\VLC"
    )) {
        if (Test-Path -LiteralPath $registryPath) {
            $installation = Get-ItemProperty -LiteralPath $registryPath
            if (-not [string]::IsNullOrWhiteSpace($installation.InstallDir)) {
                $candidates.Add([PSCustomObject]@{
                    Source = "installed VLC ($registryPath)"
                    Path = $installation.InstallDir
                })
            }
        }
    }

    foreach ($programRoot in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not [string]::IsNullOrWhiteSpace($programRoot)) {
            $candidates.Add([PSCustomObject]@{
                Source = "installed VLC"
                Path = (Join-Path $programRoot "VideoLAN\VLC")
            })
        }
    }

    $seen = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($candidate in $candidates) {
        $fullPath = [System.IO.Path]::GetFullPath($candidate.Path)
        if (-not $seen.Add($fullPath)) {
            continue
        }
        if (Test-VlcRuntime $fullPath) {
            return [PSCustomObject]@{
                Source = $candidate.Source
                Path = $fullPath
            }
        }
        if (($candidate.Source -eq "-VlcRuntimeDir") -or ($candidate.Source -eq "TIS_RETEXT_VLC_DIR")) {
            throw "Invalid LibVLC runtime from $($candidate.Source): $fullPath`nExpected x64 libvlc.dll/libvlccore.dll, a non-empty plugins directory, and COPYING.txt."
        }
    }

    throw @"
No usable 64-bit LibVLC runtime was found.

For a reproducible build, extract the official VLC win64 archive so that:
  $VendorRoot\libvlc\win-x64\libvlc.dll
  $VendorRoot\libvlc\win-x64\libvlccore.dll
  $VendorRoot\libvlc\win-x64\plugins\...
  $VendorRoot\libvlc\win-x64\COPYING.txt

Run scripts\prepare_vlc_runtime.ps1 to download and verify the pinned runtime.
Alternatively pass -VlcRuntimeDir <VLC directory>, set TIS_RETEXT_VLC_DIR,
or install 64-bit VLC. Runtime binaries are intentionally not stored in Git.
"@
}

if (-not (Test-Path $KuroToolsDir)) {
    throw "Missing resource directory: $KuroToolsDir"
}

if (-not (Test-Path $LegacyDir)) {
    throw "Missing resource directory: $LegacyDir"
}

$vlcRuntime = Resolve-VlcRuntime
$vendoredVlcRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $VendorRoot "libvlc\win-x64")
)
$runtimeManifestPath = Join-Path $vendoredVlcRoot ".tis-retext-runtime.json"
$runtimeManifest = $null
if (Test-Path -LiteralPath $runtimeManifestPath -PathType Leaf) {
    $runtimeManifest = Get-Content -Raw -LiteralPath $runtimeManifestPath |
        ConvertFrom-Json
}
if ($Release) {
    if (-not [System.IO.Path]::GetFullPath($vlcRuntime.Path).Equals(
        $vendoredVlcRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Release builds require the pinned vendored VLC runtime prepared by prepare_vlc_runtime.ps1."
    }
    if ($null -eq $runtimeManifest) {
        throw "Pinned VLC runtime manifest is missing; rerun scripts\prepare_vlc_runtime.ps1."
    }
    if (
        ($runtimeManifest.version -ne "3.0.23") -or
        ($runtimeManifest.archive_sha256 -ne "992D19DBD0B8A7CDE9167D2F7780B1EF6F92ACC8A71ACFA736101A21F35181E1")
    ) {
        throw "Pinned VLC runtime manifest does not match the approved release input."
    }
    Assert-VlcRuntimeManifest $vendoredVlcRoot $runtimeManifest
}
$vlcVersion = (Get-Item -LiteralPath (Join-Path $vlcRuntime.Path "libvlc.dll")).VersionInfo.FileVersion
Write-Host "[build] LibVLC source: $($vlcRuntime.Source)"
Write-Host "[build] LibVLC path:   $($vlcRuntime.Path)"
Write-Host "[build] LibVLC version: $vlcVersion"

$pythonPrefix = (& $PythonExecutable -c "import sys; print(sys.prefix)").Trim()
if (($LASTEXITCODE -ne 0) -or [string]::IsNullOrWhiteSpace($pythonPrefix)) {
    throw "Unable to resolve the Python environment used for packaging."
}
$condaMetadata = Join-Path $pythonPrefix "conda-meta"
if (Test-Path -LiteralPath $condaMetadata -PathType Container) {
    if ($Release) {
        throw "Release builds reject Conda environments because PyInstaller can collect the full MKL/BLAS runtime. Use a clean CPython 3.11 venv on the project drive."
    }
    Write-Warning "Conda build detected. The development package may include a large MKL/BLAS runtime; use a clean CPython 3.11 venv for size measurements."
}
$pythonRuntimeBin = Join-Path $pythonPrefix "Library\bin"
$pythonRuntimeDlls = [System.Collections.Generic.List[string]]::new()
if (Test-Path -LiteralPath $pythonRuntimeBin -PathType Container) {
    foreach ($runtimeDllName in @(
        "ffi.dll",
        "libcrypto-3-x64.dll",
        "libexpat.dll",
        "liblzma.dll",
        "libssl-3-x64.dll",
        "tcl86t.dll",
        "tk86t.dll"
    )) {
        $runtimeDllPath = Join-Path $pythonRuntimeBin $runtimeDllName
        if (Test-Path -LiteralPath $runtimeDllPath -PathType Leaf) {
            $pythonRuntimeDlls.Add($runtimeDllPath)
        }
    }
    # Let PyInstaller resolve transitive dependencies against the same
    # environment instead of accidentally finding another Python on PATH.
    $env:Path = "$pythonRuntimeBin;$env:Path"
}

if (Test-Path -LiteralPath $CollectedLicensesDir) {
    Remove-Item -LiteralPath $CollectedLicensesDir -Recurse -Force
}
$licenseCollectionArgs = @(
    (Join-Path $ScriptDir "collect_licenses.py"),
    $CollectedLicensesDir
)
if ($Release) {
    $licenseCollectionArgs += "--strict"
}
& $PythonExecutable @licenseCollectionArgs
if ($LASTEXITCODE -ne 0) {
    throw "Unable to collect third-party license files."
}

$pyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    $mode,
    "--noconsole",
    "--name", "TIS_Retext",
    "--distpath", (Join-Path $PackageRoot "dist"),
    "--workpath", (Join-Path $PackageRoot "build"),
    "--specpath", $PackageRoot,
    "--paths", $PackageRoot,
    "--additional-hooks-dir", $HookDir,
    "--runtime-hook", $NumpyRuntimeHook,
    "--version-file", $VersionFile,
    "--manifest", (Join-Path $ScriptDir "windows_app.manifest"),
    "--add-data", "$(Join-Path $KuroToolsDir 'schemas');retext\engines\kuro\schemas",
    "--add-data", "$(Join-Path $KuroToolsDir 'LICENSE.md');retext\engines\kuro",
    "--add-data", "$(Join-Path $KuroToolsDir 'README.md');retext\engines\kuro",
    "--add-data", "$(Join-Path $KuroToolsDir 'UPSTREAM.md');retext\engines\kuro",
    "--collect-submodules", "retext.engines.kuro.disasm",
    "--collect-submodules", "retext.engines.kuro.lib",
    # The application has no standalone FFmpeg dependency. Exclude a copy
    # that may happen to be installed in the build environment so it cannot
    # leak into the package.
    "--exclude-module", "imageio_ffmpeg",
    "--collect-all", "moderngl",
    "--collect-all", "glcontext",
    "--hidden-import", "vlc",
    "--add-binary", "$(Join-Path $vlcRuntime.Path 'libvlc.dll');libvlc",
    "--add-binary", "$(Join-Path $vlcRuntime.Path 'libvlccore.dll');libvlc",
    "--add-data", "$(Join-Path $vlcRuntime.Path 'COPYING.txt');libvlc"
)

# The application embeds LibVLC into its own Tk controls and never loads the
# Qt/skins UI plugins. Keep every playback/plugin category for compatibility,
# but omit the standalone VLC GUI payload (about 19 MiB).
$vlcPluginsRoot = Join-Path $vlcRuntime.Path "plugins"
foreach ($pluginFile in Get-ChildItem -LiteralPath $vlcPluginsRoot -File) {
    $pyInstallerArgs += @(
        "--add-binary",
        "$($pluginFile.FullName);libvlc\plugins"
    )
}
foreach ($pluginDirectory in Get-ChildItem -LiteralPath $vlcPluginsRoot -Directory) {
    if ($pluginDirectory.Name -eq "gui") {
        continue
    }
    $pyInstallerArgs += @(
        "--add-binary",
        "$($pluginDirectory.FullName);libvlc\plugins\$($pluginDirectory.Name)"
    )
}

if (Test-Path -LiteralPath $ThirdPartyNotice -PathType Leaf) {
    $pyInstallerArgs += @("--add-data", "${ThirdPartyNotice};.")
}
if (Test-Path -LiteralPath $ProjectLicense -PathType Leaf) {
    $pyInstallerArgs += @("--add-data", "${ProjectLicense};.")
}
if (Test-Path -LiteralPath $LicensesDir -PathType Container) {
    $pyInstallerArgs += @("--add-data", "${LicensesDir};licenses")
}
if (Test-Path -LiteralPath $CollectedLicensesDir -PathType Container) {
    $pyInstallerArgs += @(
        "--add-data",
        "${CollectedLicensesDir};licenses\python-packages"
    )
}

foreach ($runtimeDllPath in $pythonRuntimeDlls) {
    $pyInstallerArgs += @("--add-binary", "${runtimeDllPath};.")
}

if (Test-Path $PacToolsDir) {
    foreach ($fallbackFile in @(
        "extract_pac.py",
        "create_pac.py",
        "UPSTREAM.md",
        "LICENSE"
    )) {
        $fallbackPath = Join-Path $PacToolsDir $fallbackFile
        if (Test-Path -LiteralPath $fallbackPath -PathType Leaf) {
            $pyInstallerArgs += @(
                "--add-data",
                "${fallbackPath};pac_tools"
            )
        }
    }
}

$pyInstallerArgs += $LaunchScript

foreach ($pyInstallerOutputRoot in @(
    (Join-Path $PackageRoot "build\TIS_Retext"),
    (Join-Path $PackageRoot "dist\TIS_Retext")
)) {
    if (-not (Test-Path -LiteralPath $pyInstallerOutputRoot)) {
        continue
    }
    Get-ChildItem -LiteralPath $pyInstallerOutputRoot -Recurse -Force |
        ForEach-Object {
            $_.Attributes = $_.Attributes -band (
                -bnot [System.IO.FileAttributes]::ReadOnly
            )
        }
    $outputRootItem = Get-Item -LiteralPath $pyInstallerOutputRoot -Force
    $outputRootItem.Attributes = $outputRootItem.Attributes -band (
        -bnot [System.IO.FileAttributes]::ReadOnly
    )
    $resolvedOutput = [System.IO.Path]::GetFullPath($pyInstallerOutputRoot)
    $allowedParent = [System.IO.Path]::GetFullPath($PackageRoot).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedOutput.StartsWith(
        $allowedParent,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to clean a build directory outside the project: $resolvedOutput"
    }
    Remove-Item -LiteralPath $resolvedOutput -Recurse -Force
}

if ($Release) {
    & $PythonExecutable (Join-Path $ScriptDir "release_check.py") --strict
    if ($LASTEXITCODE -ne 0) {
        throw "Release checks failed with exit code $LASTEXITCODE."
    }
}

& $PythonExecutable -m PyInstaller @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}
$releaseExecutable = if ($OneFile) {
    Join-Path $PackageRoot "dist\TIS_Retext.exe"
}
else {
    Join-Path $PackageRoot "dist\TIS_Retext\TIS_Retext.exe"
}

if (-not $OneFile) {
    $releaseRootForCleanup = Split-Path -Parent $releaseExecutable
    $autoCollectedCore = Join-Path $releaseRootForCleanup "_internal\libvlccore.dll"
    $managedCore = Join-Path $releaseRootForCleanup "_internal\libvlc\libvlccore.dll"
    if (
        (Test-Path -LiteralPath $autoCollectedCore -PathType Leaf) -and
        (Test-Path -LiteralPath $managedCore -PathType Leaf)
    ) {
        $autoHash = (Get-FileHash -LiteralPath $autoCollectedCore -Algorithm SHA256).Hash
        $managedHash = (Get-FileHash -LiteralPath $managedCore -Algorithm SHA256).Hash
        if ($autoHash -ne $managedHash) {
            throw "PyInstaller collected a conflicting root libvlccore.dll; refusing to remove it."
        }
        Remove-Item -LiteralPath $autoCollectedCore -Force
        Write-Host "[build] Removed duplicate root libvlccore.dll."
    }
    $bundledVlcRoot = Join-Path $releaseRootForCleanup "_internal\libvlc"
    foreach ($requiredVlcPayload in @(
        "libvlc.dll",
        "libvlccore.dll",
        "plugins\access\libfilesystem_plugin.dll",
        "plugins\demux\libmkv_plugin.dll",
        "plugins\codec\libavcodec_plugin.dll",
        "plugins\codec\libvpx_plugin.dll",
        "plugins\codec\libopus_plugin.dll",
        "plugins\codec\libvorbis_plugin.dll",
        "plugins\audio_output\libdirectsound_plugin.dll",
        "plugins\video_output\libdirect3d11_plugin.dll"
    )) {
        $requiredVlcPath = Join-Path $bundledVlcRoot $requiredVlcPayload
        if (-not (Test-Path -LiteralPath $requiredVlcPath -PathType Leaf)) {
            throw "Packaged VLC runtime is missing a required playback component: $requiredVlcPayload"
        }
    }
    $forbiddenVlcGui = Join-Path $bundledVlcRoot "plugins\gui"
    if (Test-Path -LiteralPath $forbiddenVlcGui) {
        throw "Packaged VLC runtime unexpectedly contains standalone GUI plugins."
    }
}

$probeOutput = Join-Path $PackageRoot "build\package-probe.json"
$probeData = Join-Path $PackageRoot "build\package-probe-data"
$previousDataRoot = $env:TIS_RETEXT_DATA_DIR
try {
    $env:TIS_RETEXT_DATA_DIR = $probeData
    $probeStart = [System.Diagnostics.ProcessStartInfo]::new()
    $probeStart.FileName = $releaseExecutable
    $probeStart.Arguments = "--package-probe `"$probeOutput`""
    $probeStart.UseShellExecute = $false
    $probeStart.CreateNoWindow = $true
    foreach ($externalVlcVariable in @(
        "TIS_RETEXT_VLC_DIR",
        "PYTHON_VLC_LIB_PATH",
        "PYTHON_VLC_MODULE_PATH",
        "VLC_PLUGIN_PATH"
    )) {
        [void]$probeStart.EnvironmentVariables.Remove($externalVlcVariable)
    }
    $probeProcess = [System.Diagnostics.Process]::Start($probeStart)
    if ($null -eq $probeProcess) {
        throw "Unable to start the packaged native dependency probe."
    }
    $probeProcess.WaitForExit()
    if ($probeProcess.ExitCode -ne 0) {
        throw "Packaged native dependency probe failed with exit code $($probeProcess.ExitCode)."
    }
    if (-not (Test-Path -LiteralPath $probeOutput -PathType Leaf)) {
        throw "Packaged native dependency probe did not create its result file."
    }
}
finally {
    $env:TIS_RETEXT_DATA_DIR = $previousDataRoot
    if (Test-Path -LiteralPath $probeData) {
        Remove-Item -LiteralPath $probeData -Recurse -Force
    }
}

$releaseRoot = Split-Path -Parent $releaseExecutable
$forbiddenData = Join-Path $releaseRoot "TIS_Retext_Data"
if (Test-Path -LiteralPath $forbiddenData) {
    throw "Release output is contaminated with runtime data: $forbiddenData"
}
$forbiddenAssets = Get-ChildItem -LiteralPath $releaseRoot -Recurse -File |
    Where-Object { $_.Extension.ToLowerInvariant() -in @(".pac", ".mdl", ".dds") }
if ($null -ne ($forbiddenAssets | Select-Object -First 1)) {
    throw "Release output contains game/runtime assets: $($forbiddenAssets[0].FullName)"
}
$forbiddenCaches = Get-ChildItem -LiteralPath $releaseRoot -Recurse -Force |
    Where-Object {
        $_.Name -eq "__pycache__" -or $_.Extension.ToLowerInvariant() -eq ".pyc"
    }
if ($null -ne ($forbiddenCaches | Select-Object -First 1)) {
    throw "Release output contains Python cache artifacts: $($forbiddenCaches[0].FullName)"
}
$forbiddenStandaloneFfmpeg = Get-ChildItem -LiteralPath $releaseRoot -Recurse -Force |
    Where-Object {
        ($_.Name.ToLowerInvariant() -in @("ffmpeg.exe", "ffprobe.exe")) -or
        ($_.FullName -match "[\\/]imageio_ffmpeg(?:[\\/]|$)")
    }
if ($null -ne ($forbiddenStandaloneFfmpeg | Select-Object -First 1)) {
    throw "Release output unexpectedly contains standalone FFmpeg/imageio-ffmpeg: $($forbiddenStandaloneFfmpeg[0].FullName)"
}

$appVersion = (& $PythonExecutable -c "from retext.version import __version__; print(__version__)").Trim()
$gitCommit = (git -C $PackageRoot rev-parse HEAD 2>$null)
$gitCommitExitCode = $LASTEXITCODE
$gitDirty = $true
if ($gitCommitExitCode -eq 0) {
    $gitStatus = @(git -C $PackageRoot status --porcelain 2>$null)
    $gitDirty = ($LASTEXITCODE -ne 0) -or ($gitStatus.Count -gt 0)
}
$probeResult = Get-Content -LiteralPath $probeOutput -Raw | ConvertFrom-Json
if ($probeResult.dpi_awareness -ne 2) {
    throw "Packaged application did not start with per-monitor DPI awareness."
}
$manifestExecutable = if ($OneFile) {
    "TIS_Retext.exe"
}
else {
    "TIS_Retext/TIS_Retext.exe"
}
$buildManifest = [PSCustomObject]@{
    product = "TIS_Retext"
    version = $appVersion
    git_commit = if ($gitCommitExitCode -eq 0) { $gitCommit.Trim() } else { "unknown" }
    git_dirty = $gitDirty
    python = $pythonVersion
    pyinstaller_mode = if ($OneFile) { "onefile" } else { "onedir" }
    vlc_version = $vlcVersion
    vlc_archive_sha256 = if ($null -ne $runtimeManifest) {
        [string]$runtimeManifest.archive_sha256
    }
    else {
        "unrecorded"
    }
    vlc_plugin_profile = "embedded-no-gui"
    executable = $manifestExecutable
    executable_sha256 = (Get-FileHash -LiteralPath $releaseExecutable -Algorithm SHA256).Hash
    package_probe = [PSCustomObject]@{
        ok = [bool]$probeResult.ok
        numpy = $probeResult.numpy
        pillow = $probeResult.pillow
        zstandard = $probeResult.zstandard
        blowfish_cle = [bool]$probeResult.blowfish_cle
        pac_fallback = [bool]$probeResult.pac_fallback
        text_pac_roundtrip = [bool]$probeResult.text_pac_roundtrip
        lz4_dds_decode = [bool]$probeResult.lz4_dds_decode
        dpi_awareness = $probeResult.dpi_awareness
        vlc = $probeResult.vlc
        vlc_runtime_source = $probeResult.vlc_runtime_source
        vlc_pcm_decode = [bool]$probeResult.vlc_pcm_decode
        gpu = $probeResult.gpu
        tcl = $probeResult.tcl
        openssl = $probeResult.openssl
    }
}
$manifestPath = Join-Path $PackageRoot "dist\TIS_Retext-build.json"
$buildManifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Write-Host "[build] Complete: $releaseExecutable"
Write-Host "[build] Probe:    $probeOutput"
Write-Host "[build] Manifest: $manifestPath"
