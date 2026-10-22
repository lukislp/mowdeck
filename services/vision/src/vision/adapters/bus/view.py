"""What every Vision instance knows about the mowers, from the bus (``MowerView`` port).

* ``telemetry.mower.*`` (ephemeral, newest message per subject first): the last state.
* ``device.mower.*.map`` (ephemeral, newest per subject): the map (geofence and context).
* ``device.mower.*.capabilities`` (ephemeral, newest per subject): the capabilities (whether
  the light can be switched for a dark photo).
* ``device.mower.*.path`` (ephemeral, newest per subject): the route of the running job (the
  job context of :mod:`vision.domain.job`; ``path = None`` once the job ended).
* ``command.mower.*`` (ephemeral, new messages): recent command kinds for the context, the
  areas of the newest ``start_mow`` (the job's order when no route is known), and emergency
  stops for running executions.
* ``command.result.mower.*`` (ephemeral): refused drive frames for running executions.

Messages naming a different mower in the subject and the payload are dropped. The view keeps
nothing but the newest state, map, capabilities and route per mower, the last ten commands, the
areas of the newest start and the last :data:`MAX_TRACK` distinct positions with an RTK fix
(the context's ``recent_track``).
"""

import asyncio
import contextlib
import math
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    MowerCapabilities,
    MowerCapabilitiesSnapshot,
    MowerMap,
    MowerMapSnapshot,
    MowerPathSnapshot,
    MowerState,
    MowPath,
    ReturnToDock,
    RtkFixStatus,
    StartMow,
)

from vision.application.ports import ObservedState, WatchEvent

