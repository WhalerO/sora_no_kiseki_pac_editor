from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import sys
from pathlib import Path


DISTRIBUTIONS = (
    "zstandard",
    "lz4",
    "Pillow",
    "moderngl",
    "glcontext",
    "numpy",
    "python-vlc",
    "pycryptodome",
    "PyInstaller",
)

NATIVE_LICENSES = (
    ("libffi", "Library/usr/share/licenses/libffi/LICENSE"),
    ("OpenSSL", "Library/usr/share/licenses/libopenssl/LICENSE.txt"),
    ("Expat", "Library/usr/share/licenses/expat/COPYING"),
    ("XZ/liblzma", "Library/share/doc/xz/COPYING"),
    ("XZ/liblzma 0BSD notice", "Library/share/doc/xz/COPYING.0BSD"),
    ("SQLite", "Library/usr/share/licenses/libsqlite/LICENSE"),
    ("zlib", "Library/usr/share/licenses/zlib/LICENSE"),
    ("Tcl/Tk", "Library/lib/tk8.6/license.terms"),
)


def collect(output: Path, *, strict: bool = False) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    components: list[dict[str, object]] = []
    missing: list[str] = []
    missing_native: list[str] = []
    for name in DISTRIBUTIONS:
        distribution = importlib.metadata.distribution(name)
        component_root = output / name.casefold().replace("-", "_")
        copied: list[str] = []
        for item in distribution.files or ():
            filename = str(item).replace("\\", "/")
            upper = filename.upper()
            if not any(token in upper for token in ("LICENSE", "COPYING", "NOTICE")):
                continue
            source = Path(distribution.locate_file(item))
            if not source.is_file():
                continue
            destination = component_root / Path(filename).name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.read_bytes() == source.read_bytes():
                copied.append(
                    str(destination.relative_to(output)).replace("\\", "/")
                )
                continue
            if destination.exists():
                destination = component_root / f"{len(copied):03d}-{source.name}"
            shutil.copy2(source, destination)
            copied.append(str(destination.relative_to(output)).replace("\\", "/"))
        components.append(
            {
                "name": name,
                "version": distribution.version,
                "license": distribution.metadata.get("License-Expression")
                or distribution.metadata.get("License")
                or "see bundled license files",
                "files": copied,
            }
        )
        if not copied:
            missing.append(f"{name}: no LICENSE/COPYING/NOTICE file was found")

    python_license = next(
        (
            candidate
            for candidate in (
                Path(sys.base_prefix) / "LICENSE.txt",
                Path(sys.base_prefix) / "LICENSE_PYTHON.txt",
            )
            if candidate.is_file()
        ),
        None,
    )
    if python_license is not None:
        destination = output / "python" / "LICENSE.txt"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(python_license, destination)
        components.append(
            {
                "name": "CPython",
                "version": sys.version.split()[0],
                "license": "Python-2.0",
                "files": ["python/LICENSE.txt"],
            }
        )
    else:
        missing.append("CPython: base interpreter license file was not found")

    for name, relative_source in NATIVE_LICENSES:
        source = Path(sys.base_prefix) / Path(relative_source)
        if not source.is_file():
            missing_native.append(f"{name}: {relative_source}")
            continue
        component_name = name.casefold().replace("/", "_").replace(" ", "_")
        destination = output / "native" / component_name / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        components.append(
            {
                "name": name,
                "version": "from selected CPython environment",
                "license": "see bundled license file",
                "files": [
                    str(destination.relative_to(output)).replace("\\", "/")
                ],
            }
        )

    manifest = {
        "components": components,
        "missing": missing,
        "missing_native": missing_native,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Conda distributes these DLLs as independent packages, so their license
    # files must be present independently.  The python.org Windows runtime
    # instead carries its bundled-library notices in the CPython license file.
    conda_environment = (Path(sys.base_prefix) / "conda-meta").is_dir()
    strict_missing = missing + (missing_native if conda_environment else [])
    if strict and strict_missing:
        details = "\n  - ".join(strict_missing)
        raise RuntimeError(f"incomplete license collection:\n  - {details}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect installed dependency license files for packaging."
    )
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail if a required distribution/interpreter license is missing.",
    )
    args = parser.parse_args()
    manifest = collect(args.output.resolve(), strict=args.strict)
    print(f"Collected licenses for {len(manifest['components'])} components.")


if __name__ == "__main__":
    main()
