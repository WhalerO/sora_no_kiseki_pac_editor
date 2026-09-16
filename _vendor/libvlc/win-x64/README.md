# LibVLC runtime staging directory

This directory is intentionally empty in Git. Native VLC binaries are large and
must not be committed to this repository.

Prepare the pinned and checksum-verified VLC 3.0.23 win64 runtime with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/prepare_vlc_runtime.ps1
```

This command makes both source runs and packaged builds use the same runtime.
The resulting layout contains:

```text
_vendor/libvlc/win-x64/
├── libvlc.dll
├── libvlccore.dll
├── COPYING.txt
├── .tis-retext-runtime.json  # archive identity + per-file SHA-256
└── plugins/
    └── ... DLL files ...
```

The application does not need `vlc.exe`. The preparation script records a
per-file SHA-256 manifest, and `scripts/build_exe.ps1` validates it before copying
the two core DLLs and all playback plugins except VLC's standalone GUI plugins.
It prints the detected native file version so release logs identify the exact
runtime that was used.

The build can also use an unpacked runtime elsewhere:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_exe.ps1 `
    -VlcRuntimeDir 'D:\tools\vlc-3.0.23'
```

or the `TIS_RETEXT_VLC_DIR` environment variable. A locally installed 64-bit VLC
is a final convenience fallback, but the vendored directory is preferred for
repeatable releases.

Redistributors must preserve and ship the license notices required by the VLC
and LibVLC distribution they stage here.
