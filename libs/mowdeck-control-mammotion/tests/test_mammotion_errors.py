"""Mammotion faults: categories, the error log and warning events, also while the mower pauses.

A trapped LUBA 2 AWD (hardware, 2026-10-18) reports ``MODE_PAUSE`` with no error mode; the fault
(1201 "The robot is stuck") is only in its error log, which the cloud never pushes, and in the
warning events. Mammotion-HA reads the log on entering a pause and on a warning event.
"""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from mammotion_support import ACCOUNT_ID, MOWER_ID, NOW, cloud
from mowdeck_contracts.v1 import (
    CommandStatus,
    ConnectionKind,
    MowerActivity,
    MowerState,
    PauseMow,
    ResumeMow,
)
from mowdeck_control import SensorStop
from mowdeck_control_mammotion import errors, mapping
from mowdeck_control_mammotion.base import ERROR_LOG_ARGS
from mowdeck_control_mammotion.errors import ErrorCategory
from mowdeck_control_mammotion.fake import FakeMammotionPort, error_log_buffer, sample_device
from mowdeck_control_mammotion.port import MowerDevice, WorkMode

SINCE = NOW - timedelta(seconds=mapping.FAULT_TOLERANCE_S)


def _epoch(moment_s: float) -> int:
    return int((NOW + timedelta(seconds=moment_s)).timestamp())


def _paused(*entries: tuple[int, int]) -> MowerDevice:
    """A mower paused on the lawn (off the charger) with ``entries`` in its error log."""
    device = sample_device()
    device.report_data.dev.sys_status = int(WorkMode.MODE_PAUSE)
    device.report_data.dev.charge_state = 0
    device.buffer(error_log_buffer(list(entries)))
    return device


def _state(device: MowerDevice, **kwargs: Any) -> MowerState:
    return mapping.state_of(
        device,
        mower_id=MOWER_ID,
        account_id=ACCOUNT_ID,
        connection=ConnectionKind.CLOUD,
        device_name="Luba-VPDRYRUN",
        firmware="1.30.31.19",
        observed_at=NOW,
        **kwargs,
    )


# --- categories -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (1201, ErrorCategory.TRAPPED),
        ("-1201", ErrorCategory.TRAPPED),
        (11112, ErrorCategory.TRAPPED),
        (1214, ErrorCategory.OBSTACLE),
        (11121, ErrorCategory.OBSTACLE),
        (1100, ErrorCategory.OUTSIDE),
        (1032, ErrorCategory.WHEEL_SLIP),
        (2801, ErrorCategory.LIFT_TILT),
        (1004, ErrorCategory.LIFT_TILT),
        (11131, ErrorCategory.LIFT_TILT),
        (1217, ErrorCategory.COLLISION),
        (1005, ErrorCategory.BATTERY),
        (2713, ErrorCategory.BATTERY),
        (1202, ErrorCategory.BLADE_MOTOR),
        (2741, ErrorCategory.BLADE_MOTOR),  # the cutting height lift motor is no lift stop
        (1003, ErrorCategory.RAIN),
        (1300, ErrorCategory.POSITIONING),
        (99999999, ErrorCategory.OTHER),
        ("mode_lock", ErrorCategory.OTHER),
    ],
)
def test_mammotion_error_category(code: int | str, category: ErrorCategory) -> None:
    assert errors.category_of(code) is category


def test_mammotion_every_known_code_has_a_category_and_only_curated_ones_are_recoverable() -> None:
    known = errors.known_codes()
    assert len(known) > 600  # PyMammotion's whole table
    recoverable = {code for code in known if errors.recoverable(code)}
    assert recoverable == set(errors.RECOVERABLE_CODES)
    for code in errors.LIFT_TILT_CODES:
        assert not errors.recoverable(code)
    for code in (1005, 1006, 1007, 1202, 1216, 2801):  # battery, blades, lift: never
        assert not errors.recoverable(code)


def test_mammotion_warning_codes_are_decoded_like_mammotion_ha() -> None:
    assert errors.warning_codes("device_warning_event", {"code": 1201}) == [1201]
    data = '[{"c": -1214, "ct": 1, "ft": 1760000000000}, {"c": -2801}]'
    assert errors.warning_codes("device_warning_code_event", {"data": data}) == [1214, 2801]
    assert errors.warning_codes("device_warning_code_event", {"data": "not json"}) == []
    assert errors.warning_codes("device_notification_event", {"data": "{}"}) == []
    assert errors.warning_codes("device_warning_event", None) == []


