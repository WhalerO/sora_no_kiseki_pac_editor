from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retext.version import __version__


def numeric_version(version: str) -> tuple[int, int, int, int]:
    """Convert the public version to the four integers required by Windows."""

    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[.+-].*)?", version)
    if match is None:
        raise ValueError(f"Unsupported application version: {version!r}")
    return tuple(int(part) for part in (*match.groups(), "0"))


def render_version_info(version: str = __version__) -> str:
    numeric = numeric_version(version)
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numeric!r},
    prodvers={numeric!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404B0',
        [
          StringStruct('CompanyName', 'TIS_Retext contributors'),
          StringStruct('FileDescription', 'Trails in the Sky remake text workspace'),
          StringStruct('FileVersion', '{version}'),
          StringStruct('InternalName', 'TIS_Retext'),
          StringStruct('OriginalFilename', 'TIS_Retext.exe'),
          StringStruct('ProductName', 'TIS_Retext'),
          StringStruct('ProductVersion', '{version}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def write_version_info(output: Path) -> Path:
    destination = output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_version_info(), encoding="utf-8", newline="\n")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate PyInstaller Windows version metadata."
    )
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(write_version_info(args.output))


if __name__ == "__main__":
    main()
