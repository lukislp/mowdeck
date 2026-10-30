"""Shared test data: product items shaped like the cloud's (``tests/fixtures``)."""

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any

from mowdeck_control_worx.snapshot import WorxMower

FIXTURES = Path(__file__).resolve().parent / "fixtures"

WIRE = "20213026640700012345"
"""A wire-guided mower with GPS and cutting height modules, two zones and the edge cut."""
PLAIN = "20193018320500054321"
"""A wire-guided mower without modules or zones, docked and charging, rain sensor wet."""
VISION = "20243050010100099999"
"""A camera model (protocol 1) with an RTK position, lifted."""
SILENT = "20183017010100011111"
"""An offline mower that never reported (no name, no payload)."""


@cache
def _items() -> dict[str, dict[str, Any]]:
    data = json.loads((FIXTURES / "product_items.json").read_text("utf-8"))
    return {str(item["serial_number"]): item for item in data}


def item(serial: str) -> dict[str, Any]:
    """A fresh copy of one fixture product item."""
    return copy.deepcopy(_items()[serial])


def all_items() -> list[dict[str, Any]]:
    return [item(serial) for serial in _items()]


def mower(serial: str = WIRE, *, online: bool | None = None, **dat: Any) -> WorxMower:
    """The fixture mower ``serial`` with ``dat`` fields replaced (None removes one)."""
    data = item(serial)
    if online is not None:
        data["online"] = online
    payload = (data.get("last_status") or {}).get("payload")
    if dat:
        assert payload is not None, "the fixture has no payload"
        for key, value in dat.items():
            if value is None:
                payload["dat"].pop(key, None)
            else:
                payload["dat"][key] = value
    return WorxMower.from_item(data)
