"""A fake aerial photo WMS for the Playwright suite (ADR 0053): GetMap in EPSG:3857.

Test stacks only (infra/compose/docker-compose.e2e.yml runs it with the identity image; standard
library only; no internet). It paints a synthetic garden at the places where the simulator's demo
map (origin 48.137154, 11.576124, rotated 12.5 degrees) has its areas, so a spec and a review
screenshot can see whether the photo lines up with the outlines drawn on top:

* the front lawn (0..12 x 0..8 m) and the back lawn (0..10 x 11..17 m), mown stripes
* the flower bed (9..12 x 5.5..8 m) in the front lawn's corner
* a house (dark roof) 1..9 m west of the front lawn, a terrace, a hedge and a tree

``GET /wms?SERVICE=WMS&REQUEST=GetMap&LAYERS=fake_dop&CRS=EPSG:3857&BBOX=...&WIDTH=&HEIGHT=``
answers a PNG; every request is counted at ``GET /fake-wms/stats`` (the spec checks that tiles
are cached and that nothing outside the map is asked for). ``GET /fake-wms/healthz`` for health.
"""

import json
import math
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ORIGIN_LAT = 48.137154
ORIGIN_LON = 11.576124
ROTATION_DEG = 12.5
EARTH_RADIUS_M = 6_378_137.0
WORLD_HALF_M = 20_037_508.342789244

_lock = threading.Lock()
_stats = {"get_map": 0, "bboxes": []}


def mercator_to_local(x: float, y: float) -> tuple[float, float]:
    """The map's local metres of an EPSG:3857 point: the inverse of Telemetry's to_wgs84."""
    lon = x / WORLD_HALF_M * 180.0
    lat = math.degrees(2 * math.atan(math.exp(y / WORLD_HALF_M * math.pi)) - math.pi / 2)
    north = math.radians(lat - ORIGIN_LAT) * EARTH_RADIUS_M
    east = math.radians(lon - ORIGIN_LON) * EARTH_RADIUS_M * math.cos(math.radians(ORIGIN_LAT))
    r = math.radians(ROTATION_DEG)
    return east * math.cos(r) + north * math.sin(r), -east * math.sin(r) + north * math.cos(r)


def colour(lx: float, ly: float) -> tuple[int, int, int]:
    """The synthetic photo at a local point."""
    if 9 <= lx <= 12 and 5.5 <= ly <= 8:  # flower bed
        return (150 + int(40 * ((int(lx * 4) + int(ly * 4)) % 2)), 70, 90)
    if 0 <= lx <= 12 and 0 <= ly <= 8:  # front lawn, stripes every 0.8 m
        return (96, 150, 70) if int(ly / 0.8) % 2 else (84, 138, 62)
    if 0 <= lx <= 10 and 11 <= ly <= 17:  # back lawn
        return (92, 146, 66) if int(lx / 0.8) % 2 else (80, 134, 58)
    if -9 <= lx <= -1 and -2 <= ly <= 10:  # house roof, ridge in the middle
        return (120, 60, 52) if lx > -5 else (96, 48, 42)
    if -1 < lx < 0 and 0 <= ly <= 8:  # terrace
        return (176, 170, 160)
    if (lx - 15) ** 2 + (ly - 12) ** 2 <= 2.6**2:  # tree crown
        return (38, 82, 40)
    if -11 <= lx <= 14 and -4 <= ly < -2:  # hedge
        return (52, 98, 48)
    # surroundings: a calm meadow with a little texture
    n = (int(lx * 3) * 7 + int(ly * 3) * 13) % 5
    return (112 + n * 2, 134 + n * 2, 88 + n)


def png(width: int, height: int, rows: list[bytes]) -> bytes:
    """A minimal RGB PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def render(bbox: tuple[float, float, float, float], width: int, height: int) -> bytes:
    min_x, min_y, max_x, max_y = bbox
    rows = []
    for j in range(height):
        y = max_y - (j + 0.5) * (max_y - min_y) / height
        row = bytearray()
        for i in range(width):
            x = min_x + (i + 0.5) * (max_x - min_x) / width
            row.extend(colour(*mercator_to_local(x, y)))
        rows.append(bytes(row))
    return png(width, height, rows)


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-wms"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/fake-wms/healthz":
            self._send(200, b"ok", "text/plain")
            return
        if parts.path == "/fake-wms/stats":
            with _lock:
                self._send(200, json.dumps(_stats).encode(), "application/json")
            return
        if parts.path != "/wms":
            self._send(404, b"not found", "text/plain")
            return
        q = {k.upper(): v[0] for k, v in parse_qs(parts.query).items()}
        try:
            if (
                q.get("REQUEST") != "GetMap"
                or q.get("LAYERS") != "fake_dop"
                or q.get("CRS") != "EPSG:3857"
            ):
                raise ValueError("unsupported request")
            bbox = tuple(float(v) for v in q["BBOX"].split(","))
            width, height = int(q["WIDTH"]), int(q["HEIGHT"])
            if len(bbox) != 4 or not (16 <= width <= 1024 and 16 <= height <= 1024):
                raise ValueError("bad size")
        except (KeyError, ValueError):
            self._send(
                200,
                b"<ServiceExceptionReport><ServiceException/></ServiceExceptionReport>",
                "text/xml",
            )
            return
        with _lock:
            _stats["get_map"] += 1
            _stats["bboxes"].append(list(bbox))
        self._send(200, render(bbox, width, height), "image/png")  # type: ignore[arg-type]


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
