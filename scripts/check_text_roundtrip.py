"""Exercise all text backends without requiring private game samples."""
from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retext import RetextService
from scripts.synthetic_samples import make_dat, make_tbl


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tbl", type=Path, help="Optional local TBL instead of a synthetic fixture.")
    parser.add_argument("--dat", type=Path, help="Optional local DAT instead of a synthetic fixture.")
    args = parser.parse_args()
    service = RetextService()
    with tempfile.TemporaryDirectory(prefix="tis_retext_smoke_") as folder:
        root = Path(folder)
        tbl = args.tbl.resolve() if args.tbl else root / "t_books.tbl"
        dat = args.dat.resolve() if args.dat else root / "demo.dat"
        if args.tbl is None:
            tbl.write_bytes(make_tbl())
        if args.dat is None:
            dat.write_bytes(make_dat())
        for engine, source in (("kuro_tbl", tbl), ("legacy", tbl),
                               ("legacy", dat), ("kuro_dat", dat)):
            original_hash = hashlib.sha256(source.read_bytes()).digest()
            document = service.load(source, engine=engine)
            editable = [u for u in document.units if u.current_text.strip()]
            if not editable:
                raise AssertionError(f"{engine}: no editable text in {source.name}")
            editable[-1].current_text += "【变长回环验证】"
            expected = [u.current_text for u in document.units]
            output = root / f"{engine}_{source.name}"
            service.save(document, output_path=output, do_backup=False)
            actual = service.load(output, engine=engine, schema_hint=source.stem)
            if [u.current_text for u in actual.units] != expected:
                raise AssertionError(f"{engine}: full-text roundtrip mismatch")
            if hashlib.sha256(source.read_bytes()).digest() != original_hash:
                raise AssertionError(f"{engine}: source changed")
            print(f"[ok] {engine} {source.suffix}: {len(expected)} strings; growth, full-text readback, source unchanged")


if __name__ == "__main__":
    main()
