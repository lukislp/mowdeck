"""Device settings declared as capabilities (ADR 0046)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    SETTING_KEYS,
    CommandRequest,
    ConnectionKind,
    MowerActivity,
    MowerCapabilities,
    MowerState,
    SetSetting,
    SettingGroup,
    SettingSpec,
    SettingType,
    TimeWindow,
)
from pydantic import TypeAdapter, ValidationError

RECHARGE = SettingSpec(
    key="recharge_level", type=SettingType.INT, group=SettingGroup.BATTERY, min=15, max=30, step=1
)
VOLUME = SettingSpec(
    key="voice_volume", type=SettingType.INT, group=SettingGroup.VOICE, min=0, max=100, step=10
)
RAIN = SettingSpec(key="rain_detection", type=SettingType.BOOL, group=SettingGroup.WEATHER)
TURNS = SettingSpec(
    key="turn_style",
    type=SettingType.ENUM,
    group=SettingGroup.DRIVING,
    options=["in_place", "multi_point"],
)
QUIET = SettingSpec(key="non_work_hours", type=SettingType.TIME_WINDOW, group=SettingGroup.SCHEDULE)


def test_bool_setting_takes_only_booleans() -> None:
    assert RAIN.value_problem(True) is None
    assert RAIN.value_problem(1) == "rain_detection takes true or false"
    assert RAIN.value_problem("on") is not None


def test_int_setting_checks_range_and_step() -> None:
    assert RECHARGE.value_problem(15) is None
    assert RECHARGE.value_problem(30) is None
    assert RECHARGE.value_problem(31) == "recharge_level must lie within 15..30"
    assert RECHARGE.value_problem(True) == "recharge_level takes a whole number"
    assert VOLUME.value_problem(40) is None
    assert VOLUME.value_problem(45) == "voice_volume must be 0 plus a multiple of 10"


def test_enum_setting_takes_an_option() -> None:
    assert TURNS.value_problem("multi_point") is None
    assert TURNS.value_problem("arc") == "turn_style takes one of in_place, multi_point"
    assert TURNS.value_problem(None) is not None


def test_time_window_setting_takes_a_window_or_null() -> None:
    assert QUIET.value_problem(TimeWindow(start="22:00", end="06:00")) is None
    assert QUIET.value_problem(None) is None
    assert QUIET.value_problem("22:00-06:00") is not None


def test_time_window_rules() -> None:
    with pytest.raises(ValidationError):
        TimeWindow(start="24:00", end="06:00")
    with pytest.raises(ValidationError, match="must differ"):
        TimeWindow(start="06:00", end="06:00")
    assert TimeWindow.minutes("22:30") == 1350
    with pytest.raises(ValueError, match="time of day"):
        TimeWindow.minutes("7")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"key": "unknown_key", "type": SettingType.BOOL},
        {"key": "recharge_level", "type": SettingType.INT, "min": 30, "max": 15},
        {"key": "recharge_level", "type": SettingType.INT},
        {"key": "rain_detection", "type": SettingType.BOOL, "min": 1},
        {"key": "turn_style", "type": SettingType.ENUM},
        {"key": "turn_style", "type": SettingType.ENUM, "options": ["a", "a"]},
        {"key": "rain_detection", "type": SettingType.BOOL, "options": ["a"]},
        {"key": "Rain", "type": SettingType.BOOL},
    ],
)
def test_spec_shape_is_checked(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SettingSpec(group=SettingGroup.WEATHER, **kwargs)  # type: ignore[arg-type]


def test_capabilities_declare_each_key_once_and_find_it() -> None:
    caps = MowerCapabilities(settings=[RAIN, RECHARGE])
    assert caps.setting("rain_detection") == RAIN
    assert caps.setting("voice_volume") is None
    with pytest.raises(ValidationError, match="twice"):
        MowerCapabilities(settings=[RAIN, RAIN])
    assert MowerCapabilities().settings == []


def test_set_setting_keeps_the_value_type() -> None:
    adapter = TypeAdapter(CommandRequest)
    for value, expected in [
        (True, True),
        (25, 25),
        ("multi_point", "multi_point"),
        (None, None),
        ({"start": "22:00", "end": "06:00"}, TimeWindow(start="22:00", end="06:00")),
    ]:
        request = adapter.validate_python(
            {"command": {"kind": "set_setting", "key": "x", "value": value}}
        )
        assert isinstance(request.command, SetSetting)
        assert request.command.value == expected
        assert type(request.command.value) is type(expected)
    dumped = SetSetting(key="non_work_hours", value=TimeWindow(start="22:00", end="06:00"))
    assert dumped.model_dump(mode="json") == {
        "kind": "set_setting",
        "key": "non_work_hours",
        "value": {"start": "22:00", "end": "06:00"},
    }
    json_request = adapter.validate_json(
        '{"command": {"kind": "set_setting", "key": "a", "value": 1}}'
    )
    assert isinstance(json_request.command, SetSetting)
    assert json_request.command.value == 1 and type(json_request.command.value) is int


def test_state_carries_settings_and_rain() -> None:
    state = MowerState(
        mower_id=uuid4(),
        account_id=uuid4(),
        observed_at=datetime(2026, 10, 23, tzinfo=UTC),
        activity=MowerActivity.DOCKED,
        battery_percent=80,
        connection=ConnectionKind.SIMULATED,
        settings={"rain_detection": True, "non_work_hours": None},
        rain_detected=False,
    )
    again = MowerState.model_validate_json(state.model_dump_json())
    assert again.settings == {"rain_detection": True, "non_work_hours": None}
    assert again.rain_detected is False
    assert MowerState.model_validate(state.model_dump(exclude={"settings", "rain_detected"})) == (
        state.model_copy(update={"settings": {}, "rain_detected": None})
    )


def test_every_known_key_is_documented() -> None:
    assert all(text.endswith(".") or text.endswith(")") for text in SETTING_KEYS.values())
