from __future__ import annotations

import os
from pathlib import Path

from retext.paths import REPO_ROOT


def resolve_corpus_root() -> Path:
    configured = os.environ.get("TIS_RETEXT_TEST_CORPUS", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return REPO_ROOT / "test_raws"


CORPUS_ROOT = resolve_corpus_root()
