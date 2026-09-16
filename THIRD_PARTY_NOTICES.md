# Third-party notices

This file inventories the third-party components intentionally used by
TIS_Retext.  It is an engineering aid, not legal advice.  A distributable
release must include the license files collected by `scripts/collect_licenses.py`
and complete the release blockers in `docs/RELEASE.md`.

TIS_Retext's original code is licensed under the MIT license in the root
`LICENSE`. This does not relicense the third-party components below.

| Component | Version/source | License | Use |
|---|---|---|---|
| KuroTools resources | bundled under `retext/engines/kuro/` | MIT | Structured TBL and experimental DAT interoperability |
| kuro_dlc_tool PAC scripts | bundled under `pac_tools/` | GPL-3.0 | Explicit, subprocess-only PAC fallback |
| LibVLC/VLC | 3.0.23 win64 | LGPL-2.1-or-later and component-specific notices | Embedded audio/video playback |
| python-vlc | 3.0.21203 | LGPL-2.1-or-later | LibVLC Python binding |
| Pillow | 12.2.0 | MIT-CMU | Image and software-render output |
| zstandard | 0.25.0 | BSD-3-Clause | CLE/Zstandard payloads |
| python-lz4 / LZ4 | 4.4.5 | BSD-3-Clause / BSD-2-Clause | Read-only LZ4-wrapped DDS decoding |
| NumPy | 2.4.6 | BSD-3-Clause plus bundled component licenses | Animation and GPU buffer acceleration |
| ModernGL | 5.12.0 | MIT | Off-screen OpenGL renderer |
| glcontext | 3.0.0 | MIT | OpenGL context creation |
| PyCryptodome | 3.23.0 | BSD/Public Domain components | Blowfish CLE compatibility |
| PyInstaller | 6.19.0 | GPL-2.0-or-later with bootloader exception | Windows packaging only |
| CPython and bundled native libraries | CPython 3.11 release environment | Python-2.0 and component-specific licenses | Runtime |

The package build copies installed Python dependency license files into
`licenses/python-packages/`, the KuroTools MIT license into its resource
directory, and the pinned LibVLC `COPYING.txt` beside LibVLC.  The canonical
GPL-3.0 text is stored at `licenses/GPL-3.0.txt` and `pac_tools/LICENSE`.

No standalone FFmpeg executable or `imageio-ffmpeg` package is distributed.
The pinned VLC runtime still contains component-specific codec modules such as
`libavcodec_plugin.dll`; its complete notices and corresponding-source duties
must be reviewed as part of the VLC distribution rather than omitted from the
media-stack audit. Strict releases record that human review in
`licenses/VLC-SOURCE.json`; the record is intentionally absent until approved.

## Corresponding source for the 0.1.2.dev20260916 preview

The [same GitHub Release](https://github.com/WhalerO/sora_no_kiseki_pac_editor/releases/tag/v0.1.2.dev20260916)
provides `TIS_Retext-0.1.2.dev20260916-third-party-sources.zip` at no charge.
It includes the unmodified upstream VLC 3.0.23 source archive, all 126 source
inputs listed in that release's `contrib/src/*/SHA512SUMS`, the python-vlc
3.0.21203 source distribution, and the GPL PAC fallback source scripts.
The VLC archive includes build instructions, contrib recipes and patches.
The source bundle includes the upstream license/copyright files and a
per-input checksum manifest. These checks establish file provenance; they
are not a statement of independent legal approval.

VLC source SHA-256:
`e891cae6aa3ccda69bf94173d5105cbc55c7a7d9b1d21b9b21666e69eff3e7e0`.
Upstream: <https://download.videolan.org/pub/videolan/vlc/3.0.23/vlc-3.0.23.tar.xz>.
Contrib inputs are checked against the SHA-512 values shipped in that archive.

The distributed VLC DLLs/plugins are unchanged upstream files, with GUI
plugins omitted. `licenses/vlc/runtime-files.json` records the actual payload;
`licenses/vlc/COPYING` and `COPYING.LIB` preserve GPL-2.0 and LGPL-2.1 terms.
The MIT license must not be interpreted as replacing the GPL/LGPL obligations
of a combined distribution. The Python binding and playback libraries are
replaceable; this project imposes no additional restriction on modifying,
relinking or debugging those components as permitted by their licenses.

The application's source and build scripts are available at the version tag
on the repository. `pac_tools/` includes the exact GPL fallback Python source,
its upstream notice and license, both in the repository and the application
bundle. No upstream author is presented as endorsing this application.
