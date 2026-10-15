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
10. A path preview (:meth:`MowerController.preview_path`, adapters with
   :class:`~mowdeck_control.connection.PathPreviewCapable`) is refused while a job runs, a
   start waits for its confirmation or a drive session is open, and its areas and pattern are
   checked like a ``StartMow``. It does not take the queue: the mower only plans, and a start
   sent meanwhile simply plans the same route again.
11. A ``FirmwareUpdate`` (ADR 0038) needs ``capabilities.firmware_update`` and passes the rules
   of :func:`mowdeck_control.safety.firmware_update_problem`: the offered version only, the
   mower docked with enough battery (charging is not required), no job, no open drive session,
   no update already installing. While the mower reports ``updating`` every command but
   ``EmergencyStop`` is rejected with ``"firmware update running"``, drive frames included.
12. A ``SpotMow`` (ADR 0039) starts a :class:`~mowdeck_control.spot_run.SpotMowRun` that drives
   the plan with frames through this controller (``correlation_id = spot:<command_id>``): it
   needs ``capabilities.manual_drive``, no open drive session or pending confirmation, an idle
   mower (or a docked one when the connection has ``capabilities.undock`` and implements
   :class:`~mowdeck_control.connection.UndockCapable`, ADR 0041) with a fresh state, enough
   battery, a fixed RTK position (not yet for a docked mower: the run waits for it after
   undocking), a plan inside the geofence and a transit route on the map from the mower (from
   the dock while docked) to the plan (``no_route: ...`` otherwise); the command is answered
   ``accepted`` at once (``rejected`` or ``failed`` when a check fails) and the run's end is
   read with :meth:`MowerController.spot_run_result`. Blades run only with ``blades`` and
   ``confirm_blades`` on a mower with ``manual_drive_blades``; otherwise the run is a dry run.
   While a run is active every command except ``EmergencyStop``, ``SpotStop``, ``SetLight`` and
   the run's own frames is rejected ("spot run active; stop it first"); ``SpotStop`` without a
   run is rejected. An emergency stop, a sensor stop and a change of the activity end the run
   (while undocking only an error, an update or a lost mower do). With ``SpotMow.return_to_dock``
   the run sends ``ReturnToDock`` to the connection after it ended, unless it was stopped.

