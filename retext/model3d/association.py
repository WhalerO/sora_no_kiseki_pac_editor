from __future__ import annotations

from pathlib import Path, PurePosixPath


def animation_model_family(model_stem: str) -> str:
    """Return the logical mesh family encoded by an animation MDL name."""

    normalized = model_stem.strip()
    lowered = normalized.casefold()
    marker = lowered.find("_m_")
    if marker > 0:
        return normalized[:marker]
    if lowered.endswith("_face") and len(normalized) > len("_face"):
        return normalized[: -len("_face")]
    marker = lowered.find("_mot_")
    if marker > 0:
        return normalized[:marker]
    return ""


def companion_model_entry(logical_path: str, model_key: str) -> str | None:
    """Build a safe sibling MDL path for a resolved model key."""

    normalized = logical_path.replace("\\", "/")
    logical = PurePosixPath(normalized)
    key = model_key.strip()
    if (
        not key
        or "/" in key
        or "\\" in key
        or key in {".", ".."}
        or ":" in key
    ):
        return None
    candidate = logical.with_name(f"{key}.mdl")
    if candidate == logical:
        return None
    return candidate.as_posix()


def direct_companion_model_entry(logical_path: str) -> str | None:
    logical = PurePosixPath(logical_path.replace("\\", "/"))
    family = animation_model_family(logical.stem)
    return companion_model_entry(logical.as_posix(), family) if family else None


def companion_model_path(
    model_path: str | Path,
    model_key: str,
) -> Path | None:
    source = Path(model_path)
    entry = companion_model_entry(source.name, model_key)
    if entry is None:
        return None
    candidate = source.with_name(PurePosixPath(entry).name)
    return candidate if candidate.is_file() else None
