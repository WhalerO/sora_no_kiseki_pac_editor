"""Minimal NumPy hook for the MDL animation accelerator.

PyInstaller's stock hook deliberately collects every binary dependency of a
Conda NumPy installation.  That pulls the complete MKL/ScaLAPACK runtime into
this application even though the animation path only uses NumPy core array
operations.  Keep NumPy's own binaries and required core hidden imports while
leaving its optional numerical suites out of the release.
"""

import json
import sys
from pathlib import Path

from packaging.version import Version
from PyInstaller import compat
from PyInstaller.utils.hooks import collect_dynamic_libs, get_installer


numpy_version = Version(
    compat.importlib_metadata.version("numpy")
).release

binaries = collect_dynamic_libs("numpy")
datas = []
hiddenimports = []

numpy_distribution = compat.importlib_metadata.distribution("numpy")
for entry in numpy_distribution.files or ():
    if (
        "dist-info" in entry.as_posix()
        and entry.name.casefold().startswith("license")
    ):
        license_path = Path(numpy_distribution.locate_file(entry))
        if license_path.is_file():
            datas.append((str(license_path), "licenses/numpy"))

# Conda's small libcblas/liblapack forwarding DLLs forward exports to the
# versioned MKL dispatcher.  Collect that dispatcher and only the core
# execution dependencies needed by NumPy rather than every architecture- and
# cluster-specific MKL binary.
if get_installer("numpy") == "conda" and compat.is_win:
    conda_bin = Path(sys.prefix) / "Library" / "bin"
    for pattern in (
        "libblas.dll",
        "libcblas.dll",
        "liblapack.dll",
        "mkl_core.*.dll",
        "mkl_mc3.*.dll",
        "mkl_rt.*.dll",
        "mkl_sequential.*.dll",
        "mkl_vml_mc3.*.dll",
    ):
        binaries.extend(
            (str(candidate), ".")
            for candidate in conda_bin.glob(pattern)
        )
    conda_meta = Path(sys.prefix) / "conda-meta"
    for metadata_path in conda_meta.glob("onemkl-license-*.json"):
        try:
            metadata = json.loads(
                metadata_path.read_text(encoding="utf-8")
            )
            package_root = Path(metadata["extracted_package_dir"])
        except (KeyError, OSError, ValueError):
            continue
        for license_path in package_root.glob(
            "info/licenses/**/license.txt"
        ):
            datas.append((str(license_path), "licenses/oneMKL"))
            break

if numpy_version >= (2, 0):
    hiddenimports.extend(
        [
            "numpy._core._dtype_ctypes",
            "numpy._core._multiarray_tests",
        ]
    )
    if numpy_version >= (2, 3):
        hiddenimports.append("numpy._core._exceptions")
else:
    hiddenimports.append("numpy.core._dtype_ctypes")
    if numpy_version >= (1, 25):
        hiddenimports.append("numpy.core._multiarray_tests")

excludedimports = [
    "distutils",
    "f2py",
    "nose",
    "numpy.distutils",
    "numpy.f2py",
    "numpy.fft",
    "numpy.ma",
    "numpy.polynomial",
    "numpy.random",
    "numpy.testing",
    "pytest",
    "scipy",
    "setuptools",
]
