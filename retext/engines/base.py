from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..domain import EngineCapability, SavePlan, TextDocument


class EngineBase(ABC):
    name: str

    @abstractmethod
    def capabilities(self) -> list[EngineCapability]:
        raise NotImplementedError

    @abstractmethod
    def load(self, path: str | Path, **kwargs) -> TextDocument:
        raise NotImplementedError

    @abstractmethod
    def save(
        self,
        document: TextDocument,
        *,
        output_path: str | Path | None = None,
        **kwargs,
    ) -> Path:
        raise NotImplementedError

    @abstractmethod
    def preview_save(self, document: TextDocument, **kwargs) -> SavePlan:
        raise NotImplementedError
