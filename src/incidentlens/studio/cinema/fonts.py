"""Font loading for the cinematic renderer.

DejaVu Sans / Sans Mono ship inside the package (see assets/fonts/, license
included) so a render looks identical on macOS, Linux and CI — no dependence
on whatever the host happens to have installed.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import as_file, files

from PIL import ImageFont

_FAMILIES = {
    "sans": "DejaVuSans.ttf",
    "sans-bold": "DejaVuSans-Bold.ttf",
    "mono": "DejaVuSansMono.ttf",
    "mono-bold": "DejaVuSansMono-Bold.ttf",
}


@lru_cache(maxsize=128)
def font(family: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a bundled font at a pixel size. Cached; sizes are quantized by caller."""
    file_name = _FAMILIES.get(family)
    if file_name is None:
        raise KeyError(f"unknown font family {family!r}; options: {sorted(_FAMILIES)}")
    resource = files("incidentlens.studio.cinema").joinpath("assets/fonts").joinpath(file_name)
    with as_file(resource) as path:
        return ImageFont.truetype(str(path), size=max(6, int(size)))


def fit_text(draw, text: str, family: str, size: int, max_width: int) -> ImageFont.FreeTypeFont:
    """Shrink a font until ``text`` fits ``max_width`` pixels (floor at 6px)."""
    f = font(family, size)
    while size > 6 and draw.textlength(text, font=f) > max_width:
        size -= 1
        f = font(family, size)
    return f
