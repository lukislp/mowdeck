"""The mower's own settings and its rain sensor on the Mammotion connections (ADR 0046)."""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from mammotion_support import ACCOUNT_ID, MOWER_ID, ble, cloud
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandStatus,
    MowerState,
    PrincipalKind,
    PrincipalRef,
    SetSetting,
    SettingValue,
    TimeWindow,
)
from mowdeck_control import CommandRejected, MowerController, NotConnected, settings_port_of
from mowdeck_control_mammotion import settings
from mowdeck_control_mammotion.base import MammotionBase
from mowdeck_control_mammotion.fake import FakeMammotionPort, is_setting_read, sample_device
from mowdeck_control_mammotion.port import MulSex, NoTransportAvailableError, Priority

LUBA_2_KEYS = [
    "rain_detection",
    "turn_style",
    "traversal_mode",
    "side_led",
    "night_light",
    "wildlife_safety",
    "voice_enabled",
    "voice_volume",
    "voice_gender",
    "non_work_hours",
]
CHARGE_KEYS = ["recharge_level", "smart_recharge_level", "resume_level", "smart_resume_level"]
CHARGE_FIRMWARE = "2.1.1.5"


async def _connected(conn: MammotionBase) -> MowerState:
    await conn.connect()
    await asyncio.sleep(0.05)  # the connect-time reads run in the background
    return await conn.get_state()


def _writes(port: FakeMammotionPort) -> list[tuple[str, dict[str, Any]]]:
    return [(c.name, c.kwargs) for c in port.sent if c.via == "send"]


def test_luba_2_declares_its_settings_and_the_charge_levels_with_new_firmware() -> None:
    name = "Luba-VPDRYRUN"
    assert [s.key for s in settings.settings_of(name, "1.30.29.26")] == LUBA_2_KEYS
    with_levels = [s.key for s in settings.settings_of(name, CHARGE_FIRMWARE)]
    assert with_levels == LUBA_2_KEYS[:6] + CHARGE_KEYS + LUBA_2_KEYS[6:]
    recharge = next(s for s in settings.settings_of(name, CHARGE_FIRMWARE) if s.min == 15)
    assert (recharge.key, recharge.max, recharge.unit) == ("recharge_level", 30, "%")


@pytest.mark.parametrize("name", ["Yuka-ABC123", "Luba-ABC123", "Spino-ABC", "RTK12345"])
def test_other_families_declare_no_settings(name: str) -> None:
    assert settings.settings_of(name, CHARGE_FIRMWARE) == []


async def test_settings_are_read_at_connect_and_reported() -> None:
    device = sample_device()
    device.mower_state.rain_detection = True
    device.mower_state.turning_mode = 1
    device.mower_state.side_led.enable = 1
    device.mower_state.audio.volume = 70
    device.mower_state.audio.sex = 1
    device.mower_state.animal_protection.status = 1
    device.mower_state.animal_protection.mode = 2
    device.non_work_hours.start_time = "1320"
    device.non_work_hours.end_time = "360"
    port = FakeMammotionPort(device)
    conn, _ = cloud(port)
    assert settings_port_of(conn) is conn
    state = await _connected(conn)
    caps = await conn.capabilities()
    assert [s.key for s in caps.settings] == LUBA_2_KEYS
    assert state.settings == {
        "non_work_hours": TimeWindow(start="22:00", end="06:00"),
        "night_light": False,
        "rain_detection": True,
        "side_led": False,
        "traversal_mode": "direct",
        "turn_style": "multi_point",
        "voice_enabled": True,
        "voice_gender": "woman",
        "voice_volume": 70,
        "wildlife_safety": "low_speed_mowing",
    }
    reads = [c for c in port.sent if is_setting_read(c.name, c.kwargs)]
    assert all(c.priority is Priority.BACKGROUND and c.via == "send_and_wait" for c in reads)
    assert len(reads) == len({(c.name, tuple(sorted(c.kwargs.items()))) for c in reads})
    await conn.disconnect()


async def test_unanswered_settings_stay_out_of_the_state_and_are_read_again() -> None:
    port = FakeMammotionPort()
    port.settings_answer = False
    conn, _ = ble(port)
    conn._device_info_retry_s = 0.01
    state = await _connected(conn)
    assert state.settings == {}
    await asyncio.sleep(0.1)
    audio_reads = [c for c in port.sent if c.name == "get_car_audio_cfg"]
    assert len(audio_reads) == 4  # every device information round, at most four
    port.settings_answer = True
    await conn.disconnect()


