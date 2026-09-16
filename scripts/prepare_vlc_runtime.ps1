param(
    [string]$Destination,
    [string]$ArchivePath,
    [switch]$ForceDownload
)

$ErrorActionPreference = "Stop"

$VlcVersion = "3.0.23"
$ArchiveName = "vlc-$VlcVersion-win64.zip"
$ArchiveUrl = "https://download.videolan.org/pub/videolan/vlc/$VlcVersion/win64/$ArchiveName"
$ArchiveSha256 = "992D19DBD0B8A7CDE9167D2F7780B1EF6F92ACC8A71ACFA736101A21F35181E1"
$ArchiveSize = 79893405

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackageRoot = Split-Path -Parent $ScriptDir
$DownloadRoot = Join-Path $PackageRoot ".runtime\downloads\vlc"
$StagingParent = Join-Path $PackageRoot ".runtime\staging"
if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path $PackageRoot "_vendor\libvlc\win-x64"
}
if ([string]::IsNullOrWhiteSpace($ArchivePath)) {
    $ArchivePath = Join-Path $DownloadRoot $ArchiveName
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
        return [System.BitConverter]::ToUInt16($bytes, $peOffset + 4) -eq 0x8664
    }
    catch {
        return $false
    }
}

function Assert-VlcRuntime {
    param([string]$RuntimeRoot)

    $libvlcPath = Join-Path $RuntimeRoot "libvlc.dll"
    $corePath = Join-Path $RuntimeRoot "libvlccore.dll"
    $pluginRoot = Join-Path $RuntimeRoot "plugins"
    $licensePath = Join-Path $RuntimeRoot "COPYING.txt"
    if (
        -not (Test-Path -LiteralPath $libvlcPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $corePath -PathType Leaf) -or
        -not (Test-X64PortableExecutable $libvlcPath) -or
        -not (Test-X64PortableExecutable $corePath) -or
        -not (Test-Path -LiteralPath $pluginRoot -PathType Container) -or
        -not (Test-Path -LiteralPath $licensePath -PathType Leaf) -or
        $null -eq (
            Get-ChildItem -LiteralPath $pluginRoot -Filter "*.dll" -File -Recurse |
                Select-Object -First 1
        )
    ) {
        throw "Invalid VLC runtime at $RuntimeRoot. Expected x64 core DLLs, a non-empty plugins directory, and COPYING.txt."
    }
}

New-Item -ItemType Directory -Path (Split-Path -Parent $ArchivePath) -Force | Out-Null

$shouldDownload = $ForceDownload -or -not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)
if ($shouldDownload) {
    $downloadTemp = Join-Path (
        Split-Path -Parent $ArchivePath
    ) "$ArchiveName.$([Guid]::NewGuid().ToString('N')).download"
    Write-Host "[vlc] Downloading VLC $VlcVersion win64..."
    try {
        Invoke-WebRequest -Uri $ArchiveUrl -OutFile $downloadTemp
        $downloadHash = (Get-FileHash -LiteralPath $downloadTemp -Algorithm SHA256).Hash
        if ($downloadHash -ne $ArchiveSha256) {
            throw "Downloaded VLC archive SHA-256 mismatch: $downloadHash"
        }
        Move-Item -LiteralPath $downloadTemp -Destination $ArchivePath -Force
    }
    finally {
        if (Test-Path -LiteralPath $downloadTemp -PathType Leaf) {
            Remove-Item -LiteralPath $downloadTemp -Force
        }
    }
}

$archiveItem = Get-Item -LiteralPath $ArchivePath
$actualHash = (Get-FileHash -LiteralPath $archiveItem.FullName -Algorithm SHA256).Hash
if ($actualHash -ne $ArchiveSha256) {
    throw "VLC archive SHA-256 mismatch: $actualHash`nExpected: $ArchiveSha256"
}
if ($archiveItem.Length -ne $ArchiveSize) {
    throw "VLC archive size mismatch: $($archiveItem.Length) bytes`nExpected: $ArchiveSize bytes"
}

