"""Heatmap layers over the mower's local map frame (pure computation, no I/O).

Positions arrive as WGS84 rows of the track; they are converted to the map's local metres with
the map's :class:`~mowdeck_contracts.v1.GeoReference` and binned into square cells of
``cell_m`` metres. A cell index is ``floor(metres / cell_m)`` on each axis, so ``(-1, -1)`` is
the cell just south-west of the origin.

Layers:

* ``coverage``: the share of each cell the blades cut. Every blades-on position is a disc of the
  cutting width; consecutive blades-on positions at most ``join_seconds`` and at most
  ``join_m`` apart (both limits; defaults :data:`DEFAULT_JOIN_SECONDS` and
  :data:`DEFAULT_JOIN_M`, settings of the service) are joined by a straight swath. The cloud
  reports a mowing LUBA every few seconds up to 20 s, so the window is wide; a longer silence or
  a larger jump (RTK jump, the mower carried) leaves separate discs. The ground is rasterised
  into pixels of at most a quarter of the cutting width (and :data:`MAX_PIXEL_M`): a pixel is
  cut when its centre lies within half the cutting width of the swath. The share of a cell is
  its cut pixels over all its pixels (a vector union of 200 000 overlapping swath points takes
  minutes; the raster about a second). ``gaps`` are the map's areas (minus no-go zones and
  obstacles) minus the cut pixels, from
  ``mowdeck_patterns.coverage.gaps`` (pieces below :data:`MIN_GAP_AREA_M2` dropped).
* ``rtk``: the share of samples with a fixed solution (float counts half); samples whose fix is
  ``unknown`` are left out.
* ``wifi`` and ``mnet``: the median signal in dBm; cells without a reading are left out.

A response holds at most :data:`MAX_CELLS` cells: with more, the cell size doubles until they
fit, and the answer says which size it used.

The builder takes rows in either time order (the query reads newest first) and keeps only what
its layer needs, so a long track is streamed through it in chunks.
"""

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import shapely
from mowdeck_contracts.v1 import GeoReference, MowerMap
from mowdeck_control.geo import from_wgs84
from mowdeck_patterns.coverage import gaps as coverage_gaps
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from telemetry.domain.models import PositionRow

CELL_SIZES = (0.5, 1.0, 2.0)
"""Cell sizes in metres a client may ask for."""
DEFAULT_CELL_M = 1.0
MAX_CELLS = 20_000
"""Most cells in one response; the cell size doubles until the cells fit."""
DEFAULT_CUTTING_WIDTH_MM = 400
"""Cutting width when the mower's capabilities do not name one (LUBA 2)."""
DEFAULT_JOIN_SECONDS = 30.0
"""Consecutive blades-on positions at most this far apart in time form one swath."""
DEFAULT_JOIN_M = 10.0
"""Consecutive positions farther apart are not joined (RTK jumps, the mower carried)."""
MIN_GAP_AREA_M2 = 0.25
"""Gaps smaller than this are dropped (slivers between swaths)."""
MAX_GAPS = 500
"""Most gap polygons in one response, largest first."""
GAP_SIMPLIFY_M = 0.05
"""Tolerance of the simplification of gap outlines."""
_THIN_FACTOR = 0.1
"""A swath point closer than this share of the cutting width to the previous one is skipped."""
MAX_PIXEL_M = 0.1
"""Largest pixel of the coverage raster (also at most a quarter of the cutting width)."""
_STAMP_CHUNK = 50_000
"""Points stamped onto the raster per numpy batch (bounds the temporary arrays)."""
_KEY_OFFSET = 1 << 30
_KEY_SPAN = 1 << 31


class Layer(StrEnum):
    """What a heatmap shows."""

    COVERAGE = "coverage"
    RTK = "rtk"
    WIFI = "wifi"
    MNET = "mnet"


class LegendKind(StrEnum):
    """How the client colours the values."""

    RATIO = "ratio"
    """0..1 share of the cell that was cut."""
    FIX = "fix"
    """0..1 share of fixed RTK samples."""
    DBM = "dbm"
    """Signal strength in dBm (``min`` and ``max`` over the response)."""


_LEGEND = {
    Layer.COVERAGE: LegendKind.RATIO,
    Layer.RTK: LegendKind.FIX,
    Layer.WIFI: LegendKind.DBM,
    Layer.MNET: LegendKind.DBM,
}
_FIX_SCORE = {"fixed": 1.0, "float": 0.5, "none": 0.0}


