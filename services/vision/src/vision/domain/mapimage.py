"""A small top-down map rendering for the vision model (Pillow, no browser, no I/O).

The image is in the **mower frame, heading up**: the mower sits in the centre and points to the
top edge, like the ``forward``/``left`` geometry of the scene context; with an unknown heading
the map's local +x axis points up (``heading_known`` is false then). One PNG of
:data:`MAP_IMAGE_SIZE` pixels square, flat colours and short labels only:

* areas dark green with a light green edge and their name; transfer paths orange;
* no-go zones and obstacles red;
* the recent track cyan dots; the dock a blue square labelled ``DOCK`` (an arrow on the edge
  with the distance when it is outside the view); the mower a magenta triangle;
* a scale bar bottom left and a north arrow top right.

It uses only the generic map model (areas, zones, paths, dock), so it works for every mower
brand. Pillow comes with ``mowdeck-control[photo]``; without it :func:`render_map` returns None.
"""

import io
import math
from collections.abc import Sequence
from typing import Any, Final

from mowdeck_contracts.v1 import LocalPoint, MowerMap

from vision.domain.context import Pose, to_mower_frame

MAP_IMAGE_SIZE: Final = 512
MIN_HALF_SPAN_M: Final = 8.0
MAX_HALF_SPAN_M: Final = 40.0

BACKGROUND: Final = (32, 32, 32)
AREA_FILL: Final = (46, 107, 52)
AREA_EDGE: Final = (140, 210, 140)
PATH: Final = (240, 160, 48)
NO_GO: Final = (200, 40, 40)
TRACK: Final = (48, 200, 230)
DOCK: Final = (48, 128, 255)
MOWER: Final = (255, 64, 255)
TEXT: Final = (255, 255, 255)

CAPTION: Final = (
    "Top-down map around the mower, heading up (the mower's forward direction is the top edge, "
    "its left is the left edge). Magenta triangle: the mower. Blue square DOCK: the charging "
    "station (an arrow on the edge when it is further away). Green: mapped areas with their "
    "names. Orange lines: mapped transfer paths. Red: no-go zones and obstacles. Cyan dots: "
    "where the mower was recently. Scale bar bottom left, N arrow: north."
)


class _View:
    """Mower frame (forward, left) in metres to pixels."""

    def __init__(self, pose: Pose, half_span_m: float, size: int) -> None:
        self.pose = pose
        self.size = size
        self.scale = size / (2 * half_span_m)
        self.centre = size / 2

    def pixel(self, point: LocalPoint) -> tuple[float, float]:
        forward, left = to_mower_frame(point, self.pose)
        return (self.centre - left * self.scale, self.centre - forward * self.scale)

    def inside(self, xy: tuple[float, float], margin: float = 0.0) -> bool:
        return margin <= xy[0] <= self.size - margin and margin <= xy[1] <= self.size - margin


def half_span(mower_map: MowerMap, pose: Pose, track: Sequence[LocalPoint] = ()) -> float:
    """Half the width of the view in metres: the dock and the track fit, within bounds."""
    reach = MIN_HALF_SPAN_M
    here = (pose.x, pose.y)
    points: list[LocalPoint] = list(track)
    if mower_map.dock is not None:
        points.append((mower_map.dock.x, mower_map.dock.y))
    for p in points:
        reach = max(reach, math.dist(here, p) * 1.15 + 2.0)
    return min(MAX_HALF_SPAN_M, reach)


def _nice_length(metres: float) -> float:
    for step in (1, 2, 5, 10, 20, 50):
        if step >= metres:
            return float(step)
    return 50.0


