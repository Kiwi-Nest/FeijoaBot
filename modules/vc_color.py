from __future__ import annotations

import colorsys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import timedelta


def streak_color(duration: timedelta) -> tuple[int, int, int]:
    secs = duration.total_seconds()
    hue = (secs / 86400) % 1.0
    day = secs / 86400
    if day < 5:
        lightness = 0.5 + 0.3 * (day / 5)
    elif day < 6:
        lightness = 0.8 - 0.6 * (day - 5)
    else:
        lightness = 0.2 + 0.3 * min(day - 6, 5) / 5
    r, g, b = colorsys.hls_to_rgb(hue, lightness, 1.0)
    return int(r * 255), int(g * 255), int(b * 255)