@dataclass(frozen=True, slots=True)
class Cell:
    """One cell: integer indexes in the local frame, its value and the samples in it."""

    x: int
    y: int
    value: float
    n: int


@dataclass(frozen=True, slots=True)
class Legend:
    """Value range of the layer."""

    kind: LegendKind
    min: float | None
    max: float | None


@dataclass(frozen=True, slots=True)
class Gap:
    """A piece of the map's areas the blades missed, in local metres (implicit closing edge)."""

    polygon: list[tuple[float, float]]
    area_m2: float


@dataclass(frozen=True, slots=True)
class Heatmap:
    """A computed layer."""

    layer: Layer
    cell_m: float
    cells: list[Cell]
    legend: Legend
    gaps: list[Gap]
    samples: int


def cell_index(value: float, cell_m: float) -> int:
    """The cell of a coordinate in metres (``floor``, so negative values go to negative cells)."""
    return math.floor(value / cell_m)


@dataclass(slots=True)
class _Swaths:
    """Blades-on pieces of the track, joined while consecutive samples are close in time.

    A point closer than ``thin_m`` to the last kept point of its piece is held back as
    ``pending`` (only the newest one), so a slow or parked mower does not pile up vertices; it is
    appended when the piece ends.
    """

    thin_m: float
    join_seconds: float = DEFAULT_JOIN_SECONDS
    join_m: float = DEFAULT_JOIN_M
    pieces: list[list[tuple[float, float]]] = field(default_factory=list)
    current: list[tuple[float, float]] = field(default_factory=list)
    pending: tuple[float, float] | None = None
    last_t: float | None = None
    last_point: tuple[float, float] | None = None

    def add(self, x: float, y: float, t: float) -> None:
        point = (x, y)
        joined = (
            self.last_t is not None
            and self.last_point is not None
            and abs(t - self.last_t) <= self.join_seconds
            and math.dist(self.last_point, point) <= self.join_m
        )
        if not joined:
            self.close()
        self.last_t, self.last_point = t, point
        if self.current and math.dist(self.current[-1], point) < self.thin_m:
            self.pending = point
            return
        self.current.append(point)
        self.pending = None

    def close(self) -> None:
        """End the current piece (blades off, a pause or a jump)."""
        if self.current:
            if self.pending is not None:
                self.current.append(self.pending)
            self.pieces.append(self.current)
        self.current = []
        self.pending = None
        self.last_t = self.last_point = None

    def dense_points(self, step: float) -> np.ndarray:
        """Every swath point plus points every ``step`` metres along the swaths, as ``(n, 2)``."""
        self.close()
        if not self.pieces:
            return np.empty((0, 2))
        arrays = [np.asarray(piece, dtype=float) for piece in self.pieces]
        starts = [arr[:-1] for arr in arrays if len(arr) >= 2]
        if not starts:
            return np.concatenate(arrays)
        a = np.concatenate(starts)
        d = np.concatenate([arr[1:] for arr in arrays if len(arr) >= 2]) - a
        steps = np.maximum(1, np.ceil(np.hypot(d[:, 0], d[:, 1]) / step)).astype(np.int64)
        segment = np.repeat(np.arange(len(a)), steps)
        first = np.repeat(np.cumsum(steps) - steps, steps)
        t = (np.arange(int(steps.sum())) - first) / steps[segment]
        return np.concatenate([*arrays, a[segment] + t[:, None] * d[segment]])