def render_map(
    mower_map: MowerMap,
    pose: Pose,
    *,
    track: Sequence[LocalPoint] = (),
    size: int = MAP_IMAGE_SIZE,
) -> bytes | None:
    """The PNG of the map around ``pose`` (see the module docstring); None without Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:  # pragma: no cover - Pillow comes with mowdeck-control[photo]
        return None
    view = _View(pose, half_span(mower_map, pose, track), size)
    image = Image.new("RGB", (size, size), BACKGROUND)
    draw = ImageDraw.Draw(image)
    font: Any = ImageFont.load_default()

    for area in mower_map.areas:
        ring = [view.pixel(p) for p in area.boundary]
        draw.polygon(ring, fill=AREA_FILL, outline=AREA_EDGE, width=2)
    for zone in mower_map.forbidden_zones():
        draw.polygon([view.pixel(p) for p in zone.boundary], fill=NO_GO, outline=NO_GO)
    for path in mower_map.paths:
        draw.line([view.pixel(p) for p in path.points], fill=PATH, width=3)
    for area in mower_map.areas:
        if area.name:
            cx = sum(p[0] for p in area.boundary) / len(area.boundary)
            cy = sum(p[1] for p in area.boundary) / len(area.boundary)
            xy = view.pixel((cx, cy))
            if view.inside(xy, 20):
                draw.text(xy, area.name[:24], fill=TEXT, font=font, anchor="mm")
    for p in track:
        x, y = view.pixel(p)
        draw.ellipse((x - 2.5, y - 2.5, x + 2.5, y + 2.5), fill=TRACK)

    if mower_map.dock is not None:
        _draw_dock(draw, font, view, (mower_map.dock.x, mower_map.dock.y), pose)
    _draw_mower(draw, view)
    _draw_scale(draw, font, view)
    _draw_north(draw, font, view, mower_map, pose)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _draw_dock(draw: Any, font: Any, view: _View, dock: LocalPoint, pose: Pose) -> None:
    x, y = view.pixel(dock)
    if view.inside((x, y), 8):
        half = max(5.0, 0.4 * view.scale)
        draw.rectangle((x - half, y - half, x + half, y + half), fill=DOCK, outline=TEXT)
        draw.text((x, y - half - 3), "DOCK", fill=TEXT, font=font, anchor="md")
        return
    # Outside the view: an arrow on the edge towards it, with the distance.
    c = view.centre
    dx, dy = x - c, y - c
    reach = (c - 18) / max(abs(dx), abs(dy))
    ex, ey = c + dx * reach, c + dy * reach
    angle = math.atan2(dy, dx)
    tip = (ex + 10 * math.cos(angle), ey + 10 * math.sin(angle))
    wing = 0.5
    left = (ex + 6 * math.cos(angle + math.pi - wing), ey + 6 * math.sin(angle + math.pi - wing))
    right = (ex + 6 * math.cos(angle + math.pi + wing), ey + 6 * math.sin(angle + math.pi + wing))
    draw.polygon([tip, left, right], fill=DOCK, outline=TEXT)
    metres = math.dist((pose.x, pose.y), dock)
    draw.text((ex, ey), f"DOCK {metres:.0f} m", fill=TEXT, font=font, anchor="mm")


def _draw_mower(draw: Any, view: _View) -> None:
    c = view.centre
    length = max(14.0, 0.7 * view.scale)
    width = max(10.0, 0.6 * view.scale)
    draw.polygon(
        [(c, c - length / 2), (c - width / 2, c + length / 2), (c + width / 2, c + length / 2)],
        fill=MOWER,
        outline=TEXT,
    )


def _draw_scale(draw: Any, font: Any, view: _View) -> None:
    metres = _nice_length(view.size / view.scale / 5)
    pixels = metres * view.scale
    x0, y = 12.0, view.size - 14.0
    draw.rectangle((x0, y - 3, x0 + pixels, y), fill=TEXT)
    draw.text((x0, y - 6), f"{metres:g} m", fill=TEXT, font=font, anchor="ld")


def _draw_north(draw: Any, font: Any, view: _View, mower_map: MowerMap, pose: Pose) -> None:
    rotation = math.radians(mower_map.reference.rotation_deg)
    # North in the local frame is (sin r, cos r) (see GeoReference); as a pixel direction:
    north = (pose.x + math.sin(rotation), pose.y + math.cos(rotation))
    px, py = view.pixel(north)
    dx, dy = px - view.centre, py - view.centre
    norm = math.hypot(dx, dy) or 1.0
    ux, uy = dx / norm, dy / norm
    cx, cy = view.size - 26.0, 26.0
    draw.line((cx - ux * 12, cy - uy * 12, cx + ux * 6, cy + uy * 6), fill=TEXT, width=2)
    tip = (cx + ux * 13, cy + uy * 13)
    base = (cx + ux * 5, cy + uy * 5)
    draw.polygon(
        [tip, (base[0] - uy * 5, base[1] + ux * 5), (base[0] + uy * 5, base[1] - ux * 5)],
        fill=TEXT,
    )
    draw.text((cx + ux * 20, cy + uy * 20), "N", fill=TEXT, font=font, anchor="mm")
