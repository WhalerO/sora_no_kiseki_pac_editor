from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retext.paths import REPO_ROOT


REQUIRED_IMPORTS = {
    "zstandard": "zstandard",
    "lz4": "lz4.frame",
    "Pillow": "PIL",
    "moderngl": "moderngl",
    "glcontext": "glcontext",
    "numpy": "numpy",
    "python-vlc": "vlc",
    "pycryptodome": "Crypto",
}

EXCLUDED_SOURCE_PARTS = {
    ".git",
    ".runtime",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "scratch",
}
VLC_SOURCE_EVIDENCE = REPO_ROOT / "licenses" / "VLC-SOURCE.json"
VLC_VERSION = "3.0.23"
VLC_BINARY_ARCHIVE = "vlc-3.0.23-win64.zip"
VLC_BINARY_SHA256 = (
    "992D19DBD0B8A7CDE9167D2F7780B1EF6F92ACC8A71ACFA736101A21F35181E1"
)


def run_release_check(*, run_smoke: bool, strict: bool = False) -> int:
    if not _check_repository_hygiene(strict=strict):
        return 1
    if not _check_dependencies(strict=strict):
        return 1

    print("[check] compiling package...", flush=True)
    for path in _iter_python_files(REPO_ROOT):
        try:
            with tokenize.open(path) as stream:
                compile(stream.read(), str(path), "exec")
        except (OSError, SyntaxError, UnicodeError) as exc:
            print(f"[fail] compile failed: {path}")
            print(exc)
            return 1

    print("[check] running regression tests...", flush=True)
    tests = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=REPO_ROOT,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "."},
    )
    if tests.returncode != 0:
        print(f"[fail] regression tests exited with code {tests.returncode}.")
        return tests.returncode

    if run_smoke:
        print("[check] running smoke test...", flush=True)
        env = {**os.environ, "PYTHONPATH": "."}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "scripts/smoke_test.py"],
            cwd=REPO_ROOT,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            print(f"[fail] smoke test exited with code {result.returncode}.")
            return result.returncode

    print("[ok] release checks passed.", flush=True)
    return 0


def _check_repository_hygiene(*, strict: bool) -> bool:
    print("[check] checking repository hygiene...", flush=True)
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        print("[fail] unable to enumerate tracked repository files.")
        return False
    tracked = [
        item.decode("utf-8", errors="replace").replace("\\", "/")
        for item in completed.stdout.split(b"\0")
        if item
    ]
    existing_tracked = [
        path for path in tracked if (REPO_ROOT / Path(path)).exists()
    ]
    generated = [
        path
        for path in existing_tracked
        if (
            path.endswith((".pyc", ".pyo"))
            or "/__pycache__/" in f"/{path}"
            or path.startswith((".runtime/", "build/", "dist/"))
        )
    ]
    if generated:
        print("[fail] generated files are tracked by Git:")
        for path in generated[:20]:
            print(f"  - {path}")
        if len(generated) > 20:
            print(f"  ... and {len(generated) - 20} more")
        return False

    diff_check = subprocess.run(
        ["git", "diff", "--check"],
        cwd=REPO_ROOT,
        check=False,
    )
    if diff_check.returncode != 0:
        print("[fail] git diff --check reported whitespace errors.")
        return False

    blockers: list[str] = []
    if not (REPO_ROOT / "LICENSE").is_file():
        blockers.append("root LICENSE has not been selected")
    if not (REPO_ROOT / "THIRD_PARTY_NOTICES.md").is_file():
        blockers.append("THIRD_PARTY_NOTICES.md is missing")
    if not (REPO_ROOT / "samples" / "AUTHORIZATION.md").is_file():
        blockers.append("sample corpus authorization record is missing")
    vlc_evidence_issue = _check_vlc_source_evidence(VLC_SOURCE_EVIDENCE)
    if vlc_evidence_issue:
        blockers.append(vlc_evidence_issue)
    private_corpus = [
        path
        for path in existing_tracked
        if path.startswith("test_raws/") and path != "test_raws/README.md"
    ]
    if private_corpus:
        blockers.append(
            f"{len(private_corpus)} real-corpus files remain tracked under test_raws/"
        )
    if strict and blockers:
        print("[fail] strict release blockers:")
        for blocker in blockers:
            print(f"  - {blocker}")
        return False
    if strict:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            print("[fail] strict releases require a clean Git working tree.")
            return False
    for blocker in blockers:
        print(f"[warn] {blocker}")
    return True


