from __future__ import annotations

import math


INTERACTIVE_PIXEL_BUDGET = 12_000
INTERACTIVE_MIN_WIDTH = 96
INTERACTIVE_MIN_HEIGHT = 72


def calculate_render_dimensions(
    display_width: int,
    display_height: int,
    *,
    interactive: bool,
) -> tuple[int, int]:
    """Choose a responsive interaction buffer while preserving final quality."""

    width = max(64, int(display_width))
    height = max(64, int(display_height))
    if not interactive or width * height <= INTERACTIVE_PIXEL_BUDGET:
        return width, height
    scale = math.sqrt(INTERACTIVE_PIXEL_BUDGET / (width * height))
    return (
        min(width, max(INTERACTIVE_MIN_WIDTH, round(width * scale))),
        min(height, max(INTERACTIVE_MIN_HEIGHT, round(height * scale))),
    )
