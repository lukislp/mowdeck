"""MowerController: expiry, de-duplication, emergency stop, serialization, dead-man, limits."""

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    ConnectionKind,
    EmergencyStop,
    GeoReference,
    ManualDrive,
    MapArea,
    MowerActivity,
    MowerCapabilities,
    MowerCommand,
    MowerMap,
    MowerState,
    PauseMow,
    PrincipalKind,
    PrincipalRef,
    ResumeMow,
    ReturnToDock,
    SetBladeHeight,
    SetLight,
    SetSpeed,
    SpotMowPlan,
    SpotNativeJob,
    SpotSegment,
    StartMow,
)
from mowdeck_control import (
    CommandRejected,
    JobCommand,
    MapRebuildable,
    MowerController,
    NotConnected,
    Outcome,
    SensorStop,
    SimulatedMower,
    SimulationConfig,
    StateBroadcaster,
    StateSubscription,
    current_map_of,
)
from mowdeck_control.testing import ManualClock, ManualTimers

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
ACCOUNT = uuid4()
SQUARE = [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)]


class FakeSession:
    def __init__(self) -> None:
        self.updates: list[tuple[float, float, bool]] = []
        self.stops = 0
        self.closed = False
        self.fail: Exception | None = None
        self.fail_stop = False

    @property
    def is_open(self) -> bool:
        return not self.closed

    async def update(self, linear: float, angular: float, blade_on: bool) -> None:
        if self.fail is not None:
            raise self.fail
        self.updates.append((linear, angular, blade_on))

    async def stop(self) -> None:
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("link lost")

    async def close(self) -> None:
        self.closed = True
        if self.fail_stop:
            raise RuntimeError("link lost")


class FakeConnection:
    """A scriptable connection that records every call."""

    def __init__(
        self,
        mower_id: UUID,
        *,
        capabilities: MowerCapabilities | None = None,
        activity: MowerActivity = MowerActivity.IDLE,
    ) -> None:
        self.mower_id = mower_id
        self.caps = capabilities or MowerCapabilities(
            native_patterns=frozenset({"stripes"}),
            manual_drive=True,
            manual_drive_blades=True,
            blade_height_range_mm=(25, 70),
            max_speed_mps=1.0,
        )
        self.activity = activity
        self.error_code: str | None = None
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.execute_error: Exception | None = None
        self.outcome = Outcome.executed()
        self.sessions: list[FakeSession] = []
        self.session_error: Exception | None = None
        self.state_error: Exception | None = None
        self.map_loads = 0
        self.areas = ["front"]
        self.broadcaster = StateBroadcaster()
        self.connected = False

    @property
    def kind(self) -> ConnectionKind:
        return ConnectionKind.SIMULATED

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        self.broadcaster.close()

    async def capabilities(self) -> MowerCapabilities:
        return self.caps

    def state(self) -> MowerState:
        return MowerState(
            mower_id=self.mower_id,
            account_id=ACCOUNT,
            observed_at=ManualClock()(),
            activity=self.activity,
            battery_percent=80,
            connection=ConnectionKind.SIMULATED,
            error_code=self.error_code,
        )

    async def get_state(self) -> MowerState:
        if self.state_error is not None:
            raise self.state_error
        return self.state()

    def watch_state(self) -> StateSubscription:
        return self.broadcaster.subscribe(initial=self.state())

    async def get_map(self) -> MowerMap:
        self.map_loads += 1
        return MowerMap(
            reference=GeoReference(origin_lat=48, origin_lon=11),
            areas=[MapArea(id=a, boundary=SQUARE) for a in self.areas],
        )

    async def execute(self, command: JobCommand) -> Outcome:
        self.calls.append(f"start:{command.kind}")
        if self.gate is not None and command.kind != "emergency_stop":
            await self.gate.wait()
        if self.execute_error is not None:
            raise self.execute_error
        self.calls.append(f"end:{command.kind}")
        return self.outcome

    async def start_drive_session(self) -> FakeSession:
        if self.session_error is not None:
            raise self.session_error
        session = FakeSession()
        self.sessions.append(session)
        self.calls.append("drive_session")
        return session