MAX_COMMANDS = 10
MAX_TRACK = 20
TRACK_MIN_STEP_M = 0.3
"""A new position counts for the track once it is this far from the previous one."""
WATCH_QUEUE = 1000
_TRACK_FIXES = frozenset({RtkFixStatus.FIXED, RtkFixStatus.FLOAT})


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance of two nearby WGS84 points (equirectangular, plenty for a garden)."""
    north = math.radians(b[0] - a[0]) * 6_371_000
    east = math.radians(b[1] - a[1]) * 6_371_000 * math.cos(math.radians(a[0]))
    return math.hypot(north, east)


class _Watch:
    """``MowerWatch``: a bounded queue of events of one mower."""

    def __init__(self, view: "BusMowerView", mower_id: UUID) -> None:
        self._view = view
        self._mower_id = mower_id
        self.queue: asyncio.Queue[WatchEvent] = asyncio.Queue(maxsize=WATCH_QUEUE)

    def push(self, event: WatchEvent) -> None:
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()  # the newest events matter most
        self.queue.put_nowait(event)

    async def next(self, timeout: float) -> WatchEvent | None:  # noqa: ASYNC109 - polling API
        try:
            return await asyncio.wait_for(self.queue.get(), max(0.0, timeout))
        except TimeoutError:
            return None

    def close(self) -> None:
        self._view.unwatch(self._mower_id, self)


class BusMowerView:
    """The ``MowerView`` port, fed by the subscription handlers."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._states: dict[UUID, ObservedState] = {}
        self._maps: dict[UUID, tuple[UUID, MowerMap]] = {}
        self._capabilities: dict[UUID, tuple[UUID, MowerCapabilities]] = {}
        self._commands: dict[UUID, deque[tuple[str, datetime, str]]] = {}
        self._tracks: dict[UUID, deque[tuple[float, float, datetime]]] = {}
        self._paths: dict[UUID, tuple[UUID, MowPath | None, datetime]] = {}
        self._starts: dict[UUID, list[str]] = {}
        self._watches: dict[UUID, set[_Watch]] = {}

    # --- port -----------------------------------------------------------------------------

    def monotonic(self) -> float:
        """The clock of the ``received`` stamps."""
        return self._monotonic()

    def state(self, mower_id: UUID) -> ObservedState | None:
        """The newest state."""
        return self._states.get(mower_id)

    def mower_map(self, mower_id: UUID) -> tuple[UUID, MowerMap] | None:
        """The newest map and its account."""
        return self._maps.get(mower_id)

    def capabilities(self, mower_id: UUID) -> tuple[UUID, MowerCapabilities] | None:
        """The newest capabilities and their account."""
        return self._capabilities.get(mower_id)

    def recent_commands(self, mower_id: UUID) -> list[tuple[str, datetime]]:
        """Recent command kinds, oldest first."""
        return [(kind, at) for kind, at, _ in self._commands.get(mower_id, ())]

    def recent_track(self, mower_id: UUID) -> list[tuple[float, float, datetime]]:
        """Recent positions ``(lat, lon, observed_at)`` with an RTK fix, oldest first."""
        return list(self._tracks.get(mower_id, ()))

    def mow_path(self, mower_id: UUID) -> tuple[UUID, MowPath | None] | None:
        """The route of the running job (None once it ended) and its account."""
        found = self._paths.get(mower_id)
        return (found[0], found[1]) if found is not None else None

    def last_start(self, mower_id: UUID) -> list[str] | None:
        """The areas of the newest ``start_mow`` (None: none seen, or a dock command that ended
        the job came later)."""
        found = self._starts.get(mower_id)
        return list(found) if found is not None else None

    def watch(self, mower_id: UUID) -> _Watch:
        """Live events of a mower until ``close()``."""
        watch = _Watch(self, mower_id)
        self._watches.setdefault(mower_id, set()).add(watch)
        return watch

    def unwatch(self, mower_id: UUID, watch: _Watch) -> None:
        """Stop a watch."""
        watches = self._watches.get(mower_id)
        if watches is not None:
            watches.discard(watch)
            if not watches:
                del self._watches[mower_id]

    # --- feeding --------------------------------------------------------------------------

    def _push(self, mower_id: UUID, event: WatchEvent) -> None:
        for watch in list(self._watches.get(mower_id, ())):
            watch.push(event)

    def on_state(self, state: MowerState) -> None:
        """A telemetry state."""
        received = self._monotonic()
        current = self._states.get(state.mower_id)
        if current is not None and current.state.observed_at > state.observed_at:
            return  # an older state delivered late
        self._states[state.mower_id] = ObservedState(state, received)
        self._add_track(state)
        self._push(state.mower_id, WatchEvent("state", received, state=state))

    def _add_track(self, state: MowerState) -> None:
        position = state.position
        if position is None or state.rtk_fix not in _TRACK_FIXES:
            return
        track = self._tracks.setdefault(state.mower_id, deque(maxlen=MAX_TRACK))
        point = (position.lat, position.lon, state.observed_at)
        if track and _metres(track[-1][:2], point[:2]) < TRACK_MIN_STEP_M:
            return
        track.append(point)

    def on_map(self, snapshot: MowerMapSnapshot) -> None:
        """A map snapshot."""
        self._maps[snapshot.mower_id] = (snapshot.account_id, snapshot.map)

    def on_path(self, snapshot: MowerPathSnapshot) -> None:
        """A path snapshot (older ones delivered late are ignored)."""
        current = self._paths.get(snapshot.mower_id)
        if current is not None and current[2] > snapshot.observed_at:
            return
        self._paths[snapshot.mower_id] = (snapshot.account_id, snapshot.path, snapshot.observed_at)

    def on_capabilities(self, snapshot: MowerCapabilitiesSnapshot) -> None:
        """A capabilities snapshot."""
        self._capabilities[snapshot.mower_id] = (snapshot.account_id, snapshot.capabilities)

    def on_command(self, envelope: CommandEnvelope) -> None:
        """A command for a mower (drive frames of one session folded into one entry)."""
        kind = envelope.command.kind
        history = self._commands.setdefault(envelope.mower_id, deque(maxlen=MAX_COMMANDS))
        correlation = envelope.correlation_id
        if kind == "manual_drive" and history and history[-1][2] == correlation:
            history[-1] = (kind, envelope.issued_at, correlation)
        else:
            history.append((kind, envelope.issued_at, correlation))
        command = envelope.command
        if isinstance(command, StartMow):
            self._starts[envelope.mower_id] = list(command.area_ids)
        elif isinstance(command, ReturnToDock) and command.end_job:
            self._starts.pop(envelope.mower_id, None)
        if kind == "emergency_stop":
            self._push(envelope.mower_id, WatchEvent("emergency_stop", self._monotonic()))

    def on_result(self, result: CommandResult) -> None:
        """A command result."""
        self._push(result.mower_id, WatchEvent("result", self._monotonic(), result=result))
