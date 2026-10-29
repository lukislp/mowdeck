"""The connection of one mower: state, commands and their translation, vendor errors."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from aioautomower.exceptions import (
    ApiBadRequestError,
    ApiError,
    ApiForbiddenError,
    ApiUnauthorizedError,
    FeatureNotSupportedError,
)
from husqvarna_support import HUGO, ManualClock, attributes
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandStatus,
    ConnectionKind,
    EmergencyStop,
    MowerActivity,
    MowerState,
    PauseMow,
    PrincipalKind,
    PrincipalRef,
    ResumeMow,
    ReturnToDock,
    SetBladeHeight,
    SetLight,
    StartMow,
)
from mowdeck_control import (
    CommandRejected,
    ConnectionConfig,
    JobCommand,
    MapRebuildable,
    MowerConnection,
    MowerController,
    NotConnected,
    SensorStop,
)
from mowdeck_control_husqvarna.account import ActionKind, MowerAction
from mowdeck_control_husqvarna.auth import RateLimitedError
from mowdeck_control_husqvarna.connection import (
    NO_HARD_STOP,
    HusqvarnaConnection,
    action_for_plan,
)
from mowdeck_control_husqvarna.fake_cloud import (
    FAKE_APP_KEY,
    FAKE_APP_SECRET,
    FAKE_MOWER_OFFLINE,
    FAKE_MOWER_ONLINE,
    FakeHusqvarnaCloud,
)
from mowdeck_control_husqvarna.options import HusqvarnaOptions
from mowdeck_control_husqvarna.pool import AccountPool

MOWER_ID, ACCOUNT_ID = uuid4(), uuid4()


def _options(**extra: Any) -> dict[str, Any]:
    return {
        "cloud_provider": "husqvarna",
        "app_key": FAKE_APP_KEY,
        "app_secret": FAKE_APP_SECRET,
        "iot_id": FAKE_MOWER_ONLINE,
        "device_name": "Hugo",
        **extra,
    }


def _connection(
    pool: AccountPool, mower_id: str = FAKE_MOWER_ONLINE, **extra: Any
) -> HusqvarnaConnection:
    options = _options(iot_id=mower_id, **extra)
    config = ConnectionConfig(mower_id=MOWER_ID, account_id=ACCOUNT_ID, options=options)
    return HusqvarnaConnection(pool, config, HusqvarnaOptions.parse(options))


@pytest.fixture
def cloud() -> FakeHusqvarnaCloud:
    return FakeHusqvarnaCloud(park_seconds=0.05)


@pytest.fixture
async def pool(cloud: FakeHusqvarnaCloud) -> AsyncIterator[AccountPool]:
    clock = ManualClock()
    pool = AccountPool(cloud.opener(clock=clock, sleep=clock.sleep), linger_seconds=0)
    yield pool
    await pool.close()


async def _until(
    connection: HusqvarnaConnection, check: Callable[[MowerState], bool]
) -> MowerState:
    for _ in range(200):
        state = await connection.get_state()
        if check(state):
            return state
        await asyncio.sleep(0.01)
    raise AssertionError(f"state never matched: {await connection.get_state()}")


async def test_connection_reports_the_docked_mower(pool: AccountPool) -> None:
    connection = _connection(pool)
    assert isinstance(connection, MowerConnection)
    assert isinstance(connection, MapRebuildable)
    assert connection.kind is ConnectionKind.CLOUD
    with pytest.raises(NotConnected):
        await connection.get_state()
    await connection.connect()
    await connection.connect()  # idempotent
    state = await connection.get_state()
    assert state.activity is MowerActivity.DOCKED
    assert state.mower_id == MOWER_ID and state.account_id == ACCOUNT_ID
    assert (await connection.capabilities()).model == "Automower 430X NERA"
    mower_map = await connection.get_map()
    assert mower_map.areas == [] and mower_map.reference.origin_lat == pytest.approx(52.52045)
    assert connection.husqvarna_id == FAKE_MOWER_ONLINE
    await connection.disconnect()
    assert not connection.is_connected


async def test_the_mower_is_found_by_name_when_its_id_is_missing(pool: AccountPool) -> None:
    connection = _connection(pool, mower_id="")
    await connection.connect()
    assert connection.husqvarna_id == FAKE_MOWER_ONLINE
    await connection.disconnect()


async def test_a_mower_not_on_the_account_does_not_connect(pool: AccountPool) -> None:
    connection = _connection(pool, mower_id="not-there", device_name="Nobody")
    with pytest.raises(NotConnected, match="not on this Husqvarna account"):
        await connection.connect()
    assert pool.open_sessions == 0


async def test_refused_credentials_do_not_connect(pool: AccountPool) -> None:
    connection = _connection(pool, app_secret="wrong")  # noqa: S106 - a test value
    with pytest.raises(NotConnected, match="refused"):
        await connection.connect()


async def test_commands_reach_the_cloud_and_the_state_follows(
    pool: AccountPool, cloud: FakeHusqvarnaCloud
) -> None:
    connection = _connection(pool)
    await connection.connect()
    try:
        outcome = await connection.execute(StartMow(plan_ref="duration:90"))
        assert outcome.status is CommandStatus.ACCEPTED
        await _until(connection, lambda s: s.activity is MowerActivity.MOWING)
        assert (await connection.get_state()).raw["blade_on"] is True

        await connection.execute(PauseMow())
        await _until(connection, lambda s: s.activity is MowerActivity.PAUSED)
        await connection.execute(ResumeMow())
        await _until(connection, lambda s: s.activity is MowerActivity.MOWING)
        await connection.execute(ReturnToDock())
        await _until(connection, lambda s: s.activity is MowerActivity.RETURNING)
        await _until(connection, lambda s: s.activity is MowerActivity.DOCKED)
        await connection.execute(StartMow(plan_ref="work_area:654321:30"))
        await connection.execute(ReturnToDock(end_job=True))
        await connection.execute(StartMow(plan_ref="schedule"))
    finally:
        await connection.disconnect()
    assert [a for _, a in cloud.actions] == [
        {"type": "Start", "attributes": {"duration": 90}},
        {"type": "Pause"},
        {"type": "ResumeSchedule"},
        {"type": "ParkUntilNextSchedule"},
        {"type": "StartInWorkArea", "attributes": {"duration": 30, "workAreaId": 654321}},
        {"type": "ParkUntilFurtherNotice"},
        {"type": "ResumeSchedule"},
    ]


async def test_watch_state_pushes_changes(pool: AccountPool, cloud: FakeHusqvarnaCloud) -> None:
    connection = _connection(pool)
    await connection.connect()
    watch = connection.watch_state()
    try:
        first = await anext(watch)
        assert first.activity is MowerActivity.DOCKED
        cloud.update(FAKE_APP_KEY, FAKE_MOWER_ONLINE, state="ERROR", errorCode=71)
        while True:
            state = await asyncio.wait_for(anext(watch), timeout=2)
            if state.activity is MowerActivity.ERROR:
                break
        assert state.raw["lifted"] is True and state.raw["lift_code"] == "71"
        with pytest.raises(SensorStop):
            await connection.execute(StartMow(plan_ref="duration"))
        with pytest.raises(SensorStop):
            await connection.execute(ResumeMow())
        assert (await connection.execute(PauseMow())).status is CommandStatus.ACCEPTED
    finally:
        await connection.disconnect()
    with pytest.raises(StopAsyncIteration):
        await anext(watch)


async def test_emergency_stop_pauses_at_once_and_says_there_is_no_hard_stop(
    cloud: FakeHusqvarnaCloud,
) -> None:
    clock = ManualClock()
    pool = AccountPool(cloud.opener(clock=clock, sleep=clock.sleep), linger_seconds=0)
    connection = _connection(pool)
    await connection.connect()
    try:
        await connection.execute(StartMow(plan_ref="duration"))
        waited = list(clock.sleeps)
        outcome = await connection.execute(EmergencyStop())
        assert outcome.status is CommandStatus.ACCEPTED and outcome.reason == NO_HARD_STOP
        assert clock.sleeps == waited  # the throttle did not hold it back
        assert cloud.actions[-1][1] == {"type": "Pause"}
    finally:
        await connection.disconnect()
        await pool.close()


async def test_an_offline_mower_rejects_commands_but_takes_the_emergency_stop(
    pool: AccountPool, cloud: FakeHusqvarnaCloud
) -> None:
    connection = _connection(pool, mower_id=FAKE_MOWER_OFFLINE, device_name="Greta")
    await connection.connect()
    try:
        assert (await connection.get_state()).activity is MowerActivity.OFFLINE
        outcome = await connection.execute(StartMow(plan_ref="duration"))
        assert outcome.status is CommandStatus.REJECTED
        assert "not connected" in (outcome.reason or "")
        assert cloud.actions == []
        stop = await connection.execute(EmergencyStop())
        assert stop.status is CommandStatus.ACCEPTED
        assert cloud.actions == [(FAKE_MOWER_OFFLINE, {"type": "Pause"})]
    finally:
        await connection.disconnect()


@pytest.mark.parametrize(
    "command",
    [SetBladeHeight(mm=40), SetLight(on=True)],
)
async def test_unsupported_commands_are_rejected(pool: AccountPool, command: JobCommand) -> None:
    connection = _connection(pool)
    await connection.connect()
    try:
        outcome = await connection.execute(command)
        assert outcome.status is CommandStatus.REJECTED
        assert "not supported" in (outcome.reason or "")
        with pytest.raises(CommandRejected, match="areas"):
            await connection.execute(StartMow(area_ids=["front"]))
        with pytest.raises(CommandRejected, match="manual driving"):
            await connection.start_drive_session()
    finally:
        await connection.disconnect()


@pytest.mark.parametrize(
    ("error", "status", "words"),
    [
        (ApiForbiddenError("HTTP 403"), CommandStatus.REJECTED, "403"),
        (ApiBadRequestError("HTTP 400"), CommandStatus.REJECTED, "current state"),
        (FeatureNotSupportedError("no"), CommandStatus.REJECTED, "does not support"),
        (RateLimitedError("HTTP 429"), CommandStatus.FAILED, "rate limit"),
        (ApiUnauthorizedError("HTTP 401"), CommandStatus.FAILED, "re-authenticate"),
        (ApiError("HTTP 500"), CommandStatus.FAILED, "did not answer"),
    ],
)
async def test_vendor_errors_become_outcomes(
    pool: AccountPool,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: CommandStatus,
    words: str,
) -> None:
    connection = _connection(pool)
    await connection.connect()
    try:
        account = (await pool.acquire(FAKE_APP_KEY, FAKE_APP_SECRET)).account

        async def refuse(mower_id: str, action: MowerAction) -> None:
            raise error

        monkeypatch.setattr(account, "act", refuse)
        outcome = await connection.execute(PauseMow())
        assert outcome.status is status
        assert words in (outcome.reason or "")
        assert FAKE_APP_SECRET not in (outcome.reason or "")
    finally:
        await connection.disconnect()


def test_plan_references() -> None:
    hugo = attributes(HUGO)
    assert action_for_plan("schedule", hugo, 60) == MowerAction(ActionKind.RESUME_SCHEDULE)
    assert action_for_plan("duration", hugo, 45) == MowerAction(ActionKind.START_FOR, minutes=45)
    assert action_for_plan(" duration:15 ", hugo, 45).minutes == 15
    assert action_for_plan("work_area:123456", hugo, 60) == MowerAction(
        ActionKind.START_IN_WORK_AREA, minutes=60, work_area_id=123456
    )
    for bad, words in [
        ("duration:0", "1 to"),
        ("duration:x", "no number"),
        ("work_area:42", "unknown work area 42"),
        ("work_area:front", "unknown work area"),
        ("stripes", "unknown plan reference"),
    ]:
        with pytest.raises(CommandRejected, match=words):
            action_for_plan(bad, hugo, 60)
    lisa = attributes("8f2a6c3e-1d4b-4e5f-a6b7-c8d9e0f1a2b3")
    with pytest.raises(CommandRejected, match="no work areas"):
        action_for_plan("work_area:1", lisa, 60)


def test_options_need_credentials_and_a_mower() -> None:
    parsed = HusqvarnaOptions.parse(_options(start_minutes=30))
    assert parsed.start_minutes == 30 and parsed.mower_id == FAKE_MOWER_ONLINE
    assert FAKE_APP_SECRET not in repr(parsed)
    with pytest.raises(ValueError, match="application key and secret") as caught:
        HusqvarnaOptions.parse({"app_key": FAKE_APP_KEY, "iot_id": "x"})
    assert FAKE_APP_KEY not in str(caught.value)
    with pytest.raises(ValueError, match="iot_id or device_name"):
        HusqvarnaOptions.parse({"app_key": "k", "app_secret": "s"})
    with pytest.raises(ValueError, match="start_minutes"):
        HusqvarnaOptions.parse(_options(start_minutes=0))
    with pytest.raises(ValueError, match="whole number"):
        HusqvarnaOptions.parse(_options(start_minutes="ten"))


async def test_the_controller_runs_the_connection(pool: AccountPool) -> None:
    connection = _connection(pool)
    controller = MowerController(connection, mower_id=MOWER_ID)
    await controller.start()
    try:

        async def send(command: Any) -> Any:
            envelope = CommandEnvelope.create(
                mower_id=MOWER_ID,
                account_id=ACCOUNT_ID,
                issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
                command=command,
                ttl=timedelta(seconds=30),
            )
            return await controller.handle(envelope)

        refused = await send(StartMow(area_ids=["front"]))
        assert refused.status is CommandStatus.REJECTED
        assert "unknown area ids" in (refused.reason or "")
        height = await send(SetBladeHeight(mm=40))
        assert height.status is CommandStatus.REJECTED
        started = await send(StartMow(plan_ref="duration:20"))
        assert started.status is CommandStatus.ACCEPTED
        stopped = await send(EmergencyStop())
        assert stopped.status is CommandStatus.ACCEPTED
    finally:
        await controller.close()
