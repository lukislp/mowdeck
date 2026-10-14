"""Still images without a camera: the simulator's "photo" of its surroundings.

:func:`render_scene` draws a top-down view of the map around the mower (areas, no-go zones,
obstacles, paths, the dock and the mower with its heading), centred on the mower and rotated
so that the mower faces up, like a camera mounted on it would see the ground ahead. Pillow is
optional (``mowdeck-control[photo]``): without it the image is a flat PNG written with the
standard library, which is still a valid image for the end to end tests of AI assist.
"""

import io
import math
import struct
import zlib
from collections.abc import Sequence

from mowdeck_contracts.v1 import LocalPoint, MowerMap

from mowdeck_control.connection import Photo

WIDTH = 480
HEIGHT = 360
METRES_ACROSS = 10.0
"""Width of the view in metres."""

_GRASS = (74, 140, 66)
_SOIL = (120, 104, 80)
_NO_GO = (196, 64, 52)
_OBSTACLE = (90, 70, 50)
_PATH = (170, 170, 160)
_DOCK = (40, 90, 200)
_MOWER = (245, 245, 245)
_WARN = (250, 160, 30)


def flat_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A PNG of one colour, written with the standard library only."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def render_scene(
    mower_map: MowerMap,
    position: LocalPoint,
    theta: float,
    *,
    stuck: bool = False,
    width: int = WIDTH,
    height: int = HEIGHT,
    brightness: float = 1.0,
) -> Photo:
    """A top-down image around ``position`` with the mower heading ``theta`` facing up.

    ``theta`` is the local direction in radians (counter-clockwise from +x). ``brightness``
    (0..1) scales every pixel, so the simulator can take night shots. JPEG with Pillow, a flat
    PNG without it.
    """
    level = max(0.0, min(1.0, brightness))
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover - exercised only without Pillow
        soil = (int(_SOIL[0] * level), int(_SOIL[1] * level), int(_SOIL[2] * level))
        return Photo(flat_png(width, height, soil), "image/png")
    scale = width / METRES_ACROSS
    # Rotate the world so that the mower's heading points up (screen -y).
    rotation = math.pi / 2 - theta
    cos_r, sin_r = math.cos(rotation), math.sin(rotation)
    cx, cy = width / 2, height * 0.62

    def screen(point: LocalPoint) -> tuple[float, float]:
        dx, dy = point[0] - position[0], point[1] - position[1]
        rx, ry = dx * cos_r - dy * sin_r, dx * sin_r + dy * cos_r
        return (cx + rx * scale, cy - ry * scale)

    def polygon(points: Sequence[LocalPoint]) -> list[tuple[float, float]]:
        return [screen(p) for p in points]

    image = Image.new("RGB", (width, height), _SOIL)
    draw = ImageDraw.Draw(image)
    for area in mower_map.areas:
        draw.polygon(polygon(area.boundary), fill=_GRASS)
    for path in mower_map.paths:
        draw.line(polygon(path.points), fill=_PATH, width=max(2, int(0.4 * scale)))
    for zone in mower_map.no_go_zones:
        draw.polygon(polygon(zone.boundary), fill=_NO_GO)
    for zone in mower_map.obstacles:
        draw.polygon(polygon(zone.boundary), fill=_OBSTACLE)
    dock = mower_map.dock
    if dock is not None:
        heading = math.radians(dock.heading_deg)
        half = 0.35
        corners = [
            (dock.x + dx * math.cos(heading) - dy * math.sin(heading),
             dock.y + dx * math.sin(heading) + dy * math.cos(heading))
            for dx, dy in ((-half, -half), (half, -half), (half, half), (-half, half))
        ]  # fmt: skip
        draw.polygon(polygon(corners), fill=_DOCK)
    # The mower: a triangle pointing up (its heading), about 0.6 x 0.4 m.
    tip = (cx, cy - 0.35 * scale)
    left = (cx - 0.22 * scale, cy + 0.25 * scale)
    right = (cx + 0.22 * scale, cy + 0.25 * scale)
    draw.polygon([tip, left, right], fill=_MOWER, outline=(30, 30, 30))
    if stuck:
        size = 0.25 * scale
        x0, y0 = cx, cy - 0.7 * scale
        draw.line([(x0 - size, y0 - size), (x0 + size, y0 + size)], fill=_WARN, width=4)
        draw.line([(x0 - size, y0 + size), (x0 + size, y0 - size)], fill=_WARN, width=4)
    if level < 1.0:
        image = image.point(lambda value: int(value * level))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return Photo(buffer.getvalue(), "image/jpeg")