class HeatmapBuilder:
    """Collects rows for one layer; :meth:`build` bins them."""

    def __init__(
        self,
        layer: Layer,
        reference: GeoReference,
        cutting_width_m: float,
        *,
        join_seconds: float = DEFAULT_JOIN_SECONDS,
        join_m: float = DEFAULT_JOIN_M,
    ) -> None:
        if cutting_width_m <= 0:
            msg = "cutting_width_m must be positive"
            raise ValueError(msg)
        self.layer = layer
        self.reference = reference
        self.cutting_width_m = cutting_width_m
        self.samples = 0
        self._points: list[tuple[float, float, float]] = []
        self._swaths = _Swaths(
            thin_m=cutting_width_m * _THIN_FACTOR, join_seconds=join_seconds, join_m=join_m
        )

    def add(self, row: PositionRow) -> None:
        """Take one track row (any time order, but consistent)."""
        if self.layer is Layer.COVERAGE:
            if not row.blades_on:  # off, or unknown in rows from before migration 0007
                self._swaths.close()
                return
            x, y = from_wgs84(self.reference, row.lat, row.lon)
            self._swaths.add(x, y, row.observed_at.timestamp())
            self._points.append((x, y, 1.0))
            self.samples += 1
            return
        value = self._value(row)
        if value is None:
            return
        x, y = from_wgs84(self.reference, row.lat, row.lon)
        self._points.append((x, y, value))
        self.samples += 1

    def add_all(self, rows: Iterable[PositionRow]) -> None:
        """Take several rows."""
        for row in rows:
            self.add(row)

    def _value(self, row: PositionRow) -> float | None:
        if self.layer is Layer.RTK:
            return _FIX_SCORE.get(row.rtk_fix)
        reading = row.wifi_rssi if self.layer is Layer.WIFI else row.mnet_rssi
        return None if reading is None else float(reading)

    def build(
        self,
        cell_m: float = DEFAULT_CELL_M,
        mower_map: MowerMap | None = None,
        *,
        max_cells: int = MAX_CELLS,
    ) -> Heatmap:
        """Bin what was added; coarsens ``cell_m`` (doubling) until at most ``max_cells``."""
        if cell_m <= 0:
            msg = "cell_m must be positive"
            raise ValueError(msg)
        kind = _LEGEND[self.layer]
        if self.layer is Layer.COVERAGE:
            res, block = _raster_size(cell_m, self.cutting_width_m)
            dense = self._swaths.dense_points(res / 2)
            pixels = _covered_pixels(dense, self.cutting_width_m / 2, res)
            size, cells = _coverage_cells(pixels, block, self._points, cell_m, max_cells)
            found = _gaps(mower_map, pixels, res) if mower_map is not None else []
            return Heatmap(self.layer, size, cells, Legend(kind, 0.0, 1.0), found, self.samples)
        size, cells = _value_cells(self._points, cell_m, max_cells, self.layer)
        if kind is LegendKind.DBM:
            values = [c.value for c in cells]
            legend = Legend(kind, min(values, default=None), max(values, default=None))
        else:
            legend = Legend(kind, 0.0, 1.0)
        return Heatmap(self.layer, size, cells, legend, [], self.samples)


def _bin(
    points: Sequence[tuple[float, float, float]], cell_m: float
) -> dict[tuple[int, int], list[float]]:
    bins: dict[tuple[int, int], list[float]] = {}
    for x, y, value in points:
        bins.setdefault((cell_index(x, cell_m), cell_index(y, cell_m)), []).append(value)
    return bins


def _value_cells(
    points: Sequence[tuple[float, float, float]], cell_m: float, max_cells: int, layer: Layer
) -> tuple[float, list[Cell]]:
    size = cell_m
    bins = _bin(points, size)
    while len(bins) > max_cells:
        size *= 2
        bins = _bin(points, size)
    cells: list[Cell] = []
    for (ix, iy), values in sorted(bins.items()):
        if layer is Layer.RTK:
            value = round(sum(values) / len(values), 3)
        else:
            value = float(statistics.median(values))
        cells.append(Cell(ix, iy, value, len(values)))
    return size, cells


def _raster_size(cell_m: float, cutting_width_m: float) -> tuple[float, int]:
    """Pixel size (at most a quarter of the cutting width, and :data:`MAX_PIXEL_M`) and pixels
    per cell side; the pixel size divides the cell size, so cells are whole blocks."""
    block = math.ceil(cell_m / min(cutting_width_m / 4, MAX_PIXEL_M) - 1e-9)
    return cell_m / block, block


def _pack(ix: np.ndarray, iy: np.ndarray) -> np.ndarray:
    keys: np.ndarray = (ix + _KEY_OFFSET) * _KEY_SPAN + (iy + _KEY_OFFSET)
    return keys