Every decision is logged on the stdlib logger ``mowdeck_control.controller`` with the command id
and the correlation id (``extra`` fields ``command_id``, ``correlation_id``, ``mower_id``,
``command_kind`` and ``status``).
"""

import asyncio
import contextlib
import inspect
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    EmergencyStop,
    FirmwareUpdate,
    ManualDrive,
    MowerActivity,
    MowerCapabilities,
    MowerMap,
    MowerState,
    MowPath,
    PauseMow,
    ResumeMow,
    ReturnToDock,
    SetBladeHeight,
    SetLight,
    SetSpeed,
    SpotMow,
    SpotRunStatus,
    SpotStop,
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
    PathPreviewCapable,
    UndockCapable,
    current_map_of,
    current_path_of,
    dispatch_of,
)
from mowdeck_control.errors import CommandRejected, NotConnected, UnsupportedPattern
from mowdeck_control.safety import (
    FIRMWARE_UPDATE_RUNNING,
    decide_manual_blade,
    firmware_update_problem,
)
from mowdeck_control.spot_run import (
    REASON_ACTIVITY_CHANGE,
    REASON_EMERGENCY_STOP,
    REASON_LINK_LOST,
    REASON_SENSOR_STOP,
    REASON_STOPPED_BY_USER,
    SpotActions,
    SpotMowRun,
    SpotRunConfig,
    SpotRunReport,
    spot_start_problem,
)
from mowdeck_control.timers import AsyncioTimerFactory, Timer, TimerFactory
from mowdeck_control.translation import MowRequest, plan_to_native_job

StateCallback = Callable[[MowerState], Awaitable[None] | None]
"""Called with every state snapshot, for example to publish telemetry."""

MAX_REASON_LENGTH = 500
DEFAULT_DEDUP_SIZE = 1024
_DRIVE_BLOCKING_ACTIVITIES = frozenset({MowerActivity.MOWING, MowerActivity.RETURNING})
_JOB_ACTIVITIES = frozenset({MowerActivity.MOWING, MowerActivity.PAUSED, MowerActivity.RETURNING})
"""Activities of a running job: no path preview then (rule 10)."""
_SUPERSEDING = (StartMow, PauseMow, ResumeMow, ReturnToDock, EmergencyStop)
"""Job commands that end the confirmation wait of every earlier command (rule 9)."""
SPOT_RUN_ACTIVE = "spot run active; stop it first"
"""Rejection reason of every other command while a spot run drives (rule 12)."""
_SPOT_STOP_WAIT_S = 5.0
"""How long a stop waits for the run's zero frame before the task is cancelled."""


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
        spot_config: SpotRunConfig | None = None,
        spot_sleep: Callable[[float], Awaitable[None]] | None = None,
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
        self._spot_config = spot_config or SpotRunConfig()
        self._spot_sleep = spot_sleep
        self._spot_run: SpotMowRun | None = None
        self._spot_task: asyncio.Task[SpotRunReport] | None = None
        self._spot_status: SpotRunStatus | None = None
        self._status_tasks: set[asyncio.Task[None]] = set()

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

    @property
    def spot_run_active(self) -> bool:
        """True while a spot run drives the mower (rule 12)."""
        return self._spot_run is not None and self._spot_run.active

    @property
    def spot_status(self) -> SpotRunStatus | None:
        """The status of the current or last spot run (``MowerState.spot_run``)."""
        return self._spot_status

    async def spot_run_result(self, run_id: UUID) -> SpotRunReport | None:
        """Wait for the end of the spot run started by the ``SpotMow`` command ``run_id`` and
        return its report; None when no such run is known."""
        run = self._spot_run
        if run is None or run.run_id != run_id:
            return None
        return await run.wait()

    async def stop_spot_run(self, reason: str = REASON_LINK_LOST) -> SpotRunReport | None:
        """End an active spot run with ``reason`` (zero frame, blades off, session closed) and
        return its report; None without an active run."""
        run = self._spot_run
        if run is None or not run.active:
            return None
        run.request_stop(reason)
        task = self._spot_task
        if task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(run.wait()), _SPOT_STOP_WAIT_S)
            if not run.finished:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        return run.report

    async def end_drive_session(self, reason: str = "ended by the caller") -> None:
        """Stop and close an open manual drive session (scripts; the dead-man does it for
        clients that stop sending frames)."""
        await self._end_drive(reason)

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
        """End any spot run, drive session and confirmation wait, stop the state pump and
        disconnect.

        The disconnect always runs, also when ending the spot run, the drive session or the
        pump failed: a connection that is not disconnected keeps its link (and a bridge's
        proxy) open.
        """
        try:
            self._interrupt_confirmations("controller close")
            await self.stop_spot_run(REASON_LINK_LOST)
            await self._end_drive("controller closed")
            pump, self._pump = self._pump, None
            if pump is not None:
                pump.cancel()
                await asyncio.wait({pump})
        finally:
            try:
                await self._connection.disconnect()
            finally:
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

    # --- Mow path -----------------------------------------------------------------------------

    def current_path(self) -> MowPath | None:
        """The route of the running job with its progress, when the adapter knows it
        (:class:`~mowdeck_control.connection.MowPathSource`); the plan of an active spot run
        with its progress while one drives; None otherwise."""
        run = self._spot_run
        if run is not None and run.active:
            return run.mow_path()
        return current_path_of(self._connection)

    @property
    def can_preview_path(self) -> bool:
        """True when the adapter can plan a route without starting
        (:class:`~mowdeck_control.connection.PathPreviewCapable`)."""
        return isinstance(self._connection, PathPreviewCapable)

    async def preview_path(
        self, area_ids: Sequence[str], pattern: Mapping[str, Any] | None = None
    ) -> MowPath:
        """Let the mower plan the route of a start without starting it (rule 10).

        Raises :class:`~mowdeck_control.errors.CommandRejected` when the adapter cannot, a job
        runs, a start awaits its confirmation, a drive session is open, or the areas or the
        pattern are refused; the adapter's :class:`~mowdeck_control.errors.NotConnected` and
        :class:`~mowdeck_control.errors.PathUnavailable` pass through.
        """
        connection = self._connection
        if not isinstance(connection, PathPreviewCapable):
            raise CommandRejected("this connection cannot plan a route without starting")
        if self._session is not None:
            raise CommandRejected("a manual drive session is open")
        if self._confirming:
            raise CommandRejected("a command is still waiting for its confirmation")
        state = self._last_state
        if state is not None and state.activity in _JOB_ACTIVITIES:
            msg = f"the mower is {state.activity.value}; a preview is possible before a start"
            raise CommandRejected(msg)
        try:
            start = StartMow(area_ids=list(area_ids), pattern=dict(pattern) if pattern else None)
        except ValueError as exc:
            raise CommandRejected("choose at least one area") from exc
        problem = await self._validate_start(start)
        if problem is not None:
            raise CommandRejected(problem)
        self._log.info(
            "path preview for mower %s (%s)",
            self._mower_id,
            ", ".join(area_ids),
            extra={"mower_id": str(self._mower_id)},
        )
        return await connection.preview_path(list(area_ids), start.pattern)

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
        if isinstance(command, SpotStop):
            return await self._spot_stop(envelope)
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
            run = self._spot_run
            if run is not None and run.active:
                own_frame = (
                    isinstance(command, ManualDrive)
                    and envelope.correlation_id == run.correlation_id
                )
                if not own_frame and not isinstance(command, SetLight):
                    return self._result(envelope, CommandStatus.REJECTED, SPOT_RUN_ACTIVE)
            if await self._updating():
                return self._result(envelope, CommandStatus.REJECTED, FIRMWARE_UPDATE_RUNNING)
            if isinstance(command, FirmwareUpdate) and self._session is not None:
                return self._result(
                    envelope,
                    CommandStatus.REJECTED,
                    "a manual drive session is open; stop driving first",
                )
            if isinstance(command, ManualDrive):
                if self._confirming:
                    return self._result(
                        envelope,
                        CommandStatus.REJECTED,
                        "a command is still waiting for its confirmation; pause the mower first",
                    )
                return await self._manual_drive(envelope, command)
            if isinstance(command, SpotMow):
                return await self._spot_mow(envelope, command)
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
        await self.stop_spot_run(REASON_EMERGENCY_STOP)
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

    async def _updating(self) -> bool:
        """Rule 11: the mower reports a running firmware update (the adapter's current state,
        so a command right after the start is caught before the pushed state arrived)."""
        try:
            state: MowerState | None = await self._connection.get_state()
        except NotConnected:
            state = self._last_state
        return state is not None and state.activity is MowerActivity.UPDATING

    async def _validate(self, command: JobCommand) -> str | None:
        if isinstance(command, FirmwareUpdate):
            return await self._validate_firmware(command)
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

    async def _validate_firmware(self, command: FirmwareUpdate) -> str | None:
        """Rule 11 on the freshest state the adapter has (the last pushed one may be old)."""
        caps = await self.capabilities()
        try:
            state = await self._connection.get_state()
        except NotConnected:
            return None  # the dispatch reports "mower is not connected"
        if self._confirming:
            return "a command is still waiting for its confirmation"
        return firmware_update_problem(
            command.version, state, caps, drive_session_open=self._session is not None
        )

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

    # --- Spot runs (ADR 0039) -----------------------------------------------------------------

    async def _spot_mow(self, envelope: CommandEnvelope, command: SpotMow) -> CommandResult:
        """Rule 12: check the preconditions and start the run; ``accepted`` at once."""
        caps = await self.capabilities()
        if not caps.manual_drive:
            return self._result(
                envelope,
                CommandStatus.REJECTED,
                "manual driving is not supported on this connection",
            )
        if self._session is not None:
            return self._result(
                envelope,
                CommandStatus.REJECTED,
                "a manual drive session is open; stop driving first",
            )
        if self._confirming:
            return self._result(
                envelope, CommandStatus.REJECTED, "a command is still waiting for its confirmation"
            )
        try:
            state = await self._connection.get_state()
        except NotConnected:
            return self._result(envelope, CommandStatus.FAILED, "mower is not connected")
        undock = caps.undock and isinstance(self._connection, UndockCapable)
        problem = spot_start_problem(state, now=self._clock(), undock=undock)
        if problem is not None:
            return self._result(envelope, CommandStatus.REJECTED, problem)
        blades = command.blades and command.confirm_blades and caps.manual_drive_blades
        run = SpotMowRun(
            run_id=envelope.command_id,
            plan=command.plan,
            mower_map=await self.mower_map(),
            pose_source=lambda: self._last_state,
            frames=_SpotFrames(self, envelope, run_id=envelope.command_id),
            speed_mps=command.speed_mps,
            blades=blades,
            actions=_SpotActions(self),
            undock=undock,
            return_to_dock=command.return_to_dock,
            on_status=self._on_spot_status,
            clock=self._clock,
            sleep=self._spot_sleep,
            config=self._spot_config,
        )
        # The transit planning is pure geometry; a large map takes a moment, off the loop.
        problem = await asyncio.to_thread(run.prepare, state)
        if problem is not None:
            return self._result(envelope, CommandStatus.REJECTED, problem)
        self._spot_run = run
        self._spot_task = asyncio.create_task(run.run(), name=f"spot-{envelope.command_id}")
        self._spot_task.add_done_callback(self._spot_done)
        detail = "spot run started (dry run)" if run.dry_run else "spot run started with blades"
        if run.undocks:
            detail += "; leaving the dock first"
        if run.transit is not None and run.transit.length_m > 0:
            detail += f"; transit {run.transit.length_m:.0f} m"
        if command.blades and not blades:
            detail += "; blades forced off: " + (
                "this mower does not allow blades while driving manually"
                if not caps.manual_drive_blades
                else "confirm_blades missing"
            )
        self._log_event(envelope, detail)
        return self._result(envelope, CommandStatus.ACCEPTED, detail)

    def _spot_done(self, task: "asyncio.Task[SpotRunReport]") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._log.error("spot run task failed for mower %s", self._mower_id, exc_info=exc)

    async def _spot_stop(self, envelope: CommandEnvelope) -> CommandResult:
        """``SpotStop``: end the active run; rejected without one (rule 12)."""
        run = self._spot_run
        if run is None or not run.active:
            return self._result(envelope, CommandStatus.REJECTED, "no spot run is active")
        self._log_event(envelope, "stopping the spot run")
        report = await self.stop_spot_run(REASON_STOPPED_BY_USER)
        reason = report.summary() if report is not None else "spot run stopped"
        return self._result(envelope, CommandStatus.EXECUTED, reason)

    def _on_spot_status(self, status: SpotRunStatus) -> None:
        self._spot_status = status
        state = self._last_state
        if state is None or state.spot_run == status:
            return
        self._last_state = state.model_copy(update={"spot_run": status})
        task = asyncio.create_task(self._emit_spot_status(), name=f"spot-status-{self._mower_id}")
        self._status_tasks.add(task)
        task.add_done_callback(self._status_tasks.discard)

    async def _emit_spot_status(self) -> None:
        """Publish the latest state with the run's current status (taken when the task runs, so
        a status never overtakes a newer one, e.g. ``returning`` after the final ``done``)."""
        state = self._last_state
        if state is None:
            return
        if state.spot_run != self._spot_status:
            state = state.model_copy(update={"spot_run": self._spot_status})
            self._last_state = state
        await self._emit(state)

    async def _send_spot_frame(
        self, envelope: CommandEnvelope, run_id: UUID, frame: ManualDrive
    ) -> str | None:
        """Send one frame of the run through :meth:`handle` (all drive rules apply)."""
        wrapped = CommandEnvelope.create(
            mower_id=self._mower_id,
            account_id=envelope.account_id,
            issued_by=envelope.issued_by,
            command=frame,
            ttl=timedelta(milliseconds=frame.ttl_ms),
            correlation_id=f"spot:{run_id}",
            now=self._clock(),
        )
        result = await self.handle(wrapped)
        if result.status is CommandStatus.ACCEPTED:
            return None
        return f"{result.status.value}: {result.reason}"

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
        if self._spot_status is not None:
            state = state.model_copy(update={"spot_run": self._spot_status})
        self._last_state = state
        run = self._spot_run
        if run is not None and run.ends_on(state.activity):
            if state.activity is MowerActivity.ERROR:
                run.request_stop(REASON_SENSOR_STOP, state.error_code)
            else:
                run.request_stop(REASON_ACTIVITY_CHANGE, state.activity.value)
        if (
            state.activity is MowerActivity.ERROR
            and not state.drive_recoverable
            and self._session is not None
        ):
            await self._end_drive(f"sensor stop: {state.error_code}")
        await self._emit(state)

    async def _emit(self, state: MowerState) -> None:
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


