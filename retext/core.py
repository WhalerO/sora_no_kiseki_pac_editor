from __future__ import annotations

from pathlib import Path

from .domain import DocumentKind, EngineCapability, TextDocument, WorkflowMode
from .engines.kuro import KuroDatEngine, KuroTblEngine
from .engines.legacy import LegacyEngine


class RetextService:
    def __init__(self) -> None:
        self.legacy = LegacyEngine()
        self.kuro_tbl = KuroTblEngine()
        self.kuro_dat = KuroDatEngine()
        self.engines = {
            self.legacy.name: self.legacy,
            self.kuro_tbl.name: self.kuro_tbl,
            self.kuro_dat.name: self.kuro_dat,
        }

    def capabilities(self) -> list[EngineCapability]:
        capabilities: list[EngineCapability] = []
        for engine in self.engines.values():
            capabilities.extend(engine.capabilities())
        return capabilities

    def resolve_engine_name(
        self,
        path: str | Path,
        *,
        mode: WorkflowMode = WorkflowMode.AGILE,
        engine: str | None = None,
    ) -> str:
        return self._resolve_engine(path, mode=mode, engine=engine).name

    def load(
        self,
        path: str | Path,
        *,
        mode: WorkflowMode = WorkflowMode.AGILE,
        engine: str | None = None,
        **kwargs,
    ) -> TextDocument:
        chosen = self._resolve_engine(path, mode=mode, engine=engine)
        return chosen.load(path, **kwargs)

    def save(
        self,
        document: TextDocument,
        *,
        output_path: str | Path | None = None,
        **kwargs,
    ) -> Path:
        engine = self._engine_by_name(document.engine)
        return engine.save(document, output_path=output_path, **kwargs)

    def preview_save(self, document: TextDocument, **kwargs):
        engine = self._engine_by_name(document.engine)
        return engine.preview_save(document, **kwargs)

    def _resolve_engine(
        self,
        path: str | Path,
        *,
        mode: WorkflowMode,
        engine: str | None,
    ):
        mode = WorkflowMode(mode)
        kind = DocumentKind.from_path(path)
        if engine:
            chosen = self._engine_by_name(engine)
            if not any(capability.kind == kind for capability in chosen.capabilities()):
                raise ValueError(f"Engine {engine} does not support {kind.value.upper()} files.")
            return chosen

        if mode == WorkflowMode.AGILE:
            return self.legacy
        if kind == DocumentKind.TBL:
            return self.kuro_tbl
        return self.legacy

    def _engine_by_name(self, name: str):
        try:
            return self.engines[name]
        except KeyError as exc:
            raise ValueError(f"Unknown engine: {name}") from exc