class Harness:
    def __init__(self, **kwargs: Any) -> None:
        self.mower_id = uuid4()
        self.clock = ManualClock()
        self.timers = ManualTimers(self.clock)
        self.conn = FakeConnection(self.mower_id, **kwargs)
        self.controller = MowerController(
            self.conn, mower_id=self.mower_id, clock=self.clock, timer_factory=self.timers
        )

    def envelope(
        self,
        command: MowerCommand,
        *,
        ttl: float = 30,
        correlation_id: str | None = None,
        mower_id: UUID | None = None,
    ) -> CommandEnvelope:
        return CommandEnvelope.create(
            mower_id=mower_id or self.mower_id,
            account_id=ACCOUNT,
            issued_by=ISSUER,
            command=command,
            ttl=timedelta(seconds=ttl),
            correlation_id=correlation_id,
            now=self.clock(),
        )

    async def send(self, command: MowerCommand, **kwargs: Any) -> CommandResult:
        return await self.controller.handle(self.envelope(command, **kwargs))


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_results_carry_command_and_correlation_id() -> None:
    h = Harness()
    envelope = h.envelope(PauseMow(), correlation_id="corr-123")
    result = await h.controller.handle(envelope)
    assert result.status is CommandStatus.EXECUTED
    assert result.command_id == envelope.command_id
    assert result.correlation_id == "corr-123"
    assert result.mower_id == h.mower_id
    assert result.at == h.clock()


async def test_expired_command_is_rejected_without_touching_the_mower() -> None:
    h = Harness()
    envelope = h.envelope(StartMow(area_ids=["front"]), ttl=5)
    h.clock.advance(5)
    result = await h.controller.handle(envelope)
    assert result.status is CommandStatus.EXPIRED
    assert h.conn.calls == []


async def test_command_for_another_mower_is_rejected() -> None:
    h = Harness()
    result = await h.send(PauseMow(), mower_id=uuid4())
    assert result.status is CommandStatus.REJECTED
    assert result.reason == "command is for another mower"
    assert h.conn.calls == []


async def test_duplicates_return_the_first_result() -> None:
    h = Harness()
    envelope = h.envelope(PauseMow())
    first = await h.controller.handle(envelope)
    h.clock.advance(60)  # even after expiry the duplicate gets the first result
    second = await h.controller.handle(envelope)
    assert second == first
    assert h.conn.calls == ["start:pause_mow", "end:pause_mow"]


async def test_in_flight_duplicate_waits_for_the_first() -> None:
    h = Harness()
    h.conn.gate = asyncio.Event()
    envelope = h.envelope(ReturnToDock())
    first = asyncio.create_task(h.controller.handle(envelope))
    second = asyncio.create_task(h.controller.handle(envelope))
    await _settle()
    h.conn.gate.set()
    assert await first == await second
    assert h.conn.calls.count("start:return_to_dock") == 1


async def test_dedup_memory_is_bounded() -> None:
    h = Harness()
    h.controller = MowerController(h.conn, mower_id=h.mower_id, clock=h.clock, dedup_size=2)
    oldest = h.envelope(PauseMow())
    await h.controller.handle(oldest)
    await h.send(ResumeMow())
    await h.send(ReturnToDock())
    await h.controller.handle(oldest)  # forgotten: executed again
    assert h.conn.calls.count("start:pause_mow") == 2
    with pytest.raises(ValueError, match="dedup_size"):
        MowerController(h.conn, mower_id=h.mower_id, dedup_size=0)


async def test_cancelled_command_is_not_cached() -> None:
    h = Harness()
    h.conn.gate = asyncio.Event()
    envelope = h.envelope(ReturnToDock())
    task = asyncio.create_task(h.controller.handle(envelope))
    await _settle()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    h.conn.gate.set()
    result = await h.controller.handle(envelope)
    assert result.status is CommandStatus.EXECUTED


async def test_normal_commands_are_serialized() -> None:
    h = Harness()
    h.conn.gate = asyncio.Event()
    first = asyncio.create_task(h.send(PauseMow()))
    second = asyncio.create_task(h.send(ResumeMow()))
    await _settle()
    assert h.conn.calls == ["start:pause_mow"]
    h.conn.gate.set()
    results = await asyncio.gather(first, second)
    assert [r.status for r in results] == [CommandStatus.EXECUTED] * 2
    assert h.conn.calls == [
        "start:pause_mow",
        "end:pause_mow",
        "start:resume_mow",
        "end:resume_mow",
    ]