class _SpotFrames:
    """:class:`~mowdeck_control.spot_run.FrameChannel` on a controller: frames go through
    :meth:`MowerController.handle`, the close ends the drive session."""

    def __init__(
        self, controller: MowerController, envelope: CommandEnvelope, *, run_id: UUID
    ) -> None:
        self._controller = controller
        self._envelope = envelope
        self._run_id = run_id

    async def send(
        self, *, linear: float, angular: float, blade_on: bool, ttl_ms: int
    ) -> str | None:
        frame = ManualDrive(
            linear=round(linear, 4), angular=round(angular, 4), blade_on=blade_on, ttl_ms=ttl_ms
        )
        return await self._controller._send_spot_frame(self._envelope, self._run_id, frame)

    async def close(self) -> None:
        await self._controller._end_drive("spot run ended")


class _SpotActions(SpotActions):
    """:class:`~mowdeck_control.spot_run.SpotActions` on a controller's connection. Both go to
    the connection directly (rule 12 keeps every other command out while the run is active);
    adapter errors become the refusal text."""

    def __init__(self, controller: MowerController) -> None:
        self._controller = controller

    async def leave_dock(self) -> str | None:
        connection = self._controller.connection
        if not isinstance(connection, UndockCapable):
            return "this connection cannot undock the mower"
        try:
            await connection.leave_dock()
        except CommandRejected as exc:
            return exc.reason
        except NotConnected:
            return "mower is not connected"
        except Exception as exc:
            _log.exception("leave_dock failed")
            return f"adapter error: {type(exc).__name__}"
        _log.info("spot run: leave_dock sent")
        return None

    async def return_to_dock(self) -> str | None:
        connection = self._controller.connection
        try:
            dispatch = await dispatch_of(connection, ReturnToDock())
            outcome = await dispatch.settle()
        except CommandRejected as exc:
            return exc.reason
        except NotConnected:
            return "mower is not connected"
        except Exception as exc:
            _log.exception("return_to_dock after a spot run failed")
            return f"adapter error: {type(exc).__name__}"
        if outcome.status in (CommandStatus.REJECTED, CommandStatus.FAILED):
            return outcome.reason or outcome.status.value
        _log.info("spot run: return_to_dock sent (%s)", outcome.reason)
        return None


_log = logging.getLogger("mowdeck_control.controller")


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