# --- mapping ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "category", "message"),
    [
        (1201, "trapped", "The robot is stuck"),
        (1214, "obstacle", "The channel is obstructed"),
    ],
)
def test_mammotion_paused_mower_with_a_fresh_fault_is_in_error(
    code: int, category: str, message: str
) -> None:
    device = _paused((-code, _epoch(-5)), (-1005, _epoch(-86400)))
    state = _state(device, fault_since=SINCE)
    assert state.activity is MowerActivity.ERROR
    assert state.error_code == str(code)
    assert state.error_message == message
    assert state.drive_recoverable is True
    assert state.raw["error_category"] == category
    assert state.error_category == category  # the typed field carries the same key
    assert state.raw["error_source"] == "error_log"
    assert state.raw["work_mode"] == "MODE_PAUSE"
    assert "lifted" not in state.raw


def test_mammotion_pause_without_a_fresh_fault_stays_a_pause() -> None:
    old = _paused((-1201, _epoch(-3600)))
    state = _state(old, fault_since=SINCE)  # logged before the pause: history
    assert state.activity is MowerActivity.PAUSED and state.error_code is None
    assert not state.drive_recoverable
    undated = _paused((-1201, 4711))  # still on its uptime stamp
    assert _state(undated, fault_since=SINCE).activity is MowerActivity.PAUSED
    fresh = _paused((-1201, _epoch(-5)))
    assert _state(fresh).activity is MowerActivity.PAUSED  # no fault window: no fault


def test_mammotion_pause_on_the_charger_is_no_fault() -> None:
    device = _paused((-1201, _epoch(-5)))
    device.report_data.dev.charge_state = 1
    state = _state(device, fault_since=SINCE)
    assert state.activity is MowerActivity.PAUSED and state.error_code is None


def test_mammotion_paused_lift_stop_is_an_error_but_never_recoverable() -> None:
    state = _state(_paused((-2801, _epoch(-5))), fault_since=SINCE)
    assert state.activity is MowerActivity.ERROR and state.error_code == "2801"
    assert state.raw["lifted"] is True and state.raw["error_category"] == "lift_tilt"
    assert state.drive_recoverable is False


def test_mammotion_warning_event_names_the_fault_before_the_log() -> None:
    device = _paused((-1005, _epoch(-86400)))
    state = _state(device, fault_since=SINCE, warnings=((1201, NOW),))
    assert state.activity is MowerActivity.ERROR and state.error_code == "1201"
    assert state.raw["error_source"] == "warning_event"
    late = _state(device, fault_since=NOW + timedelta(seconds=1), warnings=((1201, NOW),))
    assert late.activity is MowerActivity.PAUSED  # a warning of an earlier window


def test_mammotion_error_mode_prefers_the_fresh_fault_and_drops_the_sign() -> None:
    device = _paused((-1201, _epoch(-5)))
    device.report_data.dev.sys_status = int(WorkMode.MODE_SYSTEM_ERROR)
    state = _state(device)
    assert state.error_code == "1201" and state.raw["error_source"] == "error_log"
    assert state.drive_recoverable is True
    device.buffer(error_log_buffer([(0, 0)]))
    state = _state(device)
    assert state.error_code == "mode_system_error"
    assert state.raw["error_category"] == "other" and state.raw["error_source"] == "work_mode"
    assert state.error_category == "other"
    assert state.drive_recoverable is False


def test_mammotion_self_check_is_reported_when_not_normal() -> None:
    device = sample_device()
    assert "self_check" not in _state(device).raw
    device.report_data.dev.self_check_status = 20
    assert _state(device).raw["self_check"] == "rain"
    device.report_data.dev.self_check_status = 77
    state = _state(device)
    assert state.raw["self_check"] == "other_77"
    assert state.self_check is not None and state.self_check.detail == "other_77"


# --- connection -------------------------------------------------------------------------------


