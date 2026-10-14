"""Render the app icons (PNG and SVG) without dependencies.

The mark: the mower marker of the map (a chevron in the "mowing" signal green) on graphite with
faint mowing stripes. Run from apps/web: ``python scripts/render-icons.py`` (writes static/icons).
"""

import struct
import zlib
from pathlib import Path

INK = (0x11, 0x14, 0x12)
STRIPE = (0x1A, 0x1E, 0x1C)
GREEN = (0x56, 0xD3, 0x7A)
CHEVRON = [(0.5, 0.22), (0.78, 0.76), (0.5, 0.62), (0.22, 0.76)]
STRIPES = 6
OUT = Path("static/icons")


def inside(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    found = False
    j = len(poly) - 1
    for i, (xi, yi) in enumerate(poly):
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            found = not found
        j = i
    return found


def scaled(poly: list[tuple[float, float]], factor: float) -> list[tuple[float, float]]:
    return [(0.5 + (x - 0.5) * factor, 0.5 + (y - 0.5) * factor) for x, y in poly]


def render(size: int, scale: float) -> bytes:
    poly = scaled(CHEVRON, scale)
    samples = 4
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            stripe = int(px / size * STRIPES) % 2 == 1
            base = STRIPE if stripe else INK
            hits = 0
            for sy in range(samples):
                for sx in range(samples):
                    x = (px + (sx + 0.5) / samples) / size
                    y = (py + (sy + 0.5) / samples) / size
                    hits += inside(x, y, poly)
            a = hits / samples**2
            row.extend(round(b * (1 - a) + g * a) for b, g in zip(base, GREEN, strict=True))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def svg() -> str:
    width = 100 / STRIPES
    stripes = "".join(
        f'<rect x="{i * width:.3f}" width="{width:.3f}" height="100" fill="#1a1e1c"/>'
        for i in range(1, STRIPES, 2)
    )
    points = " ".join(f"{x * 100:g},{y * 100:g}" for x, y in CHEVRON)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<rect width="100" height="100" fill="#111412"/>{stripes}'
        f'<polygon points="{points}" fill="#56d37a"/></svg>\n'
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "icon.svg").write_text(svg(), encoding="utf-8")
    for name, size, scale in (
        ("icon-192.png", 192, 1.0),
        ("icon-512.png", 512, 1.0),
        ("icon-maskable-512.png", 512, 0.72),
        ("apple-touch-icon.png", 180, 0.86),
    ):
        (OUT / name).write_bytes(render(size, scale))
        print(f"wrote {OUT / name}")


if __name__ == "__main__":
    main()
