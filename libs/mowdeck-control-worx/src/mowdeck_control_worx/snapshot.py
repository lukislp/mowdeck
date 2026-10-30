"""One Worx mower as the cloud describes it, read from plain JSON (no vendor SDK).

The cloud lists an account's mowers at ``GET /api/v2/product-items`` (one "product item" each:
serial number, name, model, ``online``, ``protocol``, MQTT topics) and keeps the mower's last
MQTT report in ``last_status.payload``: ``cfg`` (its configuration) and ``dat`` (its live data).
pyworxcloud updates that payload in place with every MQTT message, so a :class:`WorxMower` read
from the product item is always the newest report. The field names follow the reverse
engineered protocol as pyworxcloud 6.4.2 decodes it:

========  ============================================================================
``dat``   ``ls`` status code, ``le`` error code, ``bt`` battery (``p`` percent, ``c``
          charging 0/1/2, ``nr`` charge cycles), ``rsi`` Wi-Fi dBm, ``lz`` index of the
          last zone, ``st`` statistics (``b`` blade minutes, ``d`` metres, ``wt`` work
          minutes), ``rain`` (``s`` sensor wet, ``cnt`` minutes left), ``modules``
          (``4G.gps.coo`` position, ``EA`` cutting height module), ``rtk.pos``, ``fw``
          firmware, ``lk`` locked
``cfg``   ``rd`` rain delay in minutes, ``mz`` the four zone starting points in metres
          (0: not set), ``mzv`` the ten-slot zone rotation, ``modules.EA.h`` cutting height
          in mm, ``sc`` schedule (``ots``/``once``: one-time schedule and edge cut)
========  ============================================================================

Everything is optional: models and firmware report different subsets, and a mower that never
reported has no payload at all.
"""

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

