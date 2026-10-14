"""What every Vision instance knows about the mowers, from the bus (``MowerView`` port).

* ``telemetry.mower.*`` (ephemeral, newest message per subject first): the last state.
* ``device.mower.*.map`` (ephemeral, newest per subject): the map (geofence and context).
* ``device.mower.*.capabilities`` (ephemeral, newest per subject): the capabilities (whether
  the light can be switched for a dark photo).
* ``command.mower.*`` (ephemeral, new messages): recent command kinds for the context, and
  emergency stops for running executions.
* ``command.result.mower.*`` (ephemeral): refused drive frames for running executions.

Messages naming a different mower in the subject and the payload are dropped. The view keeps
nothing but the newest state, map and capabilities per mower and the last ten commands.
"""

import asyncio
import contextlib
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
    MowerState,
)

from vision.application.ports import ObservedState, WatchEvent

MAX_COMMANDS = 10
WATCH_QUEUE = 1000


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
        self._push(state.mower_id, WatchEvent("state", received, state=state))

    def on_map(self, snapshot: MowerMapSnapshot) -> None:
        """A map snapshot."""
        self._maps[snapshot.mower_id] = (snapshot.account_id, snapshot.map)

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
        if kind == "emergency_stop":
            self._push(envelope.mower_id, WatchEvent("emergency_stop", self._monotonic()))

    def on_result(self, result: CommandResult) -> None:
        """A command result."""
        self._push(result.mower_id, WatchEvent("result", self._monotonic(), result=result))
