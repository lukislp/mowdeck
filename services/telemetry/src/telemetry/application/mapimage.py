"""A small PNG of a mower's map for the iOS map widget (ADR 0059).

WidgetKit cannot run a map view or draw large vector data within its memory limits, so the
server draws the picture: the map areas, no-go zones, the mowed path, the dock and the mower. The
module is pure (numpy and zlib, no I/O, no clock): ``render`` takes already parsed geometry in the
local metres of the map and returns the PNG bytes.

* **Bounded**: the size is clamped to :data:`MAX_WIDTH` x :data:`MAX_HEIGHT` (and
  :data:`MAX_PIXELS`), points per layer to :data:`MAX_POINTS`, so a request cannot make the
  service allocate or loop without limit.
* **Anti-aliased** by drawing at twice the size and averaging 2 x 2 blocks.
* **Theme**: ``light`` or ``dark`` palettes; widgets ask for the one that matches the system.
* No text is drawn. The widget puts state and progress on top natively, so the language and
  Dynamic Type stay the device's business.
"""

import math
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray

Point = tuple[float, float]
Theme = Literal["light", "dark"]

MIN_WIDTH, MAX_WIDTH = 160, 1200
MIN_HEIGHT, MAX_HEIGHT = 96, 800
MAX_PIXELS = 700_000
"""Most output pixels (a 1200 x 800 request is scaled down to fit)."""
MAX_POINTS = 20_000
"""Most points drawn per layer (more are thinned evenly)."""
SUPERSAMPLE = 2
PADDING = 0.08
"""Share of the shorter side kept free around the map."""

type Canvas = NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class Palette:
    """Colours as RGB floats in 0..255."""

    background: tuple[float, float, float]
    area_fill: tuple[float, float, float]
    area_line: tuple[float, float, float]
    zone_fill: tuple[float, float, float]
    planned: tuple[float, float, float]
    mowed: tuple[float, float, float]
    dock: tuple[float, float, float]
    mower: tuple[float, float, float]
    mower_ring: tuple[float, float, float]


PALETTES: dict[str, Palette] = {
    "light": Palette(
        background=(242, 244, 241),
        area_fill=(214, 233, 207),
        area_line=(94, 140, 84),
        zone_fill=(236, 205, 200),
        planned=(168, 196, 160),
        mowed=(46, 125, 50),
        dock=(80, 90, 100),
        mower=(0, 122, 255),
        mower_ring=(255, 255, 255),
    ),
    "dark": Palette(
        background=(24, 27, 25),
        area_fill=(38, 60, 38),
        area_line=(110, 165, 100),
        zone_fill=(80, 42, 40),
        planned=(60, 90, 58),
        mowed=(120, 205, 110),
        dock=(190, 198, 206),
        mower=(10, 132, 255),
        mower_ring=(20, 22, 20),
    ),
}


@dataclass(frozen=True, slots=True)
class MapScene:
    """Everything to draw, in the local metres of the map."""

    areas: Sequence[Sequence[Point]] = field(default_factory=list)
    no_go: Sequence[Sequence[Point]] = field(default_factory=list)
    planned: Sequence[Sequence[Point]] = field(default_factory=list)
    """Polylines still to do (thin, pale)."""
    mowed: Sequence[Sequence[Point]] = field(default_factory=list)
    """Polylines already done (strong)."""
    dock: Point | None = None
    mower: Point | None = None


def clamp_size(width: int, height: int) -> tuple[int, int]:
    """The requested size within the limits, scaled down to ``MAX_PIXELS`` keeping the ratio."""
    w = max(MIN_WIDTH, min(MAX_WIDTH, width))
    h = max(MIN_HEIGHT, min(MAX_HEIGHT, height))
    if w * h > MAX_PIXELS:
        scale = math.sqrt(MAX_PIXELS / (w * h))
        w, h = max(MIN_WIDTH, int(w * scale)), max(MIN_HEIGHT, int(h * scale))
    return w, h


