from __future__ import annotations

import re
import copy
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .core import RetextService
from .domain import GameVersion, SavePlan, TextDocument, WorkflowMode
from .io_utils import atomic_write_bytes


@dataclass(slots=True)
class SessionOptions:
    mode: WorkflowMode = WorkflowMode.AGILE
    engine_override: str = "auto"
    tbl_engine: str = "kuro_tbl"
    dat_engine: str = "legacy"
    game: GameVersion = GameVersion.AUTO
    schema_hint: str = ""
    keep_artifacts: bool = False
    do_backup: bool = True
    allow_risky_repack: bool = False

    def engine_for_path(self, path: str | Path) -> str | None:
        """Resolve the global per-kind engine preference for one document."""

        if self.engine_override != "auto":
            return self.engine_override
        suffix = Path(path).suffix.lower()
        selected = self.tbl_engine if suffix == ".tbl" else self.dat_engine
        aliases = {
            "auto": None,
            "legacy": "legacy",
            "kuro": "kuro_tbl" if suffix == ".tbl" else "kuro_dat",
            "kuro_tbl": "kuro_tbl",
            "kuro_dat": "kuro_dat",
        }
        try:
            return aliases[selected]
        except KeyError as exc:
            raise ValueError(f"Unsupported {suffix or 'document'} engine: {selected}") from exc


class DocumentSession:
    def __init__(self, service: RetextService | None = None) -> None:
        self.service = service or RetextService()
        self.document: TextDocument | None = None
        self.options = SessionOptions()

    def open_document(self, path: str | Path, *, options: SessionOptions | None = None) -> TextDocument:
        self.options = options or self.options
        kwargs = self._build_load_kwargs()
        self.document = self.service.load(
            path,
            mode=self.options.mode,
            engine=self.options.engine_for_path(path),
            **kwargs,
        )
        return self.document

    def preview_save(self) -> SavePlan:
        self._require_document()
        return self.service.preview_save(self.document)

    def save(self) -> Path:
        self._require_document()
        return self.save_as(self.document.source_path)

    def save_as(self, path: str | Path) -> Path:
        self._require_document()
        target = Path(path).resolve()
        document = self.document
        expected_texts = [unit.current_text for unit in self.document.units]
        before = target.read_bytes() if target.exists() else None
        if target == document.source_path.resolve():
            loaded = getattr(document.state, "source_bytes", None)
            if loaded is None:
                loaded = getattr(document.state, "original_bytes", None)
            if loaded is not None and before != loaded:
                raise RuntimeError(f"Refusing to overwrite a file that changed on disk: {target}")
        # Engines may mutate their input on success. Keep both the live file
        # and the user's editable document unchanged until every check passes.
        with tempfile.TemporaryDirectory(prefix=".retext-save-", dir=target.parent) as folder:
            candidate = Path(folder) / target.name
            self.service.save(
                copy.deepcopy(document), output_path=candidate,
                keep_artifacts=self.options.keep_artifacts, do_backup=False,
                allow_unsafe_repack=self.options.allow_risky_repack,
            )
            refreshed = self._reload_and_verify(candidate, document.engine, expected_texts)
            current = target.read_bytes() if target.exists() else None
            if current != before:
                raise RuntimeError(f"Refusing to overwrite a file that changed on disk: {target}")
            atomic_write_bytes(target, candidate.read_bytes(),
                               do_backup=self.options.do_backup, expected_bytes=before)
        refreshed.source_path = target
        self.document = refreshed
        return target

    def update_unit(self, index: int, new_text: str) -> None:
        self._require_document()
        self.document.update_unit(index, new_text)

    def find_matches(self, keyword: str, *, case_sensitive: bool = False) -> list[tuple[int, int, int]]:
        from retext.text_presentation import find_text_spans
        if self.document is None:
            return []
        return [(unit.index, start, end) for unit in self.document.units
                for start, end in find_text_spans(unit.current_text, keyword, case_sensitive=case_sensitive)]

    def find_indices(self, keyword: str, *, case_sensitive: bool = False) -> list[int]:
        self._require_document()
        if not keyword:
            return []
        # Pasted input is not guaranteed to use the same canonical Unicode
        # form as strings decoded from a game file.  NFC keeps compatibility
        # characters distinct while making canonically equivalent text
        # searchable through the same path.
        probe = unicodedata.normalize("NFC", keyword)
        if not case_sensitive:
            probe = probe.casefold()
        matches: list[int] = []
        for unit in self.document.units:
            haystack = unicodedata.normalize("NFC", unit.current_text)
            if not case_sensitive:
                haystack = haystack.casefold()
            if probe in haystack:
                matches.append(unit.index)
        return matches

    def replace_all(self, find_text: str, replace_text: str, *, case_sensitive: bool = False) -> int:
        self._require_document()
        if not find_text:
            return 0

        changed = 0
        if case_sensitive:
            for unit in self.document.units:
                if find_text in unit.current_text:
                    unit.current_text = unit.current_text.replace(find_text, replace_text)
                    changed += 1
            return changed

        for unit in self.document.units:
            source = unit.current_text
            replaced, count = re.subn(
                re.escape(find_text),
                lambda _match: replace_text,
                source,
                flags=re.IGNORECASE,
            )
            if count:
                unit.current_text = replaced
                changed += 1
        return changed

    def changed_count(self) -> int:
        if self.document is None:
            return 0
        return len(self.document.changed_units())

    def _build_load_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "keep_artifacts": self.options.keep_artifacts,
            "game": GameVersion.normalize(self.options.game).value,
        }
        if self.options.schema_hint.strip():
            kwargs["schema_hint"] = self.options.schema_hint.strip()
        return kwargs

    def _require_document(self) -> None:
        if self.document is None:
            raise RuntimeError("No document is currently loaded.")

    def _reload_and_verify(self, path: Path, engine: str, expected_texts: list[str]) -> TextDocument:
        kwargs = self._build_load_kwargs()
        if engine == "kuro_tbl" and "schema_hint" not in kwargs and self.document is not None:
            schema_hint = self.document.metadata.get("schema_hint")
            if schema_hint:
                kwargs["schema_hint"] = schema_hint
        if engine == "kuro_tbl" and self.document is not None:
            # Verification must use the same concrete schema family selected
            # during the original load, even when the UI request was AUTO.
            resolved_game = self.document.metadata.get("resolved_game")
            if resolved_game:
                kwargs["game"] = str(resolved_game)
        refreshed = self.service.load(path, engine=engine, **kwargs)
        actual_texts = [unit.current_text for unit in refreshed.units]
        if actual_texts != expected_texts:
            raise RuntimeError("Staged file failed the text roundtrip verification; the target was not changed.")
        return refreshed
