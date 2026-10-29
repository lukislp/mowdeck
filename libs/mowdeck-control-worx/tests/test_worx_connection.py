"""The connection of one mower against the fake cloud: state, commands, settings, errors."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
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
    SetSetting,
    StartMow,
)
from mowdeck_control import (
    CommandRejected,
    ConnectionConfig,
    MapRebuildable,
    MowerConnection,
    MowerController,
    NotConnected,
    SensorStop,
    SettingsCapable,
)
from mowdeck_control_worx.connection import NO_HARD_STOP, WorxConnection, actions_for_plan
from mowdeck_control_worx.fake_cloud import (
    FAKE_CLOUD_PASSWORD,
    FAKE_MOWER_OFFLINE,
    FAKE_MOWER_ONLINE,
    FakeWorxCloud,
)
from mowdeck_control_worx.options import WorxOptions
from mowdeck_control_worx.pool import AccountPool, LoginGuard
from mowdeck_control_worx.session import (
    ActionKind,
    ChangeListener,
    CloudCommandError,
    NoAnswerError,
    WorxAction,
)
from mowdeck_control_worx.snapshot import WorxMower
from worx_support import WIRE, mower

MOWER_ID, ACCOUNT_ID = uuid4(), uuid4()
EMAIL = "garden@fake.mowdeck.test"


def _options(**extra: Any) -> dict[str, Any]:
    return {
        "cloud_provider": "worx",
        "brand": "worx",
        "email": EMAIL,
        "password": FAKE_CLOUD_PASSWORD,
        "iot_id": FAKE_MOWER_ONLINE,
        "device_name": "Wanda",
        **extra,
    }


def _connection(pool: AccountPool, **extra: Any) -> WorxConnection:
    options = _options(**extra)
    config = ConnectionConfig(mower_id=MOWER_ID, account_id=ACCOUNT_ID, options=options)
    return WorxConnection(pool, config, WorxOptions.parse(options))


@pytest.fixture
def cloud() -> FakeWorxCloud:
    return FakeWorxCloud(home_seconds=0.05)


@pytest.fixture
async def pool(cloud: FakeWorxCloud) -> AsyncIterator[AccountPool]:
    pool = AccountPool(cloud.opener(), linger_seconds=0)
    yield pool
    await pool.close()


async def _until(connection: WorxConnection, check: Callable[[MowerState], bool]) -> MowerState:
    for _ in range(200):
        state = await connection.get_state()
        if check(state):
            return state
        await asyncio.sleep(0.01)
    raise AssertionError(f"state never matched: {await connection.get_state()}")


async def test_connection_reports_the_docked_mower(pool: AccountPool) -> None:
    connection = _connection(pool)
    assert isinstance(connection, MowerConnection)
    assert isinstance(connection, MapRebuildable) and isinstance(connection, SettingsCapable)
    assert connection.kind is ConnectionKind.CLOUD and not connection.is_connected
    with pytest.raises(NotConnected):
        await connection.get_state()
    await connection.connect()
    await connection.connect()  # idempotent
    assert connection.is_connected and connection.serial_number == FAKE_MOWER_ONLINE
    state = await connection.get_state()
    assert state.activity is MowerActivity.DOCKED
    assert state.mower_id == MOWER_ID and state.account_id == ACCOUNT_ID
    assert state.settings == {"rain_delay": 60} and state.blade_height_mm == 45
    caps = await connection.capabilities()
    assert caps.model == "Landroid L 2000"
    assert [p.ref for p in caps.start_plans] == ["now", "zone:1", "zone:2", "edge"]
    mower_map = await connection.get_map()
    assert mower_map.areas == [] and mower_map.reference.origin_lat == pytest.approx(52.52045)
    assert connection.current_map() == mower_map
    await connection.disconnect()
    assert not connection.is_connected


async def test_the_mower_is_picked_by_name_or_refused(pool: AccountPool) -> None:
    by_name = _connection(pool, iot_id="unknown")
    await by_name.connect()
    assert by_name.serial_number == FAKE_MOWER_ONLINE
    gone = _connection(pool, iot_id="unknown", device_name="Nobody")
    with pytest.raises(NotConnected, match="not on this Worx Landroid account"):
        await gone.connect()  # shares the open session: no second login
    assert pool.logins == 1
    wrong = _connection(pool, password="wrong")  # noqa: S106 - a test value
    with pytest.raises(NotConnected, match="refused"):
        await wrong.connect()
    await by_name.disconnect()
    again = _connection(pool)
    with pytest.raises(NotConnected, match="moment ago"):
        await again.connect()  # the session just ended: no new login at once


async def test_start_plans_pause_resume_and_home(pool: AccountPool, cloud: FakeWorxCloud) -> None:
    connection = _connection(pool)
    await connection.connect()
    outcome = await connection.execute(StartMow(plan_ref="zone:2"))
    assert outcome.status is CommandStatus.ACCEPTED and outcome.reason == "zone + start sent"
    state = await _until(connection, lambda s: s.activity is MowerActivity.MOWING)
    assert state.raw["zone"] == 2 and state.raw["blade_on"] is True
    assert [a for _, a in cloud.actions] == [
        WorxAction(ActionKind.ZONE, 1),
        WorxAction(ActionKind.START),
    ]
    assert (await connection.execute(PauseMow())).status is CommandStatus.ACCEPTED
    await _until(connection, lambda s: s.activity is MowerActivity.PAUSED)
    assert (await connection.execute(ResumeMow())).status is CommandStatus.ACCEPTED
    await _until(connection, lambda s: s.activity is MowerActivity.MOWING)
    assert (await connection.execute(ReturnToDock(end_job=True))).status is CommandStatus.ACCEPTED
    await _until(connection, lambda s: s.activity is MowerActivity.RETURNING)
    docked = await _until(connection, lambda s: s.activity is MowerActivity.CHARGING)
    assert docked.charging
    await connection.execute(StartMow(plan_ref="edge"))
    edge = await _until(connection, lambda s: s.raw["worx_status"] == 32)
    assert edge.activity is MowerActivity.MOWING and edge.raw["blade_on"] is True
    await connection.disconnect()


async def test_undeclared_starts_are_rejected(pool: AccountPool) -> None:
    connection = _connection(pool, offer_zones=False)
    await connection.connect()
    with pytest.raises(CommandRejected, match="map areas"):
        await connection.execute(StartMow(area_ids=["front"]))
    with pytest.raises(CommandRejected, match="unknown start plan 'zone:1'"):
        await connection.execute(StartMow(plan_ref="zone:1"))
    with pytest.raises(CommandRejected, match="offers edge, now"):
        await connection.execute(StartMow(plan_ref="schedule"))
    rejected = await connection.execute(SetLight(on=True))
    assert rejected.status is CommandStatus.REJECTED and "not supported" in (rejected.reason or "")
    with pytest.raises(CommandRejected):
        await connection.start_drive_session()
    await connection.disconnect()


def test_plans_translate_into_actions() -> None:
    wire = mower(WIRE)
    assert actions_for_plan("now", wire, offer_zones=True) == [WorxAction(ActionKind.START)]
    assert actions_for_plan(" edge ", wire, offer_zones=True) == [WorxAction(ActionKind.EDGE_CUT)]
    assert actions_for_plan("zone:1", wire, offer_zones=True)[0] == WorxAction(ActionKind.ZONE, 0)
    with pytest.raises(CommandRejected):
        actions_for_plan("zone:3", wire, offer_zones=True)


async def test_blade_height_and_rain_delay(pool: AccountPool, cloud: FakeWorxCloud) -> None:
    connection = _connection(pool)
    await connection.connect()
    outcome = await connection.execute(SetBladeHeight(mm=55))
    assert outcome.status is CommandStatus.ACCEPTED
    await _until(connection, lambda s: s.blade_height_mm == 55)
    with pytest.raises(CommandRejected, match="5 mm steps"):
        await connection.execute(SetBladeHeight(mm=42))
    applied = await connection.set_setting("rain_delay", 120)
    assert applied.status is CommandStatus.ACCEPTED
    await _until(connection, lambda s: s.settings == {"rain_delay": 120})
    with pytest.raises(CommandRejected):
        await connection.set_setting("night_light", True)
    with pytest.raises(CommandRejected):
        await connection.set_setting("rain_delay", True)
    await connection.disconnect()


async def test_through_the_controller(pool: AccountPool) -> None:
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
        started = await send(StartMow(plan_ref="now"))
        assert started.status is CommandStatus.ACCEPTED
        too_high = await send(SetBladeHeight(mm=80))
        assert too_high.status is CommandStatus.REJECTED  # outside the declared 30..60
        setting = await send(SetSetting(key="rain_delay", value=2000))
        assert setting.status is CommandStatus.REJECTED  # outside 0..1440
        unknown = await send(SetSetting(key="night_light", value=True))
        assert unknown.status is CommandStatus.REJECTED
        keep = await send(ReturnToDock(end_job=False))
        assert keep.status is CommandStatus.REJECTED  # Worx home always ends the job
        delay = await send(SetSetting(key="rain_delay", value=30))
        assert delay.status is CommandStatus.ACCEPTED
        stop = await send(EmergencyStop())
        assert stop.status is CommandStatus.ACCEPTED
    finally:
        await controller.close()


async def test_errors_block_starts_and_lifts_reach_the_state(
    pool: AccountPool, cloud: FakeWorxCloud
) -> None:
    connection = _connection(pool)
    await connection.connect()
    cloud.update("worx", EMAIL, FAKE_MOWER_ONLINE, ls=7, le=2)
    state = await _until(connection, lambda s: s.activity is MowerActivity.ERROR)
    assert state.raw["lifted"] is True and state.error_code == "2"
    with pytest.raises(SensorStop, match="mower error 2: Lifted"):
        await connection.execute(StartMow(plan_ref="now"))
    with pytest.raises(SensorStop):
        await connection.execute(ResumeMow())
    stop = await connection.execute(EmergencyStop())
    assert stop.status is CommandStatus.ACCEPTED and stop.reason == NO_HARD_STOP
    await connection.disconnect()


async def test_an_offline_mower_is_rejected_but_the_stop_goes_out(
    pool: AccountPool, cloud: FakeWorxCloud
) -> None:
    connection = _connection(pool, iot_id=FAKE_MOWER_OFFLINE, device_name="Kurt")
    await connection.connect()
    assert (await connection.get_state()).activity is MowerActivity.OFFLINE
    start = await connection.execute(StartMow(plan_ref="now"))
    assert start.status is CommandStatus.REJECTED and "offline" in (start.reason or "")
    assert cloud.actions == []
    stop = await connection.execute(EmergencyStop())
    assert stop.status is CommandStatus.REJECTED  # the cloud refuses it as offline
    assert cloud.actions == [(FAKE_MOWER_OFFLINE, WorxAction(ActionKind.PAUSE))]
    assert (await connection.capabilities()).start_plans[0].ref == "now"
    await connection.disconnect()


class _StubSession:
    """A session whose commands fail in a chosen way."""

    def __init__(self, error: Exception | None, delay: float = 0) -> None:
        self.error, self.delay, self._alive = error, delay, True

    @property
    def alive(self) -> bool:
        return self._alive

    def mowers(self) -> Mapping[str, WorxMower]:
        return {WIRE: mower(WIRE)}

    def changed_at(self, serial_number: str) -> datetime | None:
        return None

    def listen(self, listener: ChangeListener) -> Callable[[], None]:
        return lambda: None

    async def send(self, serial_number: str, action: WorxAction) -> None:
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error

    async def stop(self) -> None:
        self._alive = False


@pytest.mark.parametrize(
    ("error", "delay", "status", "reason"),
    [
        (NoAnswerError("x"), 0, CommandStatus.FAILED, "did not answer start in time"),
        (None, 1, CommandStatus.FAILED, "did not answer start in time"),
        (CloudCommandError("the MQTT link is down"), 0, CommandStatus.FAILED, "MQTT link"),
    ],
)
async def test_vendor_errors_become_outcomes(
    error: Exception | None,
    delay: float,
    status: CommandStatus,
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stub = _StubSession(error, delay)

    async def opener(_: Any) -> _StubSession:
        return stub

    pool = AccountPool(opener, guard=LoginGuard())
    options = _options(iot_id=WIRE, command_timeout_s=5)
    connection = WorxConnection(
        pool,
        ConnectionConfig(mower_id=MOWER_ID, account_id=ACCOUNT_ID, options=options),
        WorxOptions.parse(options),
    )
    await connection.connect()
    if delay:
        connection._options = WorxOptions.parse({**options, "command_timeout_s": 5})
        object.__setattr__(connection._options, "command_timeout_s", 0.05)
    with caplog.at_level(logging.DEBUG):
        outcome = await connection.execute(StartMow(plan_ref="now"))
    assert outcome.status is status and reason in (outcome.reason or "")
    assert FAKE_CLOUD_PASSWORD not in caplog.text and EMAIL not in caplog.text
    assert FAKE_CLOUD_PASSWORD not in repr(connection)
    await connection.disconnect()
    await pool.close()
