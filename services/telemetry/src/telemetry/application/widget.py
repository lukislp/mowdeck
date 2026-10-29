"""What the iOS widgets read from Telemetry (ADR 0059): a compact state and a map picture.

``state`` projects the latest state, the job and the area name of one mower into a few fields a
widget needs (the widget shows state, battery, progress, area and remaining time); an error comes
with its plain-language title, category and severity from the vendor's error lexicon (ADR 0067),
so the widgets need no second request and show the words from their stored snapshot. ``map_png``
draws the mower's map with the mowed path and the mower; pictures are cached for a short time
and the number of concurrent drawings is bounded, because widgets reload often and the drawing
is CPU work (done in a worker thread, never on the event loop).
"""

import asyncio
import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import (
    ErrorExplanation,
    MowerErrorCategory,
    MowerMap,
    MowerState,
    MowPath,
    generic_text,
)
from mowdeck_control.geo import from_wgs84

from telemetry.application import mapimage
from telemetry.application.errors import ErrorExplanations
from telemetry.application.ports import Clock, Store
from telemetry.domain.errors import NoSnapshotYetError, NoTelemetryYetError, TelemetryError
from telemetry.domain.models import LatestRow

CACHE_TTL = timedelta(seconds=30)
"""A drawn picture is served again for this long (the ETag then also stays)."""
CACHE_SIZE = 64
"""Pictures kept at most (about 100 KB each)."""
MAX_RENDERS = 2
"""Pictures drawn at the same time per replica."""
TRACK_WINDOW = timedelta(hours=3)
"""Without a mow path the picture shows the position track of this long."""
TRACK_POINTS = 1500
POSITION_STEP_M = 0.5
"""The mower moves in the picture (and the ETag changes) in steps of this many metres."""


class BusyError(TelemetryError):
    """All drawing slots are taken; the client retries shortly."""


@dataclass(frozen=True, slots=True)
class WidgetJob:
    """The running job as the widgets show it."""

    progress_percent: int | None
    area_id: str | None
    area_name: str | None
    elapsed_s: int | None
    remaining_s: int | None
    mowed_m2: float | None


@dataclass(frozen=True, slots=True)
class WidgetState:
    """The compact state of one mower for the widgets."""

    mower_id: UUID
    observed_at: Any
    stale: bool
    activity: str
    battery_percent: int
    charging: bool
    rain_detected: bool | None
    error_code: str | None
    error_message: str | None
    job: WidgetJob | None
    error_title: str | None = None
    error_category: str | None = None
    error_severity: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedMap:
    """A picture and its validator."""

    png: bytes
    etag: str


def area_names(map_body: dict[str, Any] | None) -> dict[str, str]:
    """Names of the map areas by id (areas without a name are left out)."""
    if not map_body:
        return {}
    names: dict[str, str] = {}
    for area in map_body.get("areas") or []:
        if isinstance(area, dict) and area.get("id") and area.get("name"):
            names[str(area["id"])] = str(area["name"])[:100]
    return names


def error_title(explanation: ErrorExplanation) -> str | None:
    """The words a widget shows for an error: the lexicon's title, or for a code no lexicon
    knows MowDeck's generic title of its category; None when even the category is unknown (the
    widget then keeps the vendor's ``error_message``)."""
    if explanation.known:
        return explanation.title
    if explanation.category is MowerErrorCategory.OTHER:
        return None
    return generic_text(explanation.category, explanation.locale).title


def widget_state(
    row: LatestRow,
    *,
    stale: bool,
    names: dict[str, str],
    explanation: ErrorExplanation | None = None,
) -> WidgetState:
    """Project a stored state; ``explanation`` explains its error (only used in ``error``)."""
    state = MowerState.model_validate(row.state)
    job = state.job
    activity = "offline" if stale else state.activity.value
    in_error = state.activity.value == "error"
    explained = explanation if in_error and state.error_code else None
    return WidgetState(
        mower_id=row.mower_id,
        observed_at=state.observed_at,
        stale=stale,
        activity=activity,
        battery_percent=state.battery_percent,
        charging=state.charging,
        rain_detected=state.rain_detected,
        error_code=state.error_code if in_error else None,
        error_message=state.error_message if in_error else None,
        job=(
            WidgetJob(
                progress_percent=job.progress_percent,
                area_id=job.area_id,
                area_name=names.get(job.area_id) if job.area_id else None,
                elapsed_s=job.elapsed_s,
                remaining_s=job.remaining_s,
                mowed_m2=job.mowed_m2,
            )
            if job is not None
            else None
        ),
        error_title=error_title(explained) if explained is not None else None,
        error_category=(
            explained.category.value
            if explained is not None
            else state.error_category.value
            if in_error and state.error_category is not None
            else None
        ),
        error_severity=explained.severity.value if explained is not None else None,
    )