@pytest.mark.parametrize(
    ("key", "value", "writes", "expected"),
    [
        (
            "rain_detection",
            False,
            [("read_write_device", {"rw_id": 3, "context": 0, "rw": 1})],
            False,
        ),
        ("turn_style", "multi_point", [("turning_mode", {"context": 1})], "multi_point"),
        ("traversal_mode", "follow_perimeter", [("traverse_mode", {"context": 1})], None),
        (
            "side_led",
            True,
            [("read_and_set_sidelight", {"is_sidelight": True, "operate": 0})],
            True,
        ),
        ("night_light", True, [("set_car_light", {"on_off": True})], True),
        ("wildlife_safety", "stop_mowing", [("set_animal_protection_mode", {"mode": 1})], None),
        ("wildlife_safety", "off", [("set_animal_protection_mode", {"mode": 0})], None),
        ("voice_enabled", False, [("set_car_volume", {"volume": 0})], None),
        ("voice_enabled", True, [("set_car_volume", {"volume": 50})], None),
        ("voice_volume", 30, [("set_car_volume", {"volume": 30})], None),
        ("voice_gender", "woman", [("set_car_volume_sex", {"sex": MulSex.WOMAN})], None),
        (
            "non_work_hours",
            TimeWindow(start="22:30", end="06:00"),
            [("job_do_not_disturb", {"unable_start_time": "1350", "unable_end_time": "360"})],
            None,
        ),
        ("non_work_hours", None, [("job_do_not_disturb_del", {})], None),
    ],
)
async def test_set_setting_sends_the_vendor_command_once_and_reads_back(
    key: str,
    value: SettingValue,
    writes: list[tuple[str, dict[str, Any]]],
    expected: SettingValue,
) -> None:
    conn, port = cloud()
    await _connected(conn)
    port.sent.clear()
    outcome = await conn.set_setting(key, value)
    assert outcome.status is CommandStatus.EXECUTED, outcome.reason
    assert _writes(port) == writes
    assert all(c.priority is Priority.USER for c in port.sent)
    assert [c.via for c in port.sent][: len(writes)] == ["send"] * len(writes)
    assert any(is_setting_read(c.name, c.kwargs) for c in port.sent)  # the read back
    state = await conn.get_state()
    assert state.settings[key] == (value if expected is None else expected)
    await conn.disconnect()


async def test_charge_levels_and_their_smart_switch() -> None:
    device = sample_device()
    device.mower_state.recharge_level = 20
    device.mower_state.resume_level = -1
    port = FakeMammotionPort(device, firmware=CHARGE_FIRMWARE)
    conn, _ = cloud(port)
    state = await _connected(conn)
    assert state.settings["recharge_level"] == 20
    assert state.settings["smart_recharge_level"] is False
    assert state.settings["smart_resume_level"] is True
    assert "resume_level" not in state.settings  # smart: no percentage
    port.sent.clear()
    assert (await conn.set_setting("smart_recharge_level", True)).status is CommandStatus.EXECUTED
    assert _writes(port) == [("set_recharge_level", {"level": -1})]
    port.sent.clear()
    assert (await conn.set_setting("smart_resume_level", False)).status is CommandStatus.EXECUTED
    assert _writes(port) == [("set_resume_level", {"level": 100})]
    state = await conn.get_state()
    assert state.settings["resume_level"] == 100
    assert "recharge_level" not in state.settings
    await conn.disconnect()


async def test_set_setting_without_confirmation_is_accepted() -> None:
    conn, port = cloud()
    await _connected(conn)
    port.settings_follow_writes = False
    outcome = await conn.set_setting("rain_detection", True)
    assert outcome.status is CommandStatus.ACCEPTED
    port.settings_answer = False
    outcome = await conn.set_setting("night_light", True)
    assert outcome.status is CommandStatus.ACCEPTED
    assert outcome.reason == "night_light sent; the mower did not confirm it"
    await conn.disconnect()


async def test_set_setting_refuses_undeclared_keys_and_needs_a_link() -> None:
    conn, port = cloud()
    with pytest.raises(NotConnected):
        await conn.set_setting("rain_detection", True)
    await _connected(conn)
    with pytest.raises(CommandRejected, match="no setting 'recharge_level'"):
        await conn.set_setting("recharge_level", 20)  # firmware too old for the charge levels
    port.fail["read_write_device"] = NoTransportAvailableError("gone")
    with pytest.raises(NotConnected):
        await conn.set_setting("rain_detection", True)
    await conn.disconnect()


async def test_settings_work_during_an_error_and_never_start_motion() -> None:
    conn, port = cloud()
    await _connected(conn)
    port.device().report_data.dev.sys_status = 13  # MODE_LOCK: an error mode
    port.sent.clear()
    assert (await conn.set_setting("rain_detection", True)).status is CommandStatus.EXECUTED
    moving = {"start_job", "send_movement", "leave_dock", "resume_execute_task", "return_to_dock"}
    assert not moving & {c.name for c in port.sent}
    await conn.disconnect()


async def test_set_setting_through_the_controller_is_checked_first() -> None:
    conn, port = cloud()

    def envelope(command: SetSetting) -> CommandEnvelope:
        return CommandEnvelope.create(
            mower_id=MOWER_ID,
            account_id=ACCOUNT_ID,
            issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
            command=command,
            ttl=timedelta(seconds=30),
        )

    async with MowerController(conn, mower_id=MOWER_ID) as controller:
        await asyncio.sleep(0.05)
        port.sent.clear()
        bad = await controller.handle(envelope(SetSetting(key="voice_volume", value=101)))
        assert bad.status is CommandStatus.REJECTED
        assert port.sent == []
        good = await controller.handle(envelope(SetSetting(key="voice_volume", value=40)))
        assert good.status is CommandStatus.EXECUTED


async def test_rain_sensor_comes_from_the_self_check() -> None:
    conn, port = cloud()
    state = await _connected(conn)
    assert state.rain_detected is False
    port.device().report_data.dev.self_check_status = settings.RAIN_SELF_CHECK
    state = await conn.get_state()
    assert state.rain_detected is True
    assert state.self_check is not None and state.self_check.detail == "rain"
    port.report = False
    assert (await conn.get_state()).rain_detected is None
    await conn.disconnect()
