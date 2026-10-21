"""The polyline codec of spot plans (libs/mowdeck-contracts, ``StartMow.spot_plan``).

A spot plan arrives in a start command and the Device-Gateway drives the mower along its
decoded segments, so the decoder sees whatever a client put in the command. Properties:

* ``decode_polyline`` raises nothing but ``ValueError`` for any string;
* a decoded polyline encodes to a string that decodes to the same points (the canonical form),
  and encoding is stable from then on;
* ``encode_polyline`` refuses (``ValueError``) only coordinates that are not finite or too
  large to decode again, and points it encodes decode to within half a centimetre.
"""

import math
import sys

import atheris

with atheris.instrument_imports():
    from mowdeck_contracts.v1 import spot

from harness import Input

# Beyond this many metres the float of a decoded value no longer holds every centimetre.
_EXACT = 1e9


def _check_decode(text: str) -> None:
    try:
        points = spot.decode_polyline(text)
    except ValueError:
        return
    if any(abs(a) > _EXACT or abs(b) > _EXACT for a, b in points):
        return
    encoded = spot.encode_polyline(points)
    assert spot.decode_polyline(encoded) == points
    assert spot.encode_polyline(spot.decode_polyline(encoded)) == encoded


def _encodable(value: float) -> bool:
    return math.isfinite(value) and abs(value) * 10**spot.SPOT_PRECISION < 2.0**62


def _check_encode(inp: Input) -> None:
    points = [(inp.number(), inp.number()) for _ in range(inp.int_in(0, 16))]
    try:
        encoded = spot.encode_polyline(points)
    except ValueError:
        # Only for a coordinate that is not finite or too large to decode again.
        assert not all(_encodable(a) and _encodable(b) for a, b in points)
        return
    decoded = spot.decode_polyline(encoded)
    assert len(decoded) == len(points)
    for (a, b), (x, y) in zip(points, decoded, strict=True):
        if abs(a) < _EXACT and abs(b) < _EXACT:
            assert abs(a - x) <= 0.005 + 1e-9 * abs(a)
            assert abs(b - y) <= 0.005 + 1e-9 * abs(b)


def test_one_input(data: bytes) -> None:
    inp = Input(data)
    if inp.uint(1) & 1:
        _check_encode(inp)
    else:
        _check_decode(inp.rest().decode("utf-8", "replace"))


def seeds() -> list[bytes]:
    """Encoded lanes and points to encode."""
    lane = [(0.0, 0.0), (12.5, 0.0), (12.5, 0.4), (0.0, 0.4), (-3.21, 7.89)]
    return [
        b"\x00" + spot.encode_polyline(lane).encode("ascii"),
        b"\x00" + spot.encode_polyline([(1e6, -1e6), (0.01, 0.01)]).encode("ascii"),
        b"\x01\x05" + bytes(range(200, 255)) * 2,
    ]


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