def _unpack(keys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ix, iy = np.divmod(keys, _KEY_SPAN)
    return ix - _KEY_OFFSET, iy - _KEY_OFFSET


def _covered_pixels(points: np.ndarray, radius: float, res: float) -> np.ndarray:
    """Sorted keys (:func:`_pack`) of the pixels whose centre lies within ``radius`` of a point.

    Points are first snapped to a quarter of a pixel and de-duplicated: a mower crossing the
    same ground all week stamps each spot once.
    """
    if len(points) == 0:
        return np.empty(0, dtype=np.int64)
    quantum = res / 4
    steps = np.round(points / quantum).astype(np.int64)
    sx, sy = _unpack(np.unique(_pack(steps[:, 0], steps[:, 1])))
    snapped = np.stack([sx, sy], axis=1) * quantum
    # offsets k with |k + 0.5 - f| <= radius / res for a point at fraction f in [0, 1) of its pixel
    reach = math.ceil(radius / res + 0.5) - 1
    offsets = np.arange(-reach, reach + 1)
    grid_x, grid_y = np.meshgrid(offsets, offsets, indexing="ij")
    ox, oy = grid_x.ravel()[None, :], grid_y.ravel()[None, :]
    found: list[np.ndarray] = []
    for begin in range(0, len(snapped), _STAMP_CHUNK):
        chunk = snapped[begin : begin + _STAMP_CHUNK]
        base = np.floor(chunk / res).astype(np.int64)
        px, py = base[:, :1] + ox, base[:, 1:] + oy
        dx = (px + 0.5) * res - chunk[:, :1]
        dy = (py + 0.5) * res - chunk[:, 1:]
        near = dx * dx + dy * dy <= radius * radius
        found.append(_pack(px[near], py[near]))
    pixels: np.ndarray = np.unique(np.concatenate(found))
    return pixels


def _coverage_cells(
    pixels: np.ndarray,
    block: int,
    points: Sequence[tuple[float, float, float]],
    cell_m: float,
    max_cells: int,
) -> tuple[float, list[Cell]]:
    """Cells with cut pixels and their cut share, coarsened until at most ``max_cells``."""
    if len(pixels) == 0:
        return cell_m, []
    px, py = _unpack(pixels)
    size = cell_m
    while True:
        keys, covered = np.unique(
            _pack(np.floor_divide(px, block), np.floor_divide(py, block)), return_counts=True
        )
        if len(keys) <= max_cells:
            break
        size, block = size * 2, block * 2
    counts: dict[tuple[int, int], int] = {}
    for x, y, _ in points:
        key = (cell_index(x, size), cell_index(y, size))
        counts[key] = counts.get(key, 0) + 1
    cx, cy = _unpack(keys)
    shares = np.round(covered / (block * block), 3)
    return size, [
        Cell(int(x), int(y), float(share), counts.get((int(x), int(y)), 0))
        for x, y, share in zip(cx, cy, shares, strict=True)
    ]


def _covered_geometry(
    pixels: np.ndarray, res: float, bounds: tuple[float, float, float, float]
) -> BaseGeometry:
    """The cut pixels inside ``bounds`` as polygons (one box per run of pixels in a row)."""
    px, py = _unpack(pixels)
    min_x, min_y, max_x, max_y = bounds
    keep = (
        (px >= math.floor(min_x / res) - 1)
        & (px <= math.floor(max_x / res) + 1)
        & (py >= math.floor(min_y / res) - 1)
        & (py <= math.floor(max_y / res) + 1)
    )
    px, py = px[keep], py[keep]
    if len(px) == 0:
        return Polygon()
    order = np.lexsort((px, py))
    px, py = px[order], py[order]
    breaks = np.flatnonzero((np.diff(py) != 0) | (np.diff(px) != 1))
    first = np.concatenate(([0], breaks + 1))
    last = np.concatenate((breaks, [len(px) - 1]))
    boxes = shapely.box(
        px[first] * res, py[first] * res, (px[last] + 1) * res, (py[last] + 1) * res
    )
    return shapely.union_all(boxes)


def map_region(mower_map: MowerMap) -> BaseGeometry:
    """The map's mowable ground: its areas minus no-go zones and obstacles."""
    areas = [shapely.make_valid(Polygon(a.boundary)) for a in mower_map.areas]
    if not areas:
        return Polygon()
    region = unary_union(areas)
    zones = [shapely.make_valid(Polygon(z.boundary)) for z in mower_map.forbidden_zones()]
    if zones:
        region = region.difference(unary_union(zones))
    return region


def _gaps(mower_map: MowerMap, pixels: np.ndarray, res: float) -> list[Gap]:
    region = map_region(mower_map)
    if region.is_empty:
        return []
    covered = _covered_geometry(pixels, res, region.bounds)
    tolerance = max(GAP_SIMPLIFY_M, res * 0.75)  # smooths the pixel steps of the outline
    found: list[Gap] = []
    for piece in coverage_gaps(region, covered, min_area=MIN_GAP_AREA_M2)[:MAX_GAPS]:
        outline = piece.simplify(tolerance, preserve_topology=True)
        ring = list(outline.exterior.coords) if isinstance(outline, Polygon) else []
        if len(ring) < 4:  # closed ring: at least three distinct points plus the closing one
            ring = list(piece.exterior.coords)
        found.append(
            Gap(
                polygon=[(round(x, 2), round(y, 2)) for x, y in ring[:-1]],
                area_m2=round(float(piece.area), 2),
            )
        )
    return found
