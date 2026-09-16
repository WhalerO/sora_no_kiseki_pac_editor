"""One source of truth for result labels and whole-row colors."""
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class HitPresentation:
    key: str
    label: str
    background: str
    foreground: str


HIT_STYLES = {
    "unchecked": HitPresentation("unchecked", "未勾选", "#ffffff", "#202b3b"),
    "unchanged": HitPresentation("unchanged", "未变更", "#f0f2f5", "#596579"),
    "blocked": HitPresentation("blocked", "不可写", "#fce9e9", "#943b3b"),
    "risk": HitPresentation("risk", "风险·回退", "#fff0d6", "#8a5618"),
    "rebuild": HitPresentation("rebuild", "可写·重建", "#eaf2ff", "#274b7a"),
    "patch": HitPresentation("patch", "可写·原位", "#eaf6ef", "#285b3d"),
    "write": HitPresentation("write", "可写", "#f2f8ed", "#405a31"),
}


def hit_presentation(hit):
    mode = getattr(hit, "write_mode", "").lower()
    if getattr(hit, "pair_old", None) == getattr(hit, "pair_new", "") or mode in ("unchanged", "search-only"):
        return HIT_STYLES["unchanged"]
    if not getattr(hit, "writable", True):
        return HIT_STYLES["blocked"]
    if mode == "repack-risk":
        return HIT_STYLES["risk"]
    if any(part in mode for part in ("repack", "relocat", "roundtrip")):
        return HIT_STYLES["rebuild"]
    return HIT_STYLES["patch" if mode in ("patch", "slot", "inplace", "in-place") else "write"]


def hit_row_presentation(hit):
    """Selection changes row color, never the underlying write capability."""
    presentation = hit_presentation(hit)
    if not getattr(hit, "checked", True):
        return replace(HIT_STYLES["unchecked"], label=presentation.label)
    return presentation