New-Item -ItemType Directory -Path $StagingParent -Force | Out-Null
$stagingRoot = Join-Path $StagingParent "vlc-prep-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $stagingRoot | Out-Null
try {
    Write-Host "[vlc] Expanding verified archive..."
    Expand-Archive -LiteralPath $archiveItem.FullName -DestinationPath $stagingRoot
    $libvlcItem = Get-ChildItem -LiteralPath $stagingRoot -Filter "libvlc.dll" -File -Recurse |
        Select-Object -First 1
    if ($null -eq $libvlcItem) {
        throw "The verified archive does not contain libvlc.dll."
    }
    $sourceRoot = $libvlcItem.Directory.FullName
    Assert-VlcRuntime $sourceRoot

    $destinationRoot = [System.IO.Path]::GetFullPath($Destination)
    $destinationVolumeRoot = [System.IO.Path]::GetPathRoot($destinationRoot)
    if (
        $destinationRoot.Equals(
            $destinationVolumeRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or
        $destinationRoot.Equals(
            [System.IO.Path]::GetFullPath($PackageRoot),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Refusing to replace a broad VLC destination: $destinationRoot"
    }
    New-Item -ItemType Directory -Path $destinationRoot -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $sourceRoot "libvlc.dll") -Destination $destinationRoot -Force
    Copy-Item -LiteralPath (Join-Path $sourceRoot "libvlccore.dll") -Destination $destinationRoot -Force
    Copy-Item -LiteralPath (Join-Path $sourceRoot "COPYING.txt") -Destination $destinationRoot -Force
    $pluginDestination = Join-Path $destinationRoot "plugins"
    if (Test-Path -LiteralPath $pluginDestination -PathType Container) {
        $resolvedPluginDestination = [System.IO.Path]::GetFullPath(
            $pluginDestination
        )
        $destinationPrefix = $destinationRoot.TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        ) + [System.IO.Path]::DirectorySeparatorChar
        if (-not $resolvedPluginDestination.StartsWith(
            $destinationPrefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing to clean a plugin directory outside the VLC destination: $resolvedPluginDestination"
        }
        Remove-Item -LiteralPath $resolvedPluginDestination -Recurse -Force
    }
    New-Item -ItemType Directory -Path $pluginDestination -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $sourceRoot "plugins") -Force |
        Copy-Item -Destination $pluginDestination -Recurse -Force

    $payloadFiles = @(
        Get-Item -LiteralPath (Join-Path $destinationRoot "libvlc.dll")
        Get-Item -LiteralPath (Join-Path $destinationRoot "libvlccore.dll")
        Get-Item -LiteralPath (Join-Path $destinationRoot "COPYING.txt")
        Get-ChildItem -LiteralPath $pluginDestination -File -Recurse -Force
    )
    $destinationPrefixLength = $destinationRoot.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ).Length + 1
    $payloadManifest = @(
        foreach ($payloadFile in $payloadFiles) {
            [PSCustomObject]@{
                path = $payloadFile.FullName.Substring(
                    $destinationPrefixLength
                ).Replace("\", "/")
                size = $payloadFile.Length
                sha256 = (
                    Get-FileHash -LiteralPath $payloadFile.FullName -Algorithm SHA256
                ).Hash
            }
        }
    ) | Sort-Object path

    [PSCustomObject]@{
        component = "VLC win64 runtime"
        version = $VlcVersion
        archive = $ArchiveName
        archive_sha256 = $ArchiveSha256
        archive_size = $ArchiveSize
        source_url = $ArchiveUrl
        files = $payloadManifest
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (
        Join-Path $destinationRoot ".tis-retext-runtime.json"
    ) -Encoding UTF8

    Assert-VlcRuntime $destinationRoot
    $pluginCount = (
        Get-ChildItem -LiteralPath $pluginDestination -Filter "*.dll" -File -Recurse
    ).Count
    Write-Host "[vlc] Ready: $destinationRoot"
    Write-Host "[vlc] Version: $VlcVersion; plugins: $pluginCount; SHA-256: $actualHash"
}
finally {
    $resolvedStaging = [System.IO.Path]::GetFullPath($stagingRoot)
    $resolvedParent = [System.IO.Path]::GetFullPath($StagingParent).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    if ($resolvedStaging.StartsWith($resolvedParent, [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
    }
}