async def test_command_expiring_in_the_queue_is_dropped() -> None:
    h = Harness()
    h.conn.gate = asyncio.Event()
    first = asyncio.create_task(h.send(PauseMow()))
    second = asyncio.create_task(h.send(StartMow(area_ids=["front"]), ttl=2))
    await _settle()
    h.clock.advance(3)
    h.conn.gate.set()
    assert (await first).status is CommandStatus.EXECUTED
    late = await second
    assert late.status is CommandStatus.EXPIRED
    assert late.reason == "command expired while waiting in the queue"
    assert "start:start_mow" not in h.conn.calls


async def test_emergency_stop_preempts_running_and_queued_commands() -> None:
    h = Harness()
    h.conn.gate = asyncio.Event()
    running = asyncio.create_task(h.send(StartMow(area_ids=["front"])))
    queued = asyncio.create_task(h.send(SetSpeed(mps=0.5)))
    await _settle()
    stop = await h.send(EmergencyStop())
    assert stop.status is CommandStatus.EXECUTED
    assert h.conn.calls == ["start:start_mow", "start:emergency_stop", "end:emergency_stop"]
    h.conn.gate.set()
    assert (await running).status is CommandStatus.EXECUTED
    preempted = await queued
    assert preempted.status is CommandStatus.REJECTED
    assert preempted.reason == "preempted by an emergency stop"
    assert "start:set_speed" not in h.conn.calls
    assert (await h.send(SetSpeed(mps=0.5))).status is CommandStatus.EXECUTED


async def test_emergency_stop_preempts_a_drive_session() -> None:
    h = Harness()
    assert (await h.send(ManualDrive(linear=0.5, angular=0))).status is CommandStatus.ACCEPTED
    session = h.conn.sessions[0]
    assert h.controller.drive_session_active
    result = await h.send(EmergencyStop())
    assert result.status is CommandStatus.EXECUTED
    assert session.stops == 1
    assert session.closed
    assert not h.controller.drive_session_active
    assert h.timers.pending == 0
    assert h.conn.calls == ["drive_session", "start:emergency_stop", "end:emergency_stop"]


async def test_emergency_stop_preempts_a_simulated_mow() -> None:
    clock = ManualClock()
    sim = SimulatedMower(clock=clock)
    controller = MowerController(sim, mower_id=sim.mower_id, clock=clock)
    async with controller:
        start = CommandEnvelope.create(
            mower_id=sim.mower_id,
            account_id=sim.account_id,
            issued_by=ISSUER,
            command=StartMow(area_ids=["front"]),
            now=clock(),
        )
        assert (await controller.handle(start)).status is CommandStatus.EXECUTED
        sim.tick(60)
        assert sim.blade_on
        stop = CommandEnvelope.create(
            mower_id=sim.mower_id,
            account_id=sim.account_id,
            issued_by=ISSUER,
            command=EmergencyStop(),
            now=clock(),
        )
        assert (await controller.handle(stop)).status is CommandStatus.EXECUTED
        state = await sim.get_state()
        assert state.activity is MowerActivity.PAUSED
        assert not sim.blade_on
    assert not sim.is_connected


async def test_dead_man_timeout_stops_the_drive_without_sleeping() -> None:
    h = Harness()
    await h.send(ManualDrive(linear=1, angular=0, ttl_ms=500))
    session = h.conn.sessions[0]
    assert await h.timers.advance(0.4) == 0
    await h.send(ManualDrive(linear=1, angular=0.2, ttl_ms=500))  # re-arms the timer
    assert await h.timers.advance(0.4) == 0
    assert session.is_open
    assert session.stops == 0
    assert await h.timers.advance(0.2) == 1
    assert session.stops == 1
    assert session.closed
    assert not h.controller.drive_session_active
    assert len(h.conn.sessions) == 1
    await h.send(ManualDrive(linear=0.1, angular=0))  # a new frame opens a new session
    assert len(h.conn.sessions) == 2


async def test_stale_dead_man_timer_is_ignored() -> None:
    h = Harness()
    await h.send(ManualDrive(linear=1, angular=0, ttl_ms=500))
    first = h.conn.sessions[0]
    await h.send(PauseMow())  # ends the session, cancels the timer
    assert first.closed
    await h.send(ManualDrive(linear=1, angular=0, ttl_ms=2000))
    second = h.conn.sessions[1]
    await h.controller._deadman_fired(first)  # a late callback of the old session
    assert second.is_open


async def test_non_drive_command_ends_the_drive_session() -> None:
    h = Harness()
    await h.send(ManualDrive(linear=1, angular=0))
    session = h.conn.sessions[0]
    await h.send(SetBladeHeight(mm=40))
    assert session.closed
    assert h.timers.pending == 0


