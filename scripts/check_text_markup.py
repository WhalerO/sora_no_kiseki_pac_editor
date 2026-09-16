"""Read-only check: compare game control-like strings with the decoded PAC payload."""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from retext.archive import FpacArchiveService
from retext.engines.kuro.processcle import unwrapCLE
from retext.paths import create_runtime_dir, cleanup_runtime_dir
from retext.core import RetextService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pac-dir", type=Path, required=True)
    args = parser.parse_args()
    archive_service = FpacArchiveService()
    service = RetextService()
    run = create_runtime_dir("markup_review")
    pattern = re.compile(rb"<#E\[[^\]]{1,100}\]#M_[^>]{1,50}>")
    report = []
    try:
        for name in ("script_sc.pac", "table_sc.pac", "script.pac", "table.pac"):
            archive = archive_service.inspect(args.pac_dir / name)
            count = 0
            sample = None
            for entry in archive.entries:
                if not entry.name.endswith((".dat", ".tbl")):
                    continue
                payload, layers = unwrapCLE(archive_service.read_entry_prefix(archive, entry.name, entry.size))
                matches = list(pattern.finditer(payload))
                count += len(matches)
                if sample is None and matches:
                    marker = max(matches, key=lambda m: len(m.group())).group()
                    source = archive_service.extract_entry(archive, entry.name, run / Path(entry.name).name)
                    engines = ("legacy", "kuro_dat" if source.suffix == ".dat" else "kuro_tbl")
                    parsed = {}
                    for engine in engines:
                        document = service.load(source, engine=engine)
                        units = [u for u in document.units if marker.decode() in u.current_text]
                        parsed[engine] = {"units": [u.index for u in units],
                                          "occurrences": sum(u.current_text.count(marker.decode()) for u in units)}
                    sample = {"entry": entry.name, "marker": marker.decode(), "payload_offset": payload.find(marker),
                              "raw_count": payload.count(marker), "layers": [v.decode() for v in layers], "parsed": parsed}
                    assert all(p["occurrences"] == sample["raw_count"] for p in parsed.values()), sample
            assert archive_service.source_is_current(archive, deep=True)
            report.append({"pac": name, "marker_count": count, "sample": sample})
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        cleanup_runtime_dir(run)


if __name__ == "__main__":
    main()
