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


@pytest.mark.parametrize("name", ["Luba-ABC123", "Spino-ABC", "Spino-S1ABC", "RTK12345"])
def test_other_families_declare_no_settings(name: str) -> None:
    assert settings.settings_of(name, CHARGE_FIRMWARE) == []


YUKA_BASE = ["rain_detection", "turn_style", "traversal_mode"]
YUKA_VOICE = ["voice_enabled", "voice_volume", "voice_gender", "non_work_hours"]


@pytest.mark.parametrize(
    ("name", "blade_speed", "night_light", "wildlife"),
    [
        ("Yuka-ABC123", False, False, True),  # the original Yuka (LUBA_YUKA)
        ("Yuka-MN12345", True, True, True),  # YUKA mini
        ("Yuka-YM12345", True, True, True),  # YUKA mini 2
        ("Yuka-VP12345", True, False, True),  # YUKA VP (MN241)
        ("Yuka-MV12345", True, False, False),  # YUKA MV: no night light, no wildlife safety
        ("Yuka-ML12345", True, True, True),
        ("Ezy-VT12345", True, True, False),  # MN100: no wildlife safety
        ("Ezy-LD12345", True, True, True),  # MN101
        ("Yuka-HS12345", True, True, True),  # MN260
    ],
)
def test_yuka_models_declare_what_pymammotion_supports_for_them(
    name: str, blade_speed: bool, night_light: bool, wildlife: bool
) -> None:
    expected = (
        YUKA_BASE
        + (["blade_speed"] if blade_speed else [])
        + ["side_led"]
        + (["night_light"] if night_light else [])
        + (["wildlife_safety"] if wildlife else [])
        + CHARGE_KEYS
        + YUKA_VOICE
    )
    assert [s.key for s in settings.settings_of(name, CHARGE_FIRMWARE)] == expected
    old = [s.key for s in settings.settings_of(name, "1.12.9")]  # before 1.13 and 2.1.1.5
    assert old == [k for k in expected if k not in CHARGE_KEYS and k != "wildlife_safety"]


def test_blade_speed_is_a_mowing_enum_of_the_yuka_only() -> None:
    spec = next(s for s in settings.settings_of("Yuka-MN12345", "") if s.key == "blade_speed")
    assert (spec.group, spec.options) == ("mowing", ["slow", "normal", "fast"])
    # PyMammotion also offers it for these LUBA models; ADR 0046 leaves it out for them.
    for luba in ("Luba-VPDRYRUN", "Luba-MN12345", "Luba-VA12345"):
        assert "blade_speed" not in [s.key for s in settings.settings_of(luba, "")]


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


def _yuka_port(name: str = "Yuka-MN12345", **mower_state: Any) -> FakeMammotionPort:
    device = sample_device(name, product_key="")
    for field, value in mower_state.items():
        setattr(device.mower_state, field, value)
    return FakeMammotionPort(device, device_name=name, product_key="", firmware=CHARGE_FIRMWARE)


async def test_a_yuka_reads_its_settings_with_the_replies_pymammotion_routes() -> None:
    port = _yuka_port(
        rain_detection=True, traversal_mode=1, cutter_mode=2, recharge_level=25, resume_level=90
    )
    conn, _ = cloud(port)
    state = await _connected(conn)
    caps = await conn.capabilities()
    assert "blade_speed" in [s.key for s in caps.settings]
    assert state.settings["blade_speed"] == "fast"
    assert state.settings["rain_detection"] is True
    assert state.settings["traversal_mode"] == "follow_perimeter"
    assert state.settings["night_light"] is False
    assert (state.settings["recharge_level"], state.settings["resume_level"]) == (25, 90)
    replies = {(c.name, c.expected_field) for c in port.sent if is_setting_read(c.name, c.kwargs)}
    assert ("get_cutter_mode", "current_cutter_mode") in replies
    assert ("read_write_device", "nav_sys_param_cmd") in replies  # like the LUBA 2
    assert ("read_recharge_level", "nav_sys_param_cmd") in replies
    await conn.disconnect()


@pytest.mark.parametrize(("value", "mode"), [("slow", 1), ("normal", 0), ("fast", 2)])
async def test_a_yuka_sets_its_blade_speed(value: str, mode: int) -> None:
    conn, port = cloud(_yuka_port())
    await _connected(conn)
    port.sent.clear()
    outcome = await conn.set_setting("blade_speed", value)
    assert outcome.status is CommandStatus.EXECUTED, outcome.reason
    assert _writes(port) == [("set_cutter_mode", {"cutter_mode": mode})]
    assert ("get_cutter_mode", "current_cutter_mode") in [
        (c.name, c.expected_field) for c in port.sent
    ]
    assert (await conn.get_state()).settings["blade_speed"] == value
    await conn.disconnect()


async def test_an_unknown_cutter_mode_stays_out_of_the_state() -> None:
    conn, _ = cloud(_yuka_port(cutter_mode=7))
    state = await _connected(conn)
    assert "blade_speed" not in state.settings
    await conn.disconnect()


async def test_a_yuka_mv_has_no_night_light_and_the_luba_2_no_blade_speed() -> None:
    conn, _ = cloud(_yuka_port("Yuka-MV12345"))
    await _connected(conn)
    with pytest.raises(CommandRejected, match="no setting 'night_light'"):
        await conn.set_setting("night_light", True)
    await conn.disconnect()
    luba, _ = cloud()
    await _connected(luba)
    with pytest.raises(CommandRejected, match="no setting 'blade_speed'"):
        await luba.set_setting("blade_speed", "fast")
    await luba.disconnect()