@pytest.mark.parametrize(
    ("caps_blades", "activity", "requested", "expected", "reason"),
    [
        (True, MowerActivity.IDLE, True, True, None),
        (True, MowerActivity.IDLE, False, False, None),
        (False, MowerActivity.IDLE, True, False, "does not allow blades"),
        (True, MowerActivity.DOCKED, True, False, "transfer"),
        (True, MowerActivity.CHARGING, True, False, "transfer"),
    ],
)
async def test_blade_rules_for_manual_drive(
    caps_blades: bool,
    activity: MowerActivity,
    requested: bool,
    expected: bool,
    reason: str | None,
) -> None:
    caps = MowerCapabilities(manual_drive=True, manual_drive_blades=caps_blades)
    h = Harness(capabilities=caps, activity=activity)
    result = await h.send(ManualDrive(linear=0.3, angular=0, blade_on=requested))
    assert result.status is CommandStatus.ACCEPTED
    assert h.conn.sessions[0].updates == [(0.3, 0.0, expected)]
    if reason is None:
        assert result.reason is None
    else:
        assert reason in (result.reason or "")


async def test_manual_drive_rejections() -> None:
    h = Harness(capabilities=MowerCapabilities())
    result = await h.send(ManualDrive(linear=1, angular=0))
    assert result.reason == "manual driving is not supported"

    h = Harness(activity=MowerActivity.MOWING)
    result = await h.send(ManualDrive(linear=1, angular=0))
    assert result.status is CommandStatus.REJECTED
    assert "pause" in (result.reason or "")

    h = Harness(activity=MowerActivity.ERROR)
    h.conn.error_code = "lifted"
    result = await h.send(ManualDrive(linear=1, angular=0))
    assert result.reason == "sensor stop active: lifted"

    h = Harness()
    h.conn.session_error = CommandRejected("busy")
    result = await h.send(ManualDrive(linear=1, angular=0))
    assert (result.status, result.reason) == (CommandStatus.REJECTED, "busy")
    assert not h.controller.drive_session_active

    h = Harness()
    h.conn.state_error = NotConnected("gone")
    result = await h.send(ManualDrive(linear=1, angular=0))
    assert (result.status, result.reason) == (CommandStatus.FAILED, "mower is not connected")


async def test_drive_frame_errors_end_the_session() -> None:
    h = Harness()
    await h.send(ManualDrive(linear=1, angular=0))
    session = h.conn.sessions[0]
    session.fail = SensorStop("sensor stop active: lifted")
    result = await h.send(ManualDrive(linear=1, angular=0))
    assert (result.status, result.reason) == (CommandStatus.REJECTED, "sensor stop active: lifted")
    assert session.closed
    assert h.timers.pending == 0

    await h.send(ManualDrive(linear=1, angular=0))
    session = h.conn.sessions[1]
    session.fail = RuntimeError("boom")
    session.fail_stop = True  # stop and close errors are logged, not raised
    result = await h.send(ManualDrive(linear=1, angular=0))
    assert (result.status, result.reason) == (CommandStatus.FAILED, "adapter error: RuntimeError")
    assert not h.controller.drive_session_active


async def test_blade_height_and_speed_checked_against_capabilities() -> None:
    h = Harness()
    result = await h.send(SetBladeHeight(mm=80))
    assert result.status is CommandStatus.REJECTED
    assert "outside (25, 70)" in (result.reason or "")
    result = await h.send(SetSpeed(mps=1.5))
    assert result.status is CommandStatus.REJECTED
    assert "outside 0 to 1.0 m/s" in (result.reason or "")
    assert h.conn.calls == []
    assert (await h.send(SetBladeHeight(mm=70))).status is CommandStatus.EXECUTED
    assert (await h.send(SetSpeed(mps=1.0))).status is CommandStatus.EXECUTED


async def test_speed_checked_against_the_minimum_and_rejected_when_not_settable() -> None:
    h = Harness(capabilities=MowerCapabilities(min_speed_mps=0.2, max_speed_mps=0.6))
    result = await h.send(SetSpeed(mps=0.1))
    assert result.status is CommandStatus.REJECTED
    assert "outside 0.2 to 0.6 m/s" in (result.reason or "")
    assert h.conn.calls == []
    assert (await h.send(SetSpeed(mps=0.2))).status is CommandStatus.EXECUTED
    unknown = Harness(capabilities=MowerCapabilities())
    result = await unknown.send(SetSpeed(mps=0.3))
    assert result.status is CommandStatus.REJECTED
    assert "not settable" in (result.reason or "")