def _check_vlc_source_evidence(path: Path) -> str | None:
    """Validate the human-approved corresponding-source release record."""

    if not path.is_file():
        return "VLC component/source compliance record is missing"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return f"VLC component/source compliance record is invalid: {exc}"
    expected = {
        "component": "VLC win64 runtime",
        "version": VLC_VERSION,
        "binary_archive": VLC_BINARY_ARCHIVE,
        "binary_archive_sha256": VLC_BINARY_SHA256,
        "status": "approved",
    }
    for key, value in expected.items():
        if record.get(key) != value:
            return f"VLC component/source compliance record has invalid {key}"
    source_url = record.get("source_archive_url")
    source_sha256 = record.get("source_archive_sha256")
    offer = record.get("corresponding_source_offer")
    notice_inventory = record.get("component_notice_inventory")
    reviewer = record.get("reviewed_by")
    reviewed_on = record.get("reviewed_on")
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        return "VLC component/source compliance record lacks a HTTPS source archive URL"
    if not isinstance(source_sha256, str) or re.fullmatch(
        r"[0-9A-Fa-f]{64}", source_sha256
    ) is None:
        return "VLC component/source compliance record lacks a source archive SHA-256"
    if not isinstance(offer, str) or not offer.strip():
        return "VLC component/source compliance record lacks corresponding-source terms"
    if not isinstance(notice_inventory, str) or not notice_inventory.strip():
        return "VLC component/source compliance record lacks a component notice inventory"
    if not isinstance(reviewer, str) or not reviewer.strip():
        return "VLC component/source compliance record lacks reviewer approval"
    if not isinstance(reviewed_on, str) or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", reviewed_on
    ) is None:
        return "VLC component/source compliance record lacks an approval date"
    return None


def _check_dependencies(*, strict: bool) -> bool:
    print("[check] importing runtime dependencies...", flush=True)
    for distribution, module_name in REQUIRED_IMPORTS.items():
        try:
            if module_name == "vlc":
                # python-vlc loads libvlc.dll while importing.  Source runs
                # must configure the same vendored/system search path as the
                # real application before that import occurs.
                from retext.vlc_runtime import configure_vlc_runtime

                configure_vlc_runtime()
            importlib.import_module(module_name)
            version = importlib.metadata.version(distribution)
        except Exception as exc:
            print(f"[fail] {distribution}: {type(exc).__name__}: {exc}")
            return False
        print(f"  {distribution}=={version}")
    if strict and sys.version_info[:2] != (3, 11):
        print(
            "[fail] strict release builds require CPython 3.11.x; "
            f"current version is {sys.version.split()[0]}."
        )
        return False
    if strict and not _check_release_pins():
        return False
    return True


def _check_release_pins() -> bool:
    pins_path = REPO_ROOT / "requirements-release.txt"
    try:
        lines = pins_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"[fail] cannot read release dependency pins: {exc}")
        return False
    ok = True
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            print(f"[fail] release dependency is not exactly pinned: {line}")
            ok = False
            continue
        requirement, expected = line.split("==", 1)
        distribution = requirement.split("[", 1)[0]
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            print(f"[fail] pinned release dependency is missing: {distribution}")
            ok = False
            continue
        if actual != expected:
            print(
                f"[fail] release dependency mismatch: {distribution}=={actual}; "
                f"expected {expected}"
            )
            ok = False
    return ok


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in EXCLUDED_SOURCE_PARTS for part in path.parts):
            continue
        yield path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run release-prep checks for TIS_Retext.")
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Skip the sample TBL/DAT smoke test; compilation and regression tests still run.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on legal/corpus/reproducibility blockers required for a distributable release.",
    )
    args = parser.parse_args()
    raise SystemExit(
        run_release_check(
            run_smoke=not args.skip_smoke,
            strict=args.strict,
        )
    )


if __name__ == "__main__":
    main()
