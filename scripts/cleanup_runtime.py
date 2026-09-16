from __future__ import annotations

import argparse

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retext.runtime import cleanup_runtime, describe_runtime


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean TIS_Retext runtime cache.")
    parser.add_argument("--all", action="store_true", help="Remove all runtime artifacts, not just transient caches.")
    args = parser.parse_args()

    before = describe_runtime()
    removed = cleanup_runtime(remove_all=args.all)
    after = describe_runtime()

    print(f"Removed: {len(removed)}")
    print(f"Before: {before}")
    print(f"After:  {after}")
    for path in removed:
        print(path.name)
    if after["transient_count"] > 0:
        print("Remaining transient entries could not be removed and may require manual cleanup.")


if __name__ == "__main__":
    main()