async def _until(predicate: Any, timeout_s: float = 2.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def _mowing_port() -> FakeMammotionPort:
    device = sample_device()
    device.report_data.dev.sys_status = int(WorkMode.MODE_WORKING)
    device.report_data.dev.charge_state = 0
    return FakeMammotionPort(device=device)


def _error_log_reads(port: FakeMammotionPort) -> list[dict[str, Any]]:
    return [
        c.kwargs
        for c in port.sent
        if c.name == "read_write_device" and c.kwargs.get("context") == ERROR_LOG_ARGS["context"]
    ]


async def test_mammotion_trapped_pause_reads_the_error_log_and_reports_the_error() -> None:
    conn, port = cloud(_mowing_port())
    conn.error_log_delays_s = (0.0, 0.0, 0.0)
    port.error_log_answer = error_log_buffer([(-1201, _epoch(-10)), (-1005, _epoch(-86400))])
    await conn.connect()
    watch = conn.watch_state()
    assert (await anext(watch)).activity is MowerActivity.MOWING
    port.set_mode(WorkMode.MODE_PAUSE)
    await port.emit()
    state = await asyncio.wait_for(anext(watch), 1)
    while state.activity is not MowerActivity.ERROR:
        state = await asyncio.wait_for(anext(watch), 1)
    assert state.error_code == "1201" and state.drive_recoverable
    assert state.raw["error_category"] == "trapped"
    assert _error_log_reads(port) == [ERROR_LOG_ARGS]  # found: no second read
    # the owner may still resume or drive it free: a recoverable fault is no sensor stop
    assert (await conn.execute(ResumeMow())).status is not CommandStatus.FAILED
    port.set_mode(WorkMode.MODE_WORKING)
    await port.emit()
    await _until(lambda: conn._last_state is not None and conn._last_state.error_code is None)
    assert (await conn.get_state()).activity is MowerActivity.MOWING
    await conn.disconnect()


async def test_mammotion_pause_without_a_fault_reads_the_log_at_most_three_times() -> None:
    conn, port = cloud(_mowing_port())
    conn.error_log_delays_s = (0.0, 0.0, 0.0)
    port.error_log_answer = error_log_buffer([(-1201, _epoch(-86400))])  # history only
    await conn.connect()
    port.set_mode(WorkMode.MODE_PAUSE)
    await port.emit()
    await _until(lambda: len(_error_log_reads(port)) == 3)
    await asyncio.sleep(0.05)
    assert len(_error_log_reads(port)) == 3
    state = await conn.get_state()
    assert state.activity is MowerActivity.PAUSED and state.error_code is None
    await conn.disconnect()


async def test_mammotion_warning_event_reports_the_fault_at_once() -> None:
    conn, port = cloud(_mowing_port())
    conn.error_log_delays_s = (3600.0,)  # the log read would come much later
    await conn.connect()
    port.set_mode(WorkMode.MODE_PAUSE)
    await port.emit()
    assert (await conn.get_state()).activity is MowerActivity.PAUSED
    await port.deliver_notification("device_warning_event", {"code": 1201})
    state = await conn.get_state()
    assert state.activity is MowerActivity.ERROR and state.error_code == "1201"
    assert state.raw["error_source"] == "warning_event"
    await conn.disconnect()


async def test_mammotion_paused_lift_stop_is_a_sensor_stop() -> None:
    conn, port = cloud(_mowing_port())
    conn.error_log_delays_s = (0.0,)
    port.error_log_answer = error_log_buffer([(-2801, _epoch(-3))])
    await conn.connect()
    port.set_mode(WorkMode.MODE_PAUSE)
    await port.emit()
    await _until(lambda: conn._last_state is not None and conn._last_state.error_code == "2801")
    with pytest.raises(SensorStop):
        await conn.execute(PauseMow())
    await conn.disconnect()


async def test_mammotion_docked_pause_reads_no_error_log() -> None:
    conn, port = cloud()
    conn.error_log_delays_s = (0.0,)
    await conn.connect()
    port.set_mode(WorkMode.MODE_CHARGING_PAUSE)
    await port.emit()
    await asyncio.sleep(0.05)
    assert _error_log_reads(port) == []
    await conn.disconnect()


def test_every_adapter_error_category_is_a_contract_category() -> None:
    from mowdeck_contracts.v1 import MowerErrorCategory

    assert {str(c) for c in ErrorCategory} == {str(c) for c in MowerErrorCategory}
