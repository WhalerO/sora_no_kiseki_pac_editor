"""Bounded display projections. None of these helpers change document text."""
from __future__ import annotations

import unicodedata
from collections.abc import Callable

ELLIPSIS = "……"


def resolve_hit_span(current: str, original: str, start: int, end: int) -> tuple[int, int] | None:
    """Resolve a scan hit without guessing between occurrences after an edit."""
    if not 0 <= start <= end <= len(original):
        return None
    if current == original:
        return start, end
    needle = original[start:end]
    if not needle:
        return None
    found = current.find(needle)
    if found < 0 or current.find(needle, found + 1) >= 0:
        return None
    return found, found + len(needle)


def find_text_span(text: str, query: str, *, case_sensitive: bool = False) -> tuple[int, int] | None:
    """Locate the first NFC/casefold match, returning original string offsets."""
    return next(iter(find_text_spans(text, query, case_sensitive=case_sensitive)), None)


def find_text_spans(text: str, query: str, *, case_sensitive: bool = False) -> list[tuple[int, int]]:
    """Non-overlapping matches with normalized offsets mapped back to source."""
    transform = lambda value: unicodedata.normalize("NFC", value) if case_sensitive else unicodedata.normalize("NFC", value).casefold()
    needle = transform(query)
    if not needle:
        return []
    normalized = transform(text)
    start = normalized.find(needle)
    if start < 0:
        return []
    positions = []
    while start >= 0:
        positions.append((start, start + len(needle)))
        start = normalized.find(needle, start + len(needle))
    if len(normalized) == len(text) and unicodedata.normalize("NFC", text) == text:
        return positions
    # Canonical combining sequences and Hangul Jamo can normalize to fewer
    # characters; casefold can expand a character (e.g. ß -> ss).
    mapping: list[tuple[int, int]] = []
    first = 0
    for index in range(1, len(text) + 1):
        joins = index < len(text) and (
            unicodedata.combining(text[index])
            or (0x1100 <= ord(text[index]) <= 0x11FF
                and (0x1100 <= ord(text[index - 1]) <= 0x11FF or 0xAC00 <= ord(text[index - 1]) <= 0xD7A3))
        )
        if joins:
            continue
        mapping.extend([(first, index)] * len(transform(text[first:index])))
        first = index
    return list(dict.fromkeys((mapping[start][0], mapping[end - 1][1]) for start, end in positions))


def _middle(value: str, count: int) -> str:
    if len(value) <= count:
        return value
    if count <= len(ELLIPSIS):
        return ELLIPSIS[:count]
    left = (count - len(ELLIPSIS) + 1) // 2
    right = count - len(ELLIPSIS) - left
    return value[:left] + ELLIPSIS + (value[-right:] if right else "")


def excerpt_parts(text: str, start: int, end: int, *, radius: int = 42,
                  max_match: int = 80, width: int | None = None,
                  measure: Callable[[str], int] = len,
                  measure_match: Callable[[str], int] | None = None) -> tuple[str, str, str]:
    """Keep the actual hit visible, including in narrow one-line result cells."""
    normalize = lambda value: value.replace("\r\n", "↵").replace("\r", "↵").replace("\n", "↵").replace("\t", " ")
    if not 0 <= start <= end <= len(text):
        return normalize(text[:80]) + (ELLIPSIS if len(text) > 80 else ""), "", ""
    radius = max(0, radius)
    # Bound slicing before normalization, even when an entire newspaper matches.
    size = min(end - start, max(4, max_match))
    if end - start > size:
        half = max(1, (size - len(ELLIPSIS)) // 2)
        match = normalize(text[start:start + half] + ELLIPSIS + text[end - half:end])
    else:
        match = normalize(text[start:end])
    def parts(context: int, matched: str) -> tuple[str, str, str]:
        left, right = max(0, start - context), min(len(text), end + context)
        return ((ELLIPSIS if left else "") + normalize(text[left:start]), matched,
                normalize(text[end:right]) + (ELLIPSIS if right < len(text) else ""))
    if width is None:
        return parts(radius, match)
    width = max(1, width)
    bold = measure_match or measure
    fits = lambda p: measure(p[0]) + bold(p[1]) + measure(p[2]) <= width
    minimum = parts(0, match)
    if not fits(minimum):
        if measure(parts(0, "")[0] + parts(0, "")[2]) + bold(match[:1]) > width:
            # In extremely narrow columns prioritize the hit over markers.
            count = min(len(match), max(1, width))
            while count and bold(match[:count]) > width:
                count -= 1
            return "", match[:count], ""
        lo, hi = 0, len(match)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fits(parts(0, _middle(match, mid))):
                lo = mid
            else:
                hi = mid - 1
        return parts(0, _middle(match, lo))
    lo, hi = 0, radius
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(parts(mid, match)):
            lo = mid
        else:
            hi = mid - 1
    return parts(lo, match)


def wrap_bounded(text: str, width: int, measure: Callable[[str], int], max_lines: int) -> list[str]:
    """Wrap only enough text to fill the capped row, without measuring a book."""
    width, max_lines = max(1, width), max(1, max_lines)
    # Limit work before normalizing. Two glyphs/pixel is deliberately generous;
    # long combining/control sequences are display-truncated, never data-truncated.
    budget = max(128, width * 2) * max_lines
    value = text[:budget].replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    lines: list[str] = []
    position = 0
    while position < len(value) and len(lines) < max_lines:
        end = value.find("\n", position, min(len(value), position + max(128, width * 2)))
        segment_end = end if end >= 0 else min(len(value), position + max(128, width * 2))
        segment = value[position:segment_end]
        lo, hi = 0, len(segment)
        while lo < hi:
            middle = (lo + hi + 1) // 2
            if measure(segment[:middle]) <= width:
                lo = middle
            else:
                hi = middle - 1
        take = max(1, lo) if segment else 0
        lines.append(segment[:take])
        position += take
        if position == end:
            position += 1
    if not lines:
        lines = [""]
    elif position == len(value) and value.endswith("\n") and len(lines) < max_lines:
        lines.append("")
    omitted = position < len(value) or len(text) > budget
    if omitted:
        last = lines[-1]
        while last and measure(last + ELLIPSIS) > width:
            last = last[:-1]
        lines[-1] = last + ELLIPSIS
    return lines