async def test_set_light_needs_the_capability_and_runs_in_every_activity() -> None:
    without = Harness()
    result = await without.send(SetLight(on=True))
    assert result.status is CommandStatus.REJECTED
    assert "no switchable light" in (result.reason or "")
    assert without.conn.calls == []
    h = Harness(capabilities=MowerCapabilities(light=True), activity=MowerActivity.ERROR)
    assert (await h.send(SetLight(on=True))).status is CommandStatus.EXECUTED
    # Always sent, also when the same state is requested again.
    assert (await h.send(SetLight(on=True))).status is CommandStatus.EXECUTED
    assert h.conn.calls == ["start:set_light", "end:set_light"] * 2


async def test_set_light_keeps_a_drive_session() -> None:
    h = Harness(
        capabilities=MowerCapabilities(light=True, manual_drive=True, manual_drive_blades=True)
    )
    await h.send(ManualDrive(linear=1, angular=0))
    session = h.conn.sessions[0]
    assert (await h.send(SetLight(on=True))).status is CommandStatus.EXECUTED
    assert not session.closed
    assert h.controller.drive_session_active


async def test_start_mow_checked_against_the_map() -> None:
    h = Harness()
    await h.controller.start()
    assert h.conn.map_loads == 1
    result = await h.send(StartMow(area_ids=["front", "pond"]))
    assert result.status is CommandStatus.REJECTED
    assert result.reason == "unknown area ids: pond"
    assert h.conn.map_loads == 2  # reloaded once before rejecting
    h.conn.areas = ["front", "pond"]
    assert (await h.send(StartMow(area_ids=["front", "pond"]))).status is CommandStatus.EXECUTED
    assert h.conn.map_loads == 3
    unsupported = await h.send(StartMow(area_ids=["front"], pattern={"name": "spiral"}))
    assert unsupported.status is CommandStatus.REJECTED
    assert "spiral" in (unsupported.reason or "")
    supported = await h.send(StartMow(area_ids=["front"], pattern={"name": "stripes"}))
    assert supported.status is CommandStatus.EXECUTED
    assert (await h.send(StartMow(plan_ref="plan-1"))).status is CommandStatus.EXECUTED
    await h.controller.close()


async def test_adapter_errors_become_results() -> None:
    h = Harness()
    h.conn.execute_error = CommandRejected("not now")
    assert (await h.send(PauseMow())).reason == "not now"
    h.conn.execute_error = NotConnected()
    result = await h.send(PauseMow())
    assert (result.status, result.reason) == (CommandStatus.FAILED, "mower is not connected")
    h.conn.execute_error = RuntimeError("boom")
    result = await h.send(PauseMow())
    assert (result.status, result.reason) == (CommandStatus.FAILED, "adapter error: RuntimeError")
    h.conn.execute_error = None
    h.conn.outcome = Outcome.rejected("x" * 600)
    long_reason = (await h.send(PauseMow())).reason
    assert long_reason is not None
    assert len(long_reason) == 500
    assert long_reason.endswith("...")


async def test_internal_error_is_reported_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness()

    async def broken(command: JobCommand) -> str | None:
        raise KeyError(command.kind)

    monkeypatch.setattr(h.controller, "_validate", broken)
    result = await h.send(PauseMow())
    assert (result.status, result.reason) == (CommandStatus.FAILED, "internal error: KeyError")


async def test_decisions_are_logged_with_command_id(caplog: pytest.LogCaptureFixture) -> None:
    h = Harness()
    envelope = h.envelope(SetBladeHeight(mm=99), correlation_id="corr-log")
    with caplog.at_level(logging.INFO, logger="mowdeck_control.controller"):
        await h.controller.handle(envelope)
        await h.controller.handle(envelope)
    records = [r for r in caplog.records if getattr(r, "command_id", None)]
    assert str(envelope.command_id) in records[0].getMessage()
    assert records[0].levelno == logging.WARNING
    assert records[0].__dict__["correlation_id"] == "corr-log"
    assert records[0].__dict__["status"] == "rejected"
    assert "duplicate" in records[-1].getMessage()