def _thin(points: Sequence[Point], limit: int) -> Sequence[Point]:
    if len(points) <= limit:
        return points
    step = len(points) / limit
    return [points[int(i * step)] for i in range(limit)] + [points[-1]]


def _bounds(scene: MapScene) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    groups: list[Sequence[Sequence[Point]]] = [scene.areas, scene.planned, scene.mowed]
    for group in groups:
        for ring in group:
            for x, y in ring:
                xs.append(x)
                ys.append(y)
    for extra in (scene.dock, scene.mower):
        if extra is not None:
            xs.append(extra[0])
            ys.append(extra[1])
    finite = [(x, y) for x, y in zip(xs, ys, strict=True) if math.isfinite(x) and math.isfinite(y)]
    if not finite:
        return None
    fx, fy = zip(*finite, strict=True)
    return min(fx), min(fy), max(fx), max(fy)


class _View:
    """Local metres to pixels of the (supersampled) canvas: uniform scale, centred, y up."""

    def __init__(self, bounds: tuple[float, float, float, float], width: int, height: int) -> None:
        x0, y0, x1, y1 = bounds
        span_x = max(x1 - x0, 5.0)
        span_y = max(y1 - y0, 5.0)
        pad = PADDING * min(width, height)
        self.scale = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)
        self.cx = (x0 + x1) / 2
        self.cy = (y0 + y1) / 2
        self.width, self.height = width, height

    def px(self, points: Sequence[Point]) -> NDArray[np.float64]:
        arr = np.asarray(points, dtype=np.float64).reshape(-1, 2)
        out = np.empty_like(arr)
        out[:, 0] = (arr[:, 0] - self.cx) * self.scale + self.width / 2
        out[:, 1] = self.height / 2 - (arr[:, 1] - self.cy) * self.scale
        return out


def _fill_polygon(
    canvas: Canvas, pts: NDArray[np.float64], color: tuple[float, float, float]
) -> None:
    """Even-odd scanline fill of one ring."""
    if len(pts) < 3:
        return
    height, width = canvas.shape[:2]
    ys = pts[:, 1]
    first = max(math.floor(ys.min()), 0)
    last = min(math.ceil(ys.max()), height - 1)
    nxt = np.roll(pts, -1, axis=0)
    x_a, y_a, x_b, y_b = pts[:, 0], pts[:, 1], nxt[:, 0], nxt[:, 1]
    horizontal = y_a == y_b
    for row in range(first, last + 1):
        yc = row + 0.5
        crossing = ((y_a <= yc) & (y_b > yc)) | ((y_b <= yc) & (y_a > yc))
        crossing &= ~horizontal
        if not crossing.any():
            continue
        t = (yc - y_a[crossing]) / (y_b[crossing] - y_a[crossing])
        xs = np.sort(x_a[crossing] + t * (x_b[crossing] - x_a[crossing]))
        for left, right in zip(xs[0::2], xs[1::2], strict=False):
            lo = max(math.ceil(left - 0.5), 0)
            hi = min(math.floor(right - 0.5), width - 1)
            if hi >= lo:
                canvas[row, lo : hi + 1] = np.asarray(color, dtype=np.float32)


def _stamp(
    canvas: Canvas,
    centres: NDArray[np.float64],
    radius: float,
    color: tuple[float, float, float],
) -> None:
    """Paint discs of ``radius`` pixels at ``centres`` (array of x, y)."""
    height, width = canvas.shape[:2]
    r = max(radius, 0.5)
    reach = math.ceil(r)
    offsets = [
        (dx, dy)
        for dy in range(-reach, reach + 1)
        for dx in range(-reach, reach + 1)
        if dx * dx + dy * dy <= r * r + 0.25
    ]
    value = np.asarray(color, dtype=np.float32)
    cx = np.round(centres[:, 0]).astype(np.int64)
    cy = np.round(centres[:, 1]).astype(np.int64)
    for dx, dy in offsets:
        x = cx + dx
        y = cy + dy
        ok = (x >= 0) & (x < width) & (y >= 0) & (y < height)
        canvas[y[ok], x[ok]] = value


