"""Print golden vectors of ``mowdeck_control.geo`` as JSON for the TypeScript port.

The web app converts the map's local metres to WGS84 in the browser
(``libs/mowdeck-ui/src/lib/map/geo.ts``). Its Vitest test compares the port with these vectors,
so the two implementations cannot drift apart. Regenerate after a change to ``geo.py``, from
the repository root (the JSON goes to stdout, or to the file given as the only argument)::

    uv run python libs/mowdeck-ui/scripts/geo_vectors.py \
        libs/mowdeck-ui/src/tests/fixtures/geo-vectors.json
"""

import json
import math
import sys
from pathlib import Path

from mowdeck_control import default_map, geo

ROTATIONS = (0.0, 12.5, -90.0, 359.0)
LATITUDES = (0.0, 48.137154, 70.0, -33.9)
LONGITUDES = (0.0, 11.576124, 25.5, 151.2)
POINTS = (
    (0.0, 0.0),
    (1.0, 0.0),
    (0.0, 1.0),
    (-1.0, 1.0),
    (12.0, 8.0),
    (10.0, 17.0),
    (-37.25, 81.5),
    (250.0, -120.0),
)
ANGLES_RAD = (0.0, math.pi / 2, math.pi, -math.pi / 4, 2.5)


def main() -> None:
    template = default_map().reference
    cases = []
    for rotation in ROTATIONS:
        for lat0, lon0 in zip(LATITUDES, LONGITUDES, strict=True):
            ref = template.model_copy(
                update={"origin_lat": lat0, "origin_lon": lon0, "rotation_deg": rotation}
            )
            points = []
            for x, y in POINTS:
                lat, lon = geo.to_wgs84(ref, x, y)
                back_x, back_y = geo.from_wgs84(ref, lat, lon)
                points.append({"x": x, "y": y, "lat": lat, "lon": lon, "bx": back_x, "by": back_y})
            headings = [
                {"angle_rad": a, "heading": geo.compass_heading(ref, a)} for a in ANGLES_RAD
            ]
            cases.append(
                {
                    "reference": {
                        "origin_lat": lat0,
                        "origin_lon": lon0,
                        "rotation_deg": rotation,
                    },
                    "points": points,
                    "headings": headings,
                }
            )
    text = json.dumps({"source": "mowdeck_control.geo", "cases": cases}, indent=1) + "\n"
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