async def test_state_pump_forwards_states_and_sensor_stop_ends_drive() -> None:
    received: list[MowerState] = []
    async_received: list[MowerState] = []

    async def async_callback(state: MowerState) -> None:
        async_received.append(state)

    h = Harness()
    h.controller = MowerController(
        h.conn,
        mower_id=h.mower_id,
        clock=h.clock,
        timer_factory=h.timers,
        on_state=received.append,
    )
    await h.controller.start()
    await _settle()
    assert [s.activity for s in received] == [MowerActivity.IDLE]
    stream = h.controller.watch_state()
    assert (await anext(stream)).activity is MowerActivity.IDLE

    await h.send(ManualDrive(linear=1, angular=0))
    session = h.conn.sessions[0]
    h.conn.activity = MowerActivity.ERROR
    h.conn.error_code = "lifted"
    h.conn.broadcaster.publish(h.conn.state())
    await _settle()
    assert session.closed
    assert h.controller.last_state is not None
    assert h.controller.last_state.activity is MowerActivity.ERROR
    assert (await anext(stream)).error_code == "lifted"
    await h.controller.close()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    h2 = Harness()
    controller = MowerController(h2.conn, mower_id=h2.mower_id, on_state=async_callback)
    async with controller:
        await _settle()
    assert len(async_received) == 1