def _polyline(
    canvas: Canvas,
    pts: NDArray[np.float64],
    radius: float,
    color: tuple[float, float, float],
    *,
    close: bool = False,
) -> None:
    if len(pts) == 0:
        return
    if close and len(pts) > 2:
        pts = np.vstack([pts, pts[:1]])
    if len(pts) == 1:
        _stamp(canvas, pts, radius, color)
        return
    seg = np.diff(pts, axis=0)
    lengths = np.hypot(seg[:, 0], seg[:, 1])
    steps = np.maximum(np.ceil(lengths / max(radius * 0.7, 0.5)).astype(np.int64), 1)
    steps = np.minimum(steps, 4000)
    parts = []
    for i, n in enumerate(steps):
        t = np.arange(n + 1, dtype=np.float64) / n
        parts.append(pts[i] + np.outer(t, seg[i]))
    _stamp(canvas, np.vstack(parts), radius, color)


def _disc(
    canvas: Canvas, centre: NDArray[np.float64], radius: float, color: tuple[float, float, float]
) -> None:
    _stamp(canvas, centre.reshape(1, 2), radius, color)


def render(scene: MapScene, width: int, height: int, theme: Theme = "light") -> bytes:
    """Draw ``scene`` and return the PNG. Empty scenes give the plain background."""
    palette = PALETTES[theme]
    w, h = clamp_size(width, height)
    big_w, big_h = w * SUPERSAMPLE, h * SUPERSAMPLE
    canvas: Canvas = np.empty((big_h, big_w, 3), dtype=np.float32)
    canvas[:] = np.asarray(palette.background, dtype=np.float32)
    bounds = _bounds(scene)
    if bounds is not None:
        view = _View(bounds, big_w, big_h)
        unit = SUPERSAMPLE
        for ring in scene.areas:
            _fill_polygon(canvas, view.px(_thin(ring, MAX_POINTS)), palette.area_fill)
        for ring in scene.no_go:
            _fill_polygon(canvas, view.px(_thin(ring, MAX_POINTS)), palette.zone_fill)
        for ring in scene.areas:
            _polyline(
                canvas, view.px(_thin(ring, MAX_POINTS)), 1.2 * unit, palette.area_line, close=True
            )
        budget = MAX_POINTS
        for line in scene.planned:
            part = _thin(line, max(budget, 2))
            budget -= len(part)
            _polyline(canvas, view.px(part), 0.8 * unit, palette.planned)
            if budget <= 0:
                break
        budget = MAX_POINTS
        for line in scene.mowed:
            part = _thin(line, max(budget, 2))
            budget -= len(part)
            _polyline(canvas, view.px(part), 1.4 * unit, palette.mowed)
            if budget <= 0:
                break
        if scene.dock is not None:
            centre = view.px([scene.dock])[0]
            _disc(canvas, centre, 5.0 * unit, palette.dock)
            _disc(canvas, centre, 2.5 * unit, palette.background)
        if scene.mower is not None:
            centre = view.px([scene.mower])[0]
            _disc(canvas, centre, 8.5 * unit, palette.mower_ring)
            _disc(canvas, centre, 6.0 * unit, palette.mower)
    small = canvas.reshape(h, SUPERSAMPLE, w, SUPERSAMPLE, 3).mean(axis=(1, 3))
    return encode_png(np.clip(np.rint(small), 0, 255).astype(np.uint8))


def encode_png(rgb: NDArray[np.uint8]) -> bytes:
    """An 8 bit RGB PNG (filter 0 per row, zlib level 6)."""
    height, width = rgb.shape[:2]
    raw = np.concatenate([np.zeros((height, 1), dtype=np.uint8), rgb.reshape(height, -1)], axis=1)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
        + chunk(b"IEND", b"")
    )


def decode_png_size(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG header (tests and sanity checks)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        msg = "not a PNG"
        raise ValueError(msg)
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)
