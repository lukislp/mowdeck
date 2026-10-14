"""The adapter-agnostic controller of one mower: it enforces the command and safety rules.

:class:`MowerController` accepts :class:`~mowdeck_contracts.v1.CommandEnvelope` objects and
answers with :class:`~mowdeck_contracts.v1.CommandResult` objects. The rules are the same for
every :class:`~mowdeck_control.connection.MowerConnection`:

1. A command for another mower is rejected.
2. Duplicates (same ``command_id``) return the first result again; a bounded LRU remembers the
   most recent ids. A duplicate of a command still in progress waits for its result.
3. An expired envelope (``envelope.is_expired(now)``) is answered with ``expired`` and never
   reaches the mower, also when it expired while waiting in the queue.
4. ``EmergencyStop`` skips the queue: it ends an active drive session, runs immediately (also
   while another command is in progress) and rejects every command that was queued before it.
5. All other commands are sent one at a time, in arrival order. The queue is held only while a
   command is sent: when the adapter confirms through the mower's state
   (:class:`~mowdeck_control.connection.TwoPhaseExecution`), the confirmation wait runs outside
   the queue, so a later command is neither delayed by it nor expires behind it. Adapters
   without the split hold the queue for the whole ``execute``.
6. ``ManualDrive`` frames feed a drive session. Each frame re-arms a dead-man timer of
   ``ttl_ms``; when it fires, the controller stops and closes the session. Blade rules from
   :mod:`mowdeck_control.safety` apply to every frame. A non-drive command ends the session,
   except ``SetLight`` (switching the light while driving at night keeps the session).
7. ``SetBladeHeight`` and ``SetSpeed`` are checked against the capabilities, ``StartMow`` against
   the map (area ids must exist) and, if a pattern is given, against the native patterns. A
   ``StartMow`` with a ``spot_plan`` runs as a route only when ``capabilities.custom_paths`` is
   True; otherwise its ``native`` job (whole areas, one native pattern) is checked like an area
   start and sent instead, and a plan without one is rejected. ``SetLight`` needs
   ``capabilities.light``; it is allowed in every activity (also during an error, so AI assist
   can light up a stuck mower at night) and always reaches the mower, even when the reported
   state already matches: the reported state may be stale, and a second switch is harmless.
8. A state with ``activity = error`` (sensor stop) ends an active drive session. Only an error
   the adapter marks ``drive_recoverable`` (stuck, wheel slip; never lift, tilt or collision)
   admits drive frames, so the mower can be driven free; blades stay off while it lasts.
9. A job command (``StartMow``, ``PauseMow``, ``ResumeMow``, ``ReturnToDock``,
   ``EmergencyStop``) supersedes every earlier command still waiting for its confirmation: the
   wait is cancelled before the new command is sent and the earlier command is reported
   ``accepted`` (``"<what was sent>; confirmation interrupted by pause_mow"``), never failed,
   because it did go out. ``SetBladeHeight`` and ``SetSpeed`` leave pending confirmations
   alone. A ``ManualDrive`` frame is rejected while a confirmation is pending (the mower may be
   about to start or drive home).

Every decision is logged on the stdlib logger ``mowdeck_control.controller`` with the command id
and the correlation id (``extra`` fields ``command_id``, ``correlation_id``, ``mower_id``,
``command_kind`` and ``status``).
"""

import asyncio
import contextlib
import inspect
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    EmergencyStop,
    ManualDrive,
    MowerActivity,
    MowerCapabilities,
    MowerMap,
    MowerState,
    PauseMow,
    ResumeMow,
    ReturnToDock,
    SetBladeHeight,
    SetLight,
    SetSpeed,
    StartMow,
    utc_now,
)

from mowdeck_control.broadcast import StateBroadcaster, StateSubscription
from mowdeck_control.connection import (
    Dispatch,
    DriveSession,
    JobCommand,
    MowerConnection,
    Outcome,
    current_map_of,
    dispatch_of,
)
from mowdeck_control.errors import CommandRejected, NotConnected, UnsupportedPattern
from mowdeck_control.safety import decide_manual_blade
from mowdeck_control.timers import AsyncioTimerFactory, Timer, TimerFactory
from mowdeck_control.translation import MowRequest, plan_to_native_job

StateCallback = Callable[[MowerState], Awaitable[None] | None]
"""Called with every state snapshot, for example to publish telemetry."""

MAX_REASON_LENGTH = 500
DEFAULT_DEDUP_SIZE = 1024
_DRIVE_BLOCKING_ACTIVITIES = frozenset({MowerActivity.MOWING, MowerActivity.RETURNING})
_SUPERSEDING = (StartMow, PauseMow, ResumeMow, ReturnToDock, EmergencyStop)
"""Job commands that end the confirmation wait of every earlier command (rule 9)."""


