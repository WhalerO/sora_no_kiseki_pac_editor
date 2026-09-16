from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..engines.kuro.tbl import KuroTblEngine
from .association import animation_model_family


@dataclass(slots=True, frozen=True)
class ModelIdentityMatch:
    name: str
    kind: str
    model_key: str
    source_table: str
    match_mode: str


@dataclass(slots=True, frozen=True)
class ModelIdentityResolution:
    matches: tuple[ModelIdentityMatch, ...]
    warnings: tuple[str, ...] = ()
    searched_tables: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class ModelCompanionResolution:
    animation_family: str
    model_keys: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    searched_tables: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class _CachedRows:
    signature: tuple[int, int]
    rows: tuple[dict[str, Any], ...]


class ModelIdentityService:
    """Trace an MDL stem back to localized name/status table records."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, Path], _CachedRows] = {}
        self._lock = threading.Lock()

    def resolve(
        self,
        model_stem: str,
        table_paths: Mapping[str, str | Path],
    ) -> ModelIdentityResolution:
        exact = model_stem.strip().casefold()
        if not exact:
            return ModelIdentityResolution(())
        rows_by_table: dict[str, tuple[dict[str, Any], ...]] = {}
        warnings: list[str] = []
        searched: list[str] = []
        for table_key in ("t_name", "t_status"):
            raw_path = table_paths.get(table_key)
            if raw_path is None:
                continue
            path = Path(raw_path).resolve()
            searched.append(f"{table_key}.tbl")
            try:
                rows_by_table[table_key] = self._load_rows(
                    table_key,
                    path,
                )
            except Exception as exc:
                warnings.append(f"{table_key}.tbl 名称索引读取失败：{exc}")

        candidates = _model_candidates(exact)
        if exact.startswith("mon"):
            search_order = ("t_status", "t_name")
        else:
            search_order = ("t_name", "t_status")
        for candidate_index, candidate in enumerate(candidates):
            match_mode = "精确模型名" if candidate_index == 0 else "基础模型名"
            for table_key in search_order:
                rows = rows_by_table.get(table_key, ())
                matches = (
                    _match_name_rows(rows, candidate, match_mode)
                    if table_key == "t_name"
                    else _match_status_rows(rows, candidate, match_mode)
                )
                if matches:
                    return ModelIdentityResolution(
                        matches=_unique_matches(matches),
                        warnings=tuple(warnings),
                        searched_tables=tuple(searched),
                    )
        return ModelIdentityResolution(
            matches=(),
            warnings=tuple(warnings),
            searched_tables=tuple(searched),
        )

    def resolve_companion_models(
        self,
        animation_stem: str,
        table_paths: Mapping[str, str | Path],
    ) -> ModelCompanionResolution:
        """Resolve animation script/face families to compatible mesh keys."""

        exact = animation_stem.strip().casefold()
        family = animation_model_family(exact).casefold()
        if not family:
            return ModelCompanionResolution("", ())
        rows_by_table: dict[str, tuple[dict[str, Any], ...]] = {}
        warnings: list[str] = []
        searched: list[str] = []
        for table_key in ("t_name", "t_status"):
            raw_path = table_paths.get(table_key)
            if raw_path is None:
                continue
            path = Path(raw_path).resolve()
            searched.append(f"{table_key}.tbl")
            try:
                rows_by_table[table_key] = self._load_rows(
                    table_key,
                    path,
                )
            except Exception as exc:
                warnings.append(f"{table_key}.tbl 模型关联读取失败：{exc}")

        model_keys: list[str] = []
        if family.startswith("mon"):
            for row in rows_by_table.get("t_status", ()):
                if str(row.get("file4", "")).casefold() != family:
                    continue
                model_key = str(row.get("file1", "")).strip()
                if model_key:
                    model_keys.append(model_key)
        else:
            is_face_animation = exact.endswith("_face")
            for row in rows_by_table.get("t_name", ()):
                script_matches = (
                    str(row.get("script", "")).casefold() == family
                )
                face_matches = (
                    is_face_animation
                    and str(row.get("face", "")).casefold() == exact
                )
                if not (script_matches or face_matches):
                    continue
                model_key = str(row.get("model", "")).strip()
                if model_key:
                    model_keys.append(model_key)
        prioritized = sorted(
            dict.fromkeys(model_keys),
            key=lambda value: (
                value.casefold() != family,
                value.casefold(),
            ),
        )
        return ModelCompanionResolution(
            animation_family=family,
            model_keys=tuple(prioritized),
            warnings=tuple(warnings),
            searched_tables=tuple(searched),
        )

    def _load_rows(
        self,
        table_key: str,
        path: Path,
    ) -> tuple[dict[str, Any], ...]:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        cache_key = (table_key, path)
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached.signature == signature:
                return cached.rows
        engine = KuroTblEngine()
        state = engine.load(
            path,
            game="auto",
            schema_hint=table_key,
        ).state
        expected_header = (
            "NameTableData" if table_key == "t_name" else "StatusParam"
        )
        rows = tuple(
            row
            for header in state.headers
            if (
                header.name == expected_header
                and header.schema_content is not None
            )
            for row in header.data_rows
        )
        if not rows:
            raise ValueError(f"未找到可识别的 {expected_header} 记录。")
        with self._lock:
            self._cache[cache_key] = _CachedRows(signature, rows)
        return rows


def _model_candidates(model_stem: str) -> tuple[str, ...]:
    values = [model_stem]
    animation_base = re.split(r"_m(?:_|$)", model_stem, maxsplit=1)[0]
    if animation_base and animation_base not in values:
        values.append(animation_base)
    family = re.match(
        r"^(chr[a-z0-9]{4}(?:_c\d{2})?|mon\d{4}(?:_c\d{2})?)",
        model_stem,
    )
    if family is not None and family.group(1) not in values:
        values.append(family.group(1))
    root = re.match(r"^(chr[a-z0-9]{4}|mon\d{4})", model_stem)
    if root is not None and root.group(1) not in values:
        values.append(root.group(1))
    return tuple(values)


def _match_name_rows(
    rows: tuple[dict[str, Any], ...],
    candidate: str,
    match_mode: str,
) -> list[ModelIdentityMatch]:
    matches: list[ModelIdentityMatch] = []
    for row in rows:
        model = str(row.get("model", "")).casefold()
        name = str(row.get("name", "")).strip()
        if model != candidate or not name:
            continue
        matches.append(
            ModelIdentityMatch(
                name=name,
                kind=_identity_kind(candidate),
                model_key=str(row.get("model", "")),
                source_table="t_name.tbl / NameTableData.model",
                match_mode=match_mode,
            )
        )
    return matches


def _match_status_rows(
    rows: tuple[dict[str, Any], ...],
    candidate: str,
    match_mode: str,
) -> list[ModelIdentityMatch]:
    identity_fields = ("ai_file", "unknown")
    geometry_field = "file1"
    matched = [
        row
        for row in rows
        if any(
            str(row.get(field, "")).casefold() == candidate
            for field in identity_fields
        )
    ]
    source_field = "ai_file / unknown"
    if not matched:
        matched = [
            row
            for row in rows
            if str(row.get(geometry_field, "")).casefold() == candidate
        ]
        source_field = geometry_field
    return [
        ModelIdentityMatch(
            name=str(row.get("name", "")).strip(),
            kind=_identity_kind(candidate),
            model_key=next(
                (
                    str(row.get(field, ""))
                    for field in (*identity_fields, geometry_field)
                    if str(row.get(field, "")).casefold() == candidate
                ),
                candidate,
            ),
            source_table=f"t_status.tbl / StatusParam.{source_field}",
            match_mode=match_mode,
        )
        for row in matched
        if str(row.get("name", "")).strip()
    ]


def _identity_kind(model_key: str) -> str:
    if model_key.startswith("mon"):
        return "怪物"
    if model_key.startswith("chr"):
        return "角色 / NPC"
    return "场景对象"


def _unique_matches(
    matches: list[ModelIdentityMatch],
) -> tuple[ModelIdentityMatch, ...]:
    unique: list[ModelIdentityMatch] = []
    seen: set[tuple[str, str]] = set()
    for match in matches:
        key = (match.name, match.model_key.casefold())
        if key in seen:
            continue
        seen.add(key)
        unique.append(match)
    return tuple(unique)


model_identity_service = ModelIdentityService()