ZONE_SLOTS: Final = 4
"""A wire-guided mower has four zone starting points (``cfg.mz``)."""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@dataclass(frozen=True, slots=True)
class WorxMower:
    """The parts of one product item MowDeck reads (see the module docstring)."""

    serial_number: str
    name: str
    model: str | None = None
    online: bool = False
    protocol: int = 0
    cfg: Mapping[str, Any] = field(default_factory=dict)
    dat: Mapping[str, Any] = field(default_factory=dict)
    api_capabilities: tuple[str, ...] = ()
    """The cloud's own capability list of the product item (``border_cut``, ...)."""

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> "WorxMower":
        """Read a product item; raises ``ValueError`` without a serial number."""
        serial = item.get("serial_number")
        if not isinstance(serial, str | int) or not str(serial).strip():
            msg = "a product item without a serial number"
            raise ValueError(msg)
        model = _dict(item.get("model"))
        friendly = model.get("friendly_name")
        payload = _dict(_dict(item.get("last_status")).get("payload"))
        caps = item.get("capabilities")
        name = item.get("name")
        return cls(
            serial_number=str(serial).strip(),
            name=name.strip() if isinstance(name, str) and name.strip() else "No Name",
            model=friendly.strip()[:100]
            if isinstance(friendly, str) and friendly.strip()
            else None,
            online=item.get("online") is True,
            protocol=_int(item.get("protocol")) or 0,
            cfg=copy.deepcopy(_dict(payload.get("cfg"))),
            dat=copy.deepcopy(_dict(payload.get("dat"))),
            api_capabilities=tuple(c for c in caps if isinstance(c, str))
            if isinstance(caps, list)
            else (),
        )

    # --- live data ---------------------------------------------------------------------------

    @property
    def status_code(self) -> int | None:
        """``dat.ls``: what the mower does (``STATUS_TEXTS`` of the lexicon)."""
        return _int(self.dat.get("ls"))

    @property
    def error_code(self) -> int:
        """``dat.le``: the error code, 0 for none (also when not reported)."""
        return _int(self.dat.get("le")) or 0

    @property
    def battery_percent(self) -> int | None:
        value = _int(_dict(self.dat.get("bt")).get("p"))
        return None if value is None else max(0, min(100, value))

    @property
    def charging(self) -> bool:
        """``dat.bt.c`` is 1 (2 is a charging error, 0 not charging)."""
        return _int(_dict(self.dat.get("bt")).get("c")) == 1

    @property
    def charge_cycles(self) -> int | None:
        return _int(_dict(self.dat.get("bt")).get("nr"))

    @property
    def wifi_dbm(self) -> int | None:
        value = _int(self.dat.get("rsi"))
        return value if value is not None and -150 <= value <= -1 else None

    @property
    def firmware(self) -> str | None:
        value = self.dat.get("fw")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return f"{value:g}"
        if isinstance(value, str) and value.strip():
            return value.strip()[:64]
        return None

    @property
    def statistics(self) -> Mapping[str, Any]:
        return _dict(self.dat.get("st"))

    @property
    def rain_sensor_wet(self) -> bool | None:
        rain = self.dat.get("rain")
        if not isinstance(rain, dict) or "s" not in rain:
            return None
        return str(rain.get("s")) == "1"

    @property
    def rain_remaining_min(self) -> int | None:
        return _int(_dict(self.dat.get("rain")).get("cnt"))

    @property
    def locked(self) -> bool | None:
        value = self.dat.get("lk")
        return None if value is None else bool(_int(value))

    @property
    def gps(self) -> tuple[float, float, str] | None:
        """``(lat, lon, source)``: the GPS module (``4g``) or an RTK model's position
        (``rtk``); None without one or for the 0/0 a module reports before its first fix."""
        coo = _dict(_dict(_dict(self.dat.get("modules")).get("4G")).get("gps")).get("coo")
        found: tuple[Any, Any, str] | None = None
        if isinstance(coo, list | tuple) and len(coo) >= 2:
            found = (coo[0], coo[1], "4g")
        else:
            pos = _dict(self.dat.get("rtk")).get("pos")
            if isinstance(pos, list | tuple) and len(pos) >= 2:
                found = (pos[0], pos[1], "rtk")
        if found is None:
            return None
        lat, lon = _float(found[0]), _float(found[1])
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        if lat == 0 and lon == 0:
            return None
        return lat, lon, found[2]

    # --- configuration -----------------------------------------------------------------------

    @property
    def rain_delay_min(self) -> int | None:
        """``cfg.rd``: minutes the mower waits after rain before it mows again."""
        value = _int(self.cfg.get("rd"))
        return value if value is not None and value >= 0 else None

    @property
    def has_cutting_height(self) -> bool:
        """The electronic cutting height module (``EA``) is fitted."""
        modules = self.dat.get("modules")
        return isinstance(modules, dict) and "EA" in modules

    @property
    def cutting_height_mm(self) -> int | None:
        height = _int(_dict(_dict(self.cfg.get("modules")).get("EA")).get("h"))
        return height if height is not None and 0 < height <= 200 else None

    @property
    def has_edge_cut(self) -> bool:
        """The one-time schedule (and with it the edge cut on demand) is offered, the way
        pyworxcloud's capability check decides it."""
        schedule_cfg, schedule_dat = _dict(self.cfg.get("sc")), _dict(self.dat.get("sc"))
        one_time = {"ots", "once"}
        return (
            bool(one_time & set(schedule_cfg))
            or bool(one_time & set(schedule_dat))
            or "border_cut" in self.api_capabilities
        )

    @property
    def zone_starts(self) -> list[int]:
        """``cfg.mz``: the starting points in metres, padded to four (0: not set)."""
        raw = self.cfg.get("mz")
        points = [(_int(v) or 0) for v in raw] if isinstance(raw, list) else []
        return (points + [0] * ZONE_SLOTS)[:ZONE_SLOTS]

    @property
    def zone_rotation(self) -> list[int]:
        """``cfg.mzv``: which zone each of the ten starts uses."""
        raw = self.cfg.get("mzv")
        return [v for v in (_int(x) for x in raw) if v is not None] if isinstance(raw, list) else []

    @property
    def zones(self) -> list[int]:
        """Zone indices (0 to 3) a start can be sent to: a starting point is set and the zone
        is in the rotation (pyworxcloud's ``setzone`` refuses the others)."""
        starts, rotation = self.zone_starts, set(self.zone_rotation)
        usable = [z for z in range(ZONE_SLOTS) if starts[z] > 0 and z in rotation]
        return usable if len(usable) > 1 else []

    @property
    def current_zone(self) -> int | None:
        """The zone of the last or running start (``dat.lz`` indexes the rotation)."""
        index, rotation = _int(self.dat.get("lz")), self.zone_rotation
        if index is None or not 0 <= index < len(rotation):
            return None
        return rotation[index]