@dataclass(slots=True)
class _Confirmation:
    """A sent command whose confirmation wait runs outside the queue."""

    task: "asyncio.Task[Outcome]"
    sent: str
    interrupted_by: str | None = None


class MowerController:
    """Applies the command and safety rules for one mower on top of any connection."""

    def __init__(
        self,
        connection: MowerConnection,
        *,
        mower_id: UUID,
        clock: Callable[[], datetime] = utc_now,
        timer_factory: TimerFactory | None = None,
        dedup_size: int = DEFAULT_DEDUP_SIZE,
        on_state: StateCallback | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if dedup_size < 1:
            msg = "dedup_size must be at least 1"
            raise ValueError(msg)
        self._connection = connection
        self._mower_id = mower_id
        self._clock = clock
        self._timers: TimerFactory = timer_factory or AsyncioTimerFactory()
        self._dedup_size = dedup_size
        self._on_state = on_state
        self._log = logger or logging.getLogger("mowdeck_control.controller")
        self._results: OrderedDict[UUID, asyncio.Future[CommandResult]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._estop_epoch = 0
        self._session: DriveSession | None = None
        self._deadman: Timer | None = None
        self._capabilities: MowerCapabilities | None = None
        self._map: MowerMap | None = None
        self._last_state: MowerState | None = None
        self._broadcaster = StateBroadcaster()
        self._pump: asyncio.Task[None] | None = None
        self._confirming: dict[UUID, _Confirmation] = {}

    # --- Lifecycle ----------------------------------------------------------------------------

    @property
    def mower_id(self) -> UUID:
        """The mower this controller is responsible for."""
        return self._mower_id

    @property
    def connection(self) -> MowerConnection:
        """The adapter in use."""
        return self._connection

    @property
    def last_state(self) -> MowerState | None:
        """The most recent state seen by the state pump."""
        return self._last_state

    @property
    def drive_session_active(self) -> bool:
        """True while a manual drive session is open."""
        return self._session is not None

    async def start(self) -> None:
        """Connect, load capabilities and map, and start forwarding state updates."""
        await self._connection.connect()
        self._capabilities = await self._connection.capabilities()
        self._map = await self._connection.get_map()
        if self._pump is None or self._pump.done():
            self._pump = asyncio.create_task(self._pump_states(), name=f"state-{self._mower_id}")
        self._log.info(
            "controller for mower %s started (%s)", self._mower_id, self._connection.kind
        )

    async def close(self) -> None:
        """End any drive session and confirmation wait, stop the state pump and disconnect."""
        self._interrupt_confirmations("controller close")
        await self._end_drive("controller closed")
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
            self._pump = None
        await self._connection.disconnect()
        self._broadcaster.close()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    def watch_state(self) -> StateSubscription:
        """Subscribe to state snapshots (the latest known one first)."""
        return self._broadcaster.subscribe(initial=self._last_state)

    async def capabilities(self, *, refresh: bool = False) -> MowerCapabilities:
        """Capabilities of the mower (loaded once, reloaded with ``refresh``).

        A reload also updates what the command rules check: a mower that learns its model
        after connecting gets its real blade height and speed limits.
        """
        if self._capabilities is None or refresh:
            self._capabilities = await self._connection.capabilities()
        return self._capabilities

    async def mower_map(self, *, refresh: bool = False, rebuild: bool = False) -> MowerMap:
        """The mower's map (loaded once, reloaded with ``refresh``).

        ``rebuild`` takes the map the adapter rebuilds from what it already holds
        (:class:`~mowdeck_control.connection.MapRebuildable`, no traffic to the mower), so a dock
        or reference that arrived after loading shows up; adapters without it keep the cached map.
        """
        if self._map is None or refresh:
            self._map = await self._connection.get_map()
        elif rebuild and (current := current_map_of(self._connection)) is not None:
            self._map = current
        return self._map

    # --- Commands -----------------------------------------------------------------------------

    async def handle(self, envelope: CommandEnvelope) -> CommandResult:
        """Apply the rules to ``envelope`` and return its result."""
        existing = self._results.get(envelope.command_id)
        if existing is not None:
            self._results.move_to_end(envelope.command_id)
            self._log_event(envelope, "duplicate command, returning the first result")
            return await asyncio.shield(existing)
        future: asyncio.Future[CommandResult] = asyncio.get_running_loop().create_future()
        self._remember(envelope.command_id, future)
        try:
            result = await self._process(envelope)
        except asyncio.CancelledError:
            self._results.pop(envelope.command_id, None)
            future.cancel()
            raise
        except Exception as exc:
            self._log.exception("command %s: internal error", envelope.command_id)
            result = self._result(
                envelope, CommandStatus.FAILED, f"internal error: {type(exc).__name__}"
            )
        future.set_result(result)
        return result

    def _remember(self, command_id: UUID, future: asyncio.Future[CommandResult]) -> None:
        self._results[command_id] = future
        while len(self._results) > self._dedup_size:
            self._results.popitem(last=False)

    async def _process(self, envelope: CommandEnvelope) -> CommandResult:
        if envelope.mower_id != self._mower_id:
            return self._result(envelope, CommandStatus.REJECTED, "command is for another mower")
        if envelope.is_expired(self._clock()):
            return self._result(envelope, CommandStatus.EXPIRED, "command expired before delivery")
        command = envelope.command
        if isinstance(command, EmergencyStop):
            return await self._emergency_stop(envelope, command)
        epoch = self._estop_epoch
        async with self._lock:
            if epoch != self._estop_epoch:
                return self._result(
                    envelope, CommandStatus.REJECTED, "preempted by an emergency stop"
                )
            if envelope.is_expired(self._clock()):
                return self._result(
                    envelope, CommandStatus.EXPIRED, "command expired while waiting in the queue"
                )
            if isinstance(command, ManualDrive):
                if self._confirming:
                    return self._result(
                        envelope,
                        CommandStatus.REJECTED,
                        "a command is still waiting for its confirmation; pause the mower first",
                    )
                return await self._manual_drive(envelope, command)
            if not isinstance(command, SetLight):
                await self._end_drive(f"ended by {command.kind}")
            problem = await self._validate(command)
            if problem is not None:
                return self._result(envelope, CommandStatus.REJECTED, problem)
            if isinstance(command, _SUPERSEDING):
                self._interrupt_confirmations(command.kind)
            dispatch = await self._dispatch(envelope, await self._native_form(command))
            if dispatch.confirm is None:
                assert dispatch.outcome is not None  # noqa: S101 - set when there is no confirm
                return self._result(envelope, dispatch.outcome.status, dispatch.outcome.reason)
            confirmation = self._begin_confirmation(envelope, dispatch)
        # Rule 5: the queue is free again while the mower confirms.
        return await self._await_confirmation(envelope, confirmation)

    async def _emergency_stop(
        self, envelope: CommandEnvelope, command: EmergencyStop
    ) -> CommandResult:
        self._estop_epoch += 1
        self._log_event(envelope, "emergency stop: preempting queue and drive session")
        self._interrupt_confirmations(command.kind)
        await self._end_drive("emergency stop")
        return await self._execute(envelope, command)

    # --- Confirmations ------------------------------------------------------------------------

    def _begin_confirmation(self, envelope: CommandEnvelope, dispatch: Dispatch) -> _Confirmation:
        confirm = dispatch.confirm
        assert confirm is not None  # noqa: S101 - checked by the caller

        async def wait() -> Outcome:
            return await confirm()

        task = asyncio.create_task(wait(), name=f"confirm-{envelope.command_id}")
        confirmation = _Confirmation(task=task, sent=dispatch.sent)
        self._confirming[envelope.command_id] = confirmation
        self._log_event(envelope, f"{dispatch.sent}; waiting for the confirmation")
        return confirmation

    async def _await_confirmation(
        self, envelope: CommandEnvelope, confirmation: _Confirmation
    ) -> CommandResult:
        try:
            outcome = await confirmation.task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            caller_cancelled = current is not None and current.cancelling() > 0
            if confirmation.interrupted_by is None or caller_cancelled:
                confirmation.task.cancel()
                raise
            outcome = Outcome.accepted(
                f"{confirmation.sent}; confirmation interrupted by {confirmation.interrupted_by}"
            )
        except Exception as exc:
            self._log.exception("command %s: confirmation failed", envelope.command_id)
            outcome = Outcome.accepted(
                f"{confirmation.sent}; confirmation failed: {type(exc).__name__}"
            )
        finally:
            self._confirming.pop(envelope.command_id, None)
        return self._result(envelope, outcome.status, outcome.reason)

    def _interrupt_confirmations(self, by: str) -> None:
        """Rule 9: cancel every pending confirmation wait; its command reports ``accepted``."""
        for command_id, confirmation in list(self._confirming.items()):
            if confirmation.task.done():
                continue
            confirmation.interrupted_by = by
            confirmation.task.cancel()
            self._log.info(
                "command %s: confirmation interrupted by %s",
                command_id,
                by,
                extra={"command_id": str(command_id), "mower_id": str(self._mower_id)},
            )

    async def _dispatch(self, envelope: CommandEnvelope, command: JobCommand) -> Dispatch:
        """The send phase; adapter errors become a final outcome like in :meth:`_execute`."""
        try:
            return await dispatch_of(self._connection, command)
        except CommandRejected as exc:
            return Dispatch.done(Outcome.rejected(exc.reason))
        except NotConnected:
            return Dispatch.done(Outcome.failed("mower is not connected"))
        except Exception as exc:
            self._log.exception("command %s: adapter error", envelope.command_id)
            return Dispatch.done(Outcome.failed(f"adapter error: {type(exc).__name__}"))

    async def _execute(self, envelope: CommandEnvelope, command: JobCommand) -> CommandResult:
        try:
            outcome = await self._connection.execute(command)
        except CommandRejected as exc:
            outcome = Outcome.rejected(exc.reason)
        except NotConnected:
            outcome = Outcome.failed("mower is not connected")
        except Exception as exc:
            self._log.exception("command %s: adapter error", envelope.command_id)
            outcome = Outcome.failed(f"adapter error: {type(exc).__name__}")
        return self._result(envelope, outcome.status, outcome.reason)

    async def _validate(self, command: JobCommand) -> str | None:
        if isinstance(command, SetBladeHeight):
            caps = await self.capabilities()
            if not caps.blade_height_allowed(command.mm):
                return f"blade height {command.mm} mm is outside {caps.blade_height_range_mm}"
        elif isinstance(command, SetSpeed):
            caps = await self.capabilities()
            if not caps.speed_allowed(command.mps):
                return f"speed {command.mps} m/s is outside {_speed_range(caps)}"
        elif isinstance(command, SetLight):
            if not (await self.capabilities()).light:
                return "this mower has no switchable light"
        elif isinstance(command, StartMow) and command.spot_plan is not None:
            return await self._validate_spot(command)
        elif isinstance(command, StartMow) and command.area_ids:
            return await self._validate_start(command)
        return None

    async def _validate_spot(self, command: StartMow) -> str | None:
        plan = command.spot_plan
        assert plan is not None  # noqa: S101 - checked by the caller
        if (await self.capabilities()).custom_paths:
            return None
        if plan.native is None:
            return "custom paths not supported by this mower and the plan has no native form"
        return await self._validate_start(_native_start(command))

    async def _native_form(self, command: JobCommand) -> JobCommand:
        """A spot plan for a mower without custom paths becomes its native area start."""
        if (
            isinstance(command, StartMow)
            and command.spot_plan is not None
            and command.spot_plan.native is not None
            and not (await self.capabilities()).custom_paths
        ):
            return _native_start(command)
        return command

    async def _validate_start(self, command: StartMow) -> str | None:
        mower_map = await self.mower_map()
        unknown = [a for a in command.area_ids if mower_map.area(a) is None]
        if unknown:
            mower_map = await self.mower_map(refresh=True)
            unknown = [a for a in command.area_ids if mower_map.area(a) is None]
        if unknown:
            return f"unknown area ids: {', '.join(unknown)}"
        if command.pattern is not None:
            try:
                plan_to_native_job(MowRequest.from_start_mow(command), await self.capabilities())
            except (UnsupportedPattern, ValueError) as exc:
                return str(exc)
        return None

    # --- Manual driving -----------------------------------------------------------------------

    async def _manual_drive(self, envelope: CommandEnvelope, frame: ManualDrive) -> CommandResult:
        caps = await self.capabilities()
        if not caps.manual_drive:
            return self._result(envelope, CommandStatus.REJECTED, "manual driving is not supported")
        try:
            state = await self._connection.get_state()
        except NotConnected:
            return self._result(envelope, CommandStatus.FAILED, "mower is not connected")
        if state.activity is MowerActivity.ERROR and not state.drive_recoverable:
            await self._end_drive("sensor stop")
            return self._result(
                envelope, CommandStatus.REJECTED, f"sensor stop active: {state.error_code}"
            )
        if self._session is None and state.activity in _DRIVE_BLOCKING_ACTIVITIES:
            return self._result(
                envelope, CommandStatus.REJECTED, "pause or end the job before driving manually"
            )
        decision = decide_manual_blade(frame, caps, state.activity)
        if self._session is None or not self._session.is_open:
            try:
                self._session = await self._connection.start_drive_session()
            except CommandRejected as exc:
                self._session = None
                return self._result(envelope, CommandStatus.REJECTED, exc.reason)
            self._log_event(envelope, "drive session opened")
        try:
            await self._session.update(frame.linear, frame.angular, decision.blade_on)
        except CommandRejected as exc:
            await self._end_drive("drive frame rejected")
            return self._result(envelope, CommandStatus.REJECTED, exc.reason)
        except Exception as exc:
            self._log.exception("command %s: drive frame failed", envelope.command_id)
            await self._end_drive("drive frame failed")
            return self._result(
                envelope, CommandStatus.FAILED, f"adapter error: {type(exc).__name__}"
            )
        self._arm_deadman(frame.ttl_ms)
        return self._result(envelope, CommandStatus.ACCEPTED, decision.forced_off_reason)

    def _arm_deadman(self, ttl_ms: int) -> None:
        if self._deadman is not None:
            self._deadman.cancel()
        session = self._session

        async def fire() -> None:
            await self._deadman_fired(session)

        self._deadman = self._timers(ttl_ms / 1000, fire)

    async def _deadman_fired(self, session: DriveSession | None) -> None:
        if session is None or self._session is not session:
            return
        self._log.warning(
            "dead-man timeout for mower %s: no drive frame in time, stopping", self._mower_id
        )
        self._deadman = None
        await self._end_drive("dead-man timeout")

    async def _end_drive(self, reason: str) -> None:
        if self._deadman is not None:
            self._deadman.cancel()
            self._deadman = None
        session, self._session = self._session, None
        if session is None:
            return
        try:
            await session.stop()
        except Exception:
            self._log.exception("drive session stop failed for mower %s", self._mower_id)
        try:
            await session.close()
        except Exception:
            self._log.exception("drive session close failed for mower %s", self._mower_id)
        self._log.info("drive session of mower %s ended: %s", self._mower_id, reason)

    # --- State --------------------------------------------------------------------------------

    async def _pump_states(self) -> None:
        try:
            async for state in self._connection.watch_state():
                await self._on_state_received(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log.exception("state stream of mower %s failed", self._mower_id)

    async def _on_state_received(self, state: MowerState) -> None:
        self._last_state = state
        if (
            state.activity is MowerActivity.ERROR
            and not state.drive_recoverable
            and self._session is not None
        ):
            await self._end_drive(f"sensor stop: {state.error_code}")
        self._broadcaster.publish(state)
        if self._on_state is not None:
            try:
                maybe = self._on_state(state)
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception:
                self._log.exception("state callback failed for mower %s", self._mower_id)

    # --- Results and logging ------------------------------------------------------------------

    def _result(
        self, envelope: CommandEnvelope, status: CommandStatus, reason: str | None
    ) -> CommandResult:
        if reason is not None and len(reason) > MAX_REASON_LENGTH:
            reason = reason[: MAX_REASON_LENGTH - 3] + "..."
        result = CommandResult(
            command_id=envelope.command_id,
            mower_id=envelope.mower_id,
            status=status,
            reason=reason,
            at=self._clock(),
            correlation_id=envelope.correlation_id,
        )
        level = (
            logging.WARNING
            if status in (CommandStatus.REJECTED, CommandStatus.FAILED, CommandStatus.EXPIRED)
            else logging.INFO
        )
        self._log.log(
            level,
            "command %s (%s) %s%s",
            envelope.command_id,
            envelope.command.kind,
            status,
            f": {reason}" if reason else "",
            extra=self._extra(envelope, status),
        )
        return result

    def _log_event(self, envelope: CommandEnvelope, message: str) -> None:
        self._log.info(
            "command %s (%s): %s",
            envelope.command_id,
            envelope.command.kind,
            message,
            extra=self._extra(envelope, None),
        )

    def _extra(self, envelope: CommandEnvelope, status: CommandStatus | None) -> dict[str, str]:
        extra = {
            "command_id": str(envelope.command_id),
            "correlation_id": envelope.correlation_id,
            "mower_id": str(envelope.mower_id),
            "command_kind": envelope.command.kind,
        }
        if status is not None:
            extra["status"] = str(status)
        return extra


def _native_start(command: StartMow) -> StartMow:
    plan = command.spot_plan
    assert plan is not None and plan.native is not None  # noqa: S101 - checked by the callers
    return StartMow(area_ids=list(plan.native.area_ids), pattern=dict(plan.native.pattern))


def _speed_range(caps: MowerCapabilities) -> str:
    """The settable speed range for messages, for example ``0.2 to 0.6 m/s``."""
    if caps.max_speed_mps is None:
        return "the supported range (the speed is not settable)"
    low = caps.min_speed_mps if caps.min_speed_mps is not None else 0
    return f"{low} to {caps.max_speed_mps} m/s"