async def test_state_callback_and_stream_failures_are_contained(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def failing(state: MowerState) -> None:
        raise RuntimeError("callback broke")

    h = Harness()
    controller = MowerController(h.conn, mower_id=h.mower_id, on_state=failing)
    with caplog.at_level(logging.ERROR, logger="mowdeck_control.controller"):
        async with controller:
            await _settle()
    assert "state callback failed" in caplog.text

    class BrokenStream(FakeConnection):
        def watch_state(self) -> StateSubscription:
            raise RuntimeError("no stream")

    broken = BrokenStream(h.mower_id)
    with caplog.at_level(logging.ERROR, logger="mowdeck_control.controller"):
        async with MowerController(broken, mower_id=h.mower_id) as controller:
            await _settle()
            assert controller.last_state is None
    assert "state stream" in caplog.text


async def test_lazy_capabilities_map_and_properties() -> None:
    h = Harness()
    assert h.controller.mower_id == h.mower_id
    assert h.controller.connection is h.conn
    assert h.controller.last_state is None
    assert (await h.controller.capabilities()).manual_drive
    assert (await h.controller.mower_map()).area_ids == ["front"]
    assert (await h.controller.mower_map()).area_ids == ["front"]
    assert h.conn.map_loads == 1
    await h.controller.close()  # close without start is fine


class RebuildingConnection(FakeConnection):
    """A connection with :class:`MapRebuildable`: ``current_map`` reads ``self.areas``."""

    def __init__(self, mower_id: UUID) -> None:
        super().__init__(mower_id)
        self.rebuilds = 0

    def current_map(self) -> MowerMap | None:
        self.rebuilds += 1
        return MowerMap(
            reference=GeoReference(origin_lat=48, origin_lon=11),
            areas=[MapArea(id=a, boundary=SQUARE) for a in self.areas],
        )


async def test_mower_map_rebuild_uses_the_adapter_data_without_a_reload() -> None:
    conn = RebuildingConnection(uuid4())
    controller = MowerController(conn, mower_id=conn.mower_id)
    assert isinstance(conn, MapRebuildable)
    assert (await controller.mower_map(rebuild=True)).area_ids == ["front"]
    assert (conn.map_loads, conn.rebuilds) == (1, 0)  # first load reads the map
    conn.areas = ["front", "back"]
    assert (await controller.mower_map()).area_ids == ["front"]  # cached
    assert (await controller.mower_map(rebuild=True)).area_ids == ["front", "back"]
    assert (await controller.mower_map()).area_ids == ["front", "back"]  # rebuild is cached
    assert conn.map_loads == 1


async def test_mower_map_rebuild_without_the_capability_keeps_the_cache() -> None:
    h = Harness()
    assert current_map_of(h.conn) is None
    await h.controller.mower_map()
    h.conn.areas = ["back"]
    assert (await h.controller.mower_map(rebuild=True)).area_ids == ["front"]
    assert h.conn.map_loads == 1


async def _collect(stream: AsyncIterator[MowerState], count: int) -> list[MowerState]:
    return [await anext(stream) for _ in range(count)]


async def test_controller_with_simulator_end_to_end() -> None:
    clock = ManualClock()
    sim = SimulatedMower(clock=clock)
    async with MowerController(sim, mower_id=sim.mower_id, clock=clock) as controller:
        await _settle()
        stream = controller.watch_state()
        envelope = CommandEnvelope.create(
            mower_id=sim.mower_id,
            account_id=sim.account_id,
            issued_by=ISSUER,
            command=StartMow(area_ids=["back"], pattern={"name": "checkerboard"}),
            now=clock(),
        )
        assert (await controller.handle(envelope)).status is CommandStatus.EXECUTED
        sim.tick(10)
        await _settle()
        states = await _collect(stream, 3)
        assert states[-1].activity is MowerActivity.MOWING


def _spot(native: SpotNativeJob | None = None) -> SpotMowPlan:
    return SpotMowPlan.build(
        [
            SpotSegment.from_points(
                [(1, 1), (4, 1), (4, 1.35), (1, 1.35)], blades_on=True, speed_mps=0.4
            )
        ],
        ["stripes"],
        native=native,
    )


async def test_spot_plans_run_as_routes_only_with_custom_paths() -> None:
    h = Harness(
        capabilities=MowerCapabilities(native_patterns=frozenset({"stripes"}), custom_paths=True)
    )
    await h.controller.start()
    assert (await h.send(StartMow(spot_plan=_spot()))).status is CommandStatus.EXECUTED
    assert h.conn.calls[-1] == "end:start_mow"
    await h.controller.close()


async def test_spot_plans_without_custom_paths_need_a_native_job() -> None:
    sent: list[JobCommand] = []
    h = Harness()
    original = h.conn.execute

    async def record(command: JobCommand) -> Outcome:
        sent.append(command)
        return await original(command)

    h.conn.execute = record  # type: ignore[method-assign]
    await h.controller.start()
    refused = await h.send(StartMow(spot_plan=_spot()))
    assert refused.status is CommandStatus.REJECTED
    assert "custom paths not supported" in (refused.reason or "")
    unknown = await h.send(
        StartMow(spot_plan=_spot(SpotNativeJob(area_ids=["pond"], pattern={"name": "stripes"})))
    )
    assert unknown.reason == "unknown area ids: pond"
    native = SpotNativeJob(area_ids=["front"], pattern={"name": "stripes", "angle_deg": 30})
    accepted = await h.send(StartMow(spot_plan=_spot(native)))
    assert accepted.status is CommandStatus.EXECUTED
    assert len(sent) == 1
    command = sent[0]
    assert isinstance(command, StartMow)
    assert command.spot_plan is None
    assert command.area_ids == ["front"]
    assert command.pattern == {"name": "stripes", "angle_deg": 30}
    await h.controller.close()


# --- Confirmation waits outside the queue (rules 5 and 9) ------------------------------------


class SlowStart:
    """A controller on a simulator whose job start is confirmed late or never."""

    def __init__(self, *, start_delay_s: float = math.inf, confirm_timeout_s: float = 60) -> None:
        self.clock = ManualClock()
        self.sim = SimulatedMower(
            clock=self.clock,
            config=SimulationConfig(
                start_delay_s=start_delay_s, confirm_timeout_s=confirm_timeout_s
            ),
        )
        self.controller = MowerController(
            self.sim,
            mower_id=self.sim.mower_id,
            clock=self.clock,
            timer_factory=ManualTimers(self.clock),
        )

    def envelope(self, command: MowerCommand, *, ttl: float = 30) -> CommandEnvelope:
        return CommandEnvelope.create(
            mower_id=self.sim.mower_id,
            account_id=self.sim.account_id,
            issued_by=ISSUER,
            command=command,
            ttl=timedelta(seconds=ttl),
            now=self.clock(),
        )

    async def send(self, command: MowerCommand, **kwargs: Any) -> CommandResult:
        return await self.controller.handle(self.envelope(command, **kwargs))

    async def start(self) -> "asyncio.Task[CommandResult]":
        task = asyncio.create_task(self.send(StartMow(area_ids=["front"])))
        await _settle()
        assert not task.done()  # sent, waiting for mowing
        return task


async def test_pause_seven_seconds_after_an_unconfirmed_start_runs_at_once() -> None:
    """The incident of 2026-10-10: a start whose confirmation never came held the queue."""
    s = SlowStart()
    await s.controller.start()
    start = await s.start()
    s.clock.advance(7)  # the owner presses pause 7 s later
    loop = asyncio.get_running_loop()
    began = loop.time()
    pause = await asyncio.wait_for(s.send(PauseMow()), 1.0)
    assert loop.time() - began < 1.0
    assert pause.status is CommandStatus.EXECUTED
    started = await asyncio.wait_for(start, 1.0)
    assert started.status is CommandStatus.ACCEPTED
    assert started.reason == "start_mow sent; confirmation interrupted by pause_mow"
    s.sim.tick(60)
    assert s.sim.snapshot().activity is not MowerActivity.MOWING
    await s.controller.close()


@pytest.mark.parametrize(
    ("command", "kind"),
    [(ReturnToDock(), "return_to_dock"), (EmergencyStop(), "emergency_stop")],
)
async def test_return_or_emergency_stop_supersede_a_waiting_start(
    command: MowerCommand, kind: str
) -> None:
    s = SlowStart()
    await s.controller.start()
    start = await s.start()
    s.clock.advance(7)
    result = await asyncio.wait_for(s.send(command, ttl=10), 1.0)
    assert result.status is CommandStatus.EXECUTED
    started = await asyncio.wait_for(start, 1.0)
    assert started.status is CommandStatus.ACCEPTED
    assert started.reason == f"start_mow sent; confirmation interrupted by {kind}"
    await s.controller.close()


async def test_return_to_dock_with_end_job_passes_and_supersedes_like_a_plain_return() -> None:
    s = SlowStart()
    await s.controller.start()
    start = await s.start()
    s.clock.advance(7)
    result = await asyncio.wait_for(s.send(ReturnToDock(end_job=True), ttl=10), 1.0)
    assert result.status is CommandStatus.EXECUTED
    started = await asyncio.wait_for(start, 1.0)
    assert started.reason == "start_mow sent; confirmation interrupted by return_to_dock"
    await s.controller.close()


async def test_a_waiting_confirmation_lets_later_commands_run_before_they_expire() -> None:
    s = SlowStart(start_delay_s=5)
    await s.controller.start()
    start = await s.start()
    s.clock.advance(3)
    height = await asyncio.wait_for(s.send(SetBladeHeight(mm=50), ttl=2), 1.0)
    assert height.status is CommandStatus.EXECUTED  # ran at once, did not expire in a queue
    assert not start.done()  # settings leave the confirmation alone
    s.sim.tick(5)
    started = await asyncio.wait_for(start, 1.0)
    assert started.status is CommandStatus.EXECUTED
    assert started.reason == "mower is mowing"
    await s.controller.close()


async def test_unconfirmed_start_is_accepted_after_the_timeout() -> None:
    s = SlowStart(confirm_timeout_s=0.05)
    await s.controller.start()
    result = await asyncio.wait_for(s.send(StartMow(area_ids=["front"])), 1.0)
    assert result.status is CommandStatus.ACCEPTED
    assert "did not report mowing" in (result.reason or "")
    await s.controller.close()


async def test_manual_drive_is_rejected_while_a_start_waits_for_confirmation() -> None:
    s = SlowStart()
    await s.controller.start()
    start = await s.start()
    frame = await s.send(ManualDrive(linear=0.5, angular=0, ttl_ms=500), ttl=2)
    assert frame.status is CommandStatus.REJECTED
    assert "waiting for its confirmation" in (frame.reason or "")
    assert not s.controller.drive_session_active
    assert not start.done()
    await s.controller.close()
    assert (await start).reason == "start_mow sent; confirmation interrupted by controller close"


async def test_duplicate_of_a_waiting_command_gets_its_result() -> None:
    s = SlowStart()
    await s.controller.start()
    envelope = s.envelope(StartMow(area_ids=["front"]))
    first = asyncio.create_task(s.controller.handle(envelope))
    await _settle()
    second = asyncio.create_task(s.controller.handle(envelope))
    await _settle()
    assert not second.done()
    await s.send(PauseMow())
    assert await first == await second
    await s.controller.close()


async def test_cancelled_caller_cancels_its_confirmation_wait() -> None:
    s = SlowStart()
    await s.controller.start()
    envelope = s.envelope(StartMow(area_ids=["front"]))
    task = asyncio.create_task(s.controller.handle(envelope))
    await _settle()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle()
    assert s.controller._confirming == {}
    await s.controller.close()


async def test_expiry_still_applies_while_a_confirmation_waits() -> None:
    s = SlowStart()
    await s.controller.start()
    start = await s.start()
    stale = s.envelope(SetSpeed(mps=0.5), ttl=2)
    s.clock.advance(3)
    result = await s.controller.handle(stale)
    assert result.status is CommandStatus.EXPIRED
    assert result.reason == "command expired before delivery"
    assert not start.done()
    await s.controller.close()
