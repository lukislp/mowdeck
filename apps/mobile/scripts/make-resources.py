"""Render the source images of the app icons and splash screens into apps/mobile/resources/.

The artwork is the web app's icon (apps/web/static/icons/icon.svg): graphite mowing lanes with
the green MowDeck mark. ``@capacitor/assets`` turns these sources into every iOS and Android
size (``pnpm --filter mobile assets``). Run with the repository's Python (Pillow is locked)::

    uv run python apps/mobile/scripts/make-resources.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "resources"
SUPERSAMPLE = 2
"""Render at twice the size and scale down (smooth edges, bounded memory for the splash)."""

GRAPHITE = "#111412"
LANE = "#1a1e1c"
MARK_DARK_BG = "#56d37a"
MARK_LIGHT_BG = "#1f7a35"
LIGHT_BG = "#f4f5f2"
DARK_BG = "#0e1011"

# The mark of icon.svg in its 100 x 100 view box.
MARK = [(50, 22), (78, 76), (50, 62), (22, 76)]


def _canvas(
    size: int, color: str | tuple[int, int, int, int]
) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGBA", (size * SUPERSAMPLE, size * SUPERSAMPLE), color)
    return image, ImageDraw.Draw(image)


def _lanes(draw: ImageDraw.ImageDraw, size: int) -> None:
    scale = size * SUPERSAMPLE / 100
    for left in (16.667, 50.0, 83.333):
        draw.rectangle([left * scale, 0, (left + 16.667) * scale, 100 * scale], fill=LANE)


def _mark(
    draw: ImageDraw.ImageDraw, size: int, color: str, *, extent: float, center: float = 50
) -> None:
    """The mark scaled to ``extent`` percent of the canvas, centred."""
    scale = size * SUPERSAMPLE / 100
    factor = extent / 56  # the mark spans 56 units (22..78) in the view box
    points = [
        ((center + (x - 50) * factor) * scale, (center + (y - 49) * factor) * scale)
        for x, y in MARK
    ]
    draw.polygon(points, fill=color)


def _save(image: Image.Image, size: int, name: str, *, opaque: bool) -> None:
    image = image.resize((size, size), Image.Resampling.LANCZOS)
    if opaque:
        image = image.convert("RGB")
    image.save(OUT / name)
    print(f"wrote {OUT / name}")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    size = 1024
    # iOS icon (no transparency allowed) and the legacy Android icon.
    image, draw = _canvas(size, GRAPHITE)
    _lanes(draw, size)
    _mark(draw, size, MARK_DARK_BG, extent=56)
    _save(image, size, "icon-only.png", opaque=True)
    # Android adaptive icon: lanes as background, the mark in front (the generator insets
    # both layers to the visible 72 dp).
    image, draw = _canvas(size, GRAPHITE)
    _lanes(draw, size)
    _save(image, size, "icon-background.png", opaque=True)
    image, draw = _canvas(size, (0, 0, 0, 0))
    _mark(draw, size, MARK_DARK_BG, extent=50)
    _save(image, size, "icon-foreground.png", opaque=False)
    # Splash screens follow the system appearance like the app.
    splash = 2732
    image, draw = _canvas(splash, LIGHT_BG)
    _mark(draw, splash, MARK_LIGHT_BG, extent=14)
    _save(image, splash, "splash.png", opaque=True)
    image, draw = _canvas(splash, DARK_BG)
    _mark(draw, splash, MARK_DARK_BG, extent=14)
    _save(image, splash, "splash-dark.png", opaque=True)


if __name__ == "__main__":
    main()