class Widget:
    """Read use cases for the iOS widgets."""

    def __init__(self, store: Store, stale_check: Any, clock: Clock) -> None:
        self._store = store
        self._is_stale = stale_check
        self._clock = clock
        self._cache: OrderedDict[str, tuple[Any, RenderedMap]] = OrderedDict()
        self._slots = asyncio.Semaphore(MAX_RENDERS)

    async def state(
        self,
        account_id: UUID,
        mower_id: UUID,
        *,
        locale: str | None = None,
        errors: ErrorExplanations | None = None,
    ) -> WidgetState:
        """The compact state; :class:`NoTelemetryYetError` before the first state. With
        ``errors`` an error is explained in ``locale`` (never raises: a gateway that does not
        answer gives the generic words of the error's category)."""
        row = await self._store.latest_of(account_id, mower_id)
        if row is None:
            msg = "no telemetry yet"
            raise NoTelemetryYetError(msg)
        snapshot = await self._store.snapshot_of(account_id, mower_id, "map")
        names = area_names(snapshot.body if snapshot is not None else None)
        explanation = None
        if errors is not None and row.state.get("activity") == "error":
            explanation = await errors.for_state(row.state, locale)
        return widget_state(row, stale=self._is_stale(row), names=names, explanation=explanation)

    async def map_png(
        self, account_id: UUID, mower_id: UUID, width: int, height: int, theme: str
    ) -> RenderedMap:
        """The picture; :class:`NoSnapshotYetError` (map) before the gateway published a map,
        :class:`BusyError` when every drawing slot is taken."""
        width, height = mapimage.clamp_size(width, height)
        theme = "dark" if theme == "dark" else "light"
        snapshot = await self._store.snapshot_of(account_id, mower_id, "map")
        if snapshot is None:
            msg = "no map received from the mower yet"
            raise NoSnapshotYetError(msg, "map")
        latest = await self._store.latest_of(account_id, mower_id)
        path_row = await self._store.snapshot_of(account_id, mower_id, "path")
        path = (
            MowPath.model_validate(path_row.body)
            if path_row is not None and path_row.body
            else None
        )
        state = MowerState.model_validate(latest.state) if latest is not None else None
        mower_map = MowerMap.model_validate(snapshot.body)
        local = None
        if state is not None and state.position is not None:
            local = from_wgs84(mower_map, state.position.lat, state.position.lon)
        progress = path.progress.index if path is not None and path.progress else None
        key = "|".join(
            str(part)
            for part in (
                mower_id,
                snapshot.revision,
                path.path_hash if path is not None else "-",
                progress,
                None
                if local is None
                else (round(local[0] / POSITION_STEP_M), round(local[1] / POSITION_STEP_M)),
                width,
                height,
                theme,
            )
        )
        etag = '"' + hashlib.sha256(key.encode()).hexdigest()[:24] + '"'
        now = self._clock()
        cached = self._cache.get(etag)
        if cached is not None and now - cached[0] < CACHE_TTL:
            self._cache.move_to_end(etag)
            return cached[1]
        if self._slots.locked():
            msg = "map pictures are being drawn, try again in a moment"
            raise BusyError(msg)
        async with self._slots:
            track: list[mapimage.Point] = []
            if path is None:
                rows = await self._store.positions(
                    account_id, mower_id, now - TRACK_WINDOW, now, TRACK_POINTS
                )
                track = [from_wgs84(mower_map, r.lat, r.lon) for r in rows]
            scene = scene_of(mower_map, path, track, local)
            png = await asyncio.to_thread(mapimage.render, scene, width, height, theme)  # type: ignore[arg-type]
        rendered = RenderedMap(png, etag)
        self._cache[etag] = (now, rendered)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return rendered


def scene_of(
    mower_map: MowerMap,
    path: MowPath | None,
    track: list[mapimage.Point],
    mower: mapimage.Point | None,
) -> mapimage.MapScene:
    """The geometry to draw: the map, the mow path split at the progress (or the recent track),
    the dock and the mower."""
    planned: list[list[mapimage.Point]] = []
    mowed: list[list[mapimage.Point]] = []
    if path is not None:
        done = path.progress.index if path.progress is not None else 0
        seen = 0
        for index, line in enumerate(path.polylines):
            kind = path.kinds[index] if path.kinds else "mow"
            if kind == "transfer":
                seen += len(line)
                continue
            cut = max(0, min(len(line), done - seen))
            if cut >= 2:
                mowed.append(list(line[:cut]))
            if len(line) - cut >= 2:
                planned.append(list(line[max(cut - 1, 0) :]))
            seen += len(line)
    elif len(track) >= 2:
        mowed.append(track)
    return mapimage.MapScene(
        areas=[a.boundary for a in mower_map.areas],
        no_go=[z.boundary for z in mower_map.forbidden_zones()],
        planned=planned,
        mowed=mowed,
        dock=(mower_map.dock.x, mower_map.dock.y) if mower_map.dock is not None else None,
        mower=mower,
    )
