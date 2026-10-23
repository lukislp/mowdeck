"""The mower's own settings (ADR 0046): the controller's rule 14 and the simulator."""

from datetime import timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    ManualDrive,
    MowerActivity,
    MowerCommand,
    PrincipalKind,
    PrincipalRef,
    SelfCheckResult,
    SetSetting,
    StartMow,
    TimeWindow,
)
from mowdeck_control import (
    CommandRejected,
    MowerController,
    Outcome,
    SimulatedMower,
    SimulationConfig,
    settings_port_of,
)
from mowdeck_control.simulator import RAIN_BLOCKS_START, SIMULATED_SETTING_DEFAULTS

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)


async def _send(
    controller: MowerController, mower: SimulatedMower, command: MowerCommand
) -> CommandResult:
    envelope = CommandEnvelope.create(
        mower_id=mower.mower_id,
        account_id=mower.account_id,
        issued_by=ISSUER,
        command=command,
        ttl=timedelta(seconds=30),
    )
    return await controller.handle(envelope)


async def test_simulator_declares_and_reports_its_settings() -> None:
    mower = SimulatedMower()
    await mower.connect()
    caps = await mower.capabilities()
    assert {s.key for s in caps.settings} == set(SIMULATED_SETTING_DEFAULTS)
    state = await mower.get_state()
    assert state.settings == dict(SIMULATED_SETTING_DEFAULTS)
    assert state.rain_detected is False
    assert settings_port_of(mower) is mower


async def test_simulator_without_settings_declares_none() -> None:
    mower = SimulatedMower(config=SimulationConfig(settings=False))
    await mower.connect()
    assert (await mower.capabilities()).settings == []
    assert (await mower.get_state()).settings == {}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("rain_detection", False),
        ("recharge_level", 25),
        ("turn_style", "multi_point"),
        ("non_work_hours", TimeWindow(start="22:00", end="06:00")),
        ("non_work_hours", None),
    ],
)
async def test_set_setting_reaches_the_mower(key: str, value: object) -> None:
    mower = SimulatedMower()
    async with MowerController(mower, mower_id=mower.mower_id) as controller:
        result = await _send(controller, mower, SetSetting(key=key, value=value))  # type: ignore[arg-type]
        assert result.status is CommandStatus.EXECUTED, result.reason
        assert (await mower.get_state()).settings[key] == value


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        (SetSetting(key="voice_gender", value="woman"), "this mower has no setting 'voice_gender'"),
        (SetSetting(key="recharge_level", value=50), "recharge_level must lie within 15..30"),
        (SetSetting(key="rain_detection", value=1), "rain_detection takes true or false"),
        (
            SetSetting(key="turn_style", value="arc"),
            "turn_style takes one of in_place, multi_point",
        ),
    ],
)
async def test_set_setting_is_checked_against_the_declaration(
    command: SetSetting, reason: str
) -> None:
    mower = SimulatedMower()
    async with MowerController(mower, mower_id=mower.mower_id) as controller:
        result = await _send(controller, mower, command)
        assert result.status is CommandStatus.REJECTED
        assert result.reason == reason
    assert mower.settings == SIMULATED_SETTING_DEFAULTS


async def test_set_setting_needs_a_settings_port() -> None:
    class NoSettings(SimulatedMower):
        """Declares settings but does not implement the protocol."""

        set_setting = None  # type: ignore[assignment]

    mower = NoSettings()
    assert settings_port_of(mower) is None
    async with MowerController(mower, mower_id=mower.mower_id) as controller:
        result = await _send(controller, mower, SetSetting(key="rain_detection", value=False))
    assert result.status is CommandStatus.REJECTED
    assert result.reason == "this mower has no setting 'rain_detection'"


async def test_set_setting_keeps_the_drive_session_and_works_during_an_error() -> None:
    mower = SimulatedMower()
    await mower.connect()
    mower.inject_stuck()
    async with MowerController(mower, mower_id=mower.mower_id) as controller:
        drive = await _send(controller, mower, ManualDrive(linear=-0.3, angular=0.0))
        assert drive.status is CommandStatus.ACCEPTED, drive.reason
        assert controller.drive_session_active
        result = await _send(controller, mower, SetSetting(key="night_light", value=True))
        assert result.status is CommandStatus.EXECUTED
        assert controller.drive_session_active
        assert (await mower.get_state()).activity is MowerActivity.ERROR


async def test_set_setting_reports_adapter_outcomes() -> None:
    class Refusing(SimulatedMower):
        def __init__(self, error: Exception | None, outcome: Outcome | None = None) -> None:
            super().__init__()
            self.error = error
            self.outcome = outcome

        async def set_setting(self, key: str, value: object) -> Outcome:
            if self.error is not None:
                raise self.error
            assert self.outcome is not None
            return self.outcome

    cases = [
        (CommandRejected("busy"), CommandStatus.REJECTED, "busy"),
        (RuntimeError("boom"), CommandStatus.FAILED, "adapter error: RuntimeError"),
    ]
    for error, status, reason in cases:
        mower = Refusing(error)
        async with MowerController(mower, mower_id=mower.mower_id) as controller:
            result = await _send(controller, mower, SetSetting(key="rain_detection", value=True))
        assert (result.status, result.reason) == (status, reason)
    mower = Refusing(None, Outcome.accepted("sent; not confirmed"))
    async with MowerController(mower, mower_id=mower.mower_id) as controller:
        result = await _send(controller, mower, SetSetting(key="rain_detection", value=True))
    assert (result.status, result.reason) == (CommandStatus.ACCEPTED, "sent; not confirmed")


async def test_rain_sensor_blocks_a_start_only_with_rain_detection_on() -> None:
    mower = SimulatedMower(config=SimulationConfig(raining=True))
    async with MowerController(mower, mower_id=mower.mower_id) as controller:
        state = await mower.get_state()
        assert state.rain_detected is True
        assert state.self_check is not None
        assert (state.self_check.result, state.self_check.detail) == (
            SelfCheckResult.BLOCKED,
            "rain",
        )
        start = await _send(controller, mower, StartMow(area_ids=["front"]))
        assert (start.status, start.reason) == (CommandStatus.REJECTED, RAIN_BLOCKS_START)
        off = await _send(controller, mower, SetSetting(key="rain_detection", value=False))
        assert off.status is CommandStatus.EXECUTED
        state = await mower.get_state()
        assert state.self_check is not None and state.self_check.result is SelfCheckResult.OK
        assert (await _send(controller, mower, StartMow(area_ids=["front"]))).status is (
            CommandStatus.EXECUTED
        )
        mower.set_raining(False)
        assert (await mower.get_state()).rain_detected is False
