"""The mower's own settings on Mammotion mowers (ADR 0046).

Which settings a mower declares (:func:`settings_of`), how each is read and written, and how
PyMammotion's device state turns into ``MowerState.settings`` (:func:`values_of`). Every
command, argument and reply field is the one Mammotion-HA uses (``coordinator.py``,
``switch.py``, ``number.py``, ``select.py``) on PyMammotion 0.10.9:

==========================  =================================================================
``rain_detection``          ``read_write_device(rw_id=3, context=on, rw=1)``; read with
                            ``rw_id=3, context=1, rw=0``, answer ``nav_sys_param_cmd``
                            (``mower_state.rain_detection``)
``turn_style``              ``turning_mode(context)`` (0 zero-turn = ``in_place``, 1 multipoint
                            = ``multi_point``); read ``rw_id=6`` (``mower_state.turning_mode``)
``traversal_mode``          ``traverse_mode(context)`` (0 ``direct``, 1 ``follow_perimeter``);
                            read ``rw_id=7`` (``mower_state.traversal_mode``)
``side_led``                ``read_and_set_sidelight(is_sidelight=on, operate=0)``; read with
                            ``operate=1``, answer ``todev_time_ctrl_light``; on while
                            ``mower_state.side_led.enable`` is 0 (Mammotion-HA's reading)
``night_light``             ``set_car_light(on_off=on)``; read ``get_car_light(ids=1123)``,
                            answer ``get_lamp_rsp`` (``lamp_info.night_light``); models with
                            the fill light only
``wildlife_safety``         ``set_animal_protection_mode(mode)`` (0 ``off``, 1
                            ``stop_mowing``, 2 ``low_speed_mowing``); read
                            ``read_animal_protection_mode`` and ``..._status``, answer
                            ``nav_sys_param_cmd``; ``off`` while the status is 0; gated by
                            ``DeviceType.supports_wildlife_safety`` (model and firmware)
``recharge_level``,         ``set_recharge_level(level)`` (15 to 30 %, or -1 for smart) and
``smart_recharge_level``    ``read_recharge_level``, answer ``nav_sys_param_cmd``
                            (``mower_state.recharge_level``, 0 until read); leaving smart writes
                            the top of the range, as the app does
``resume_level``,           the same with ``set_resume_level`` (40 to 100 %) and
``smart_resume_level``      ``read_resume_level``; both pairs gated by
                            ``DeviceType.supports_charge_limit`` (firmware 2.1.1.5 or newer)
``voice_volume``,           ``set_car_volume(volume)`` (0 to 100; ``voice_enabled`` writes 50 or
``voice_enabled``           0, like Mammotion-HA) and ``set_car_volume_sex(sex)`` (``MAN``,
``voice_gender``            ``WOMAN``); read ``get_car_audio_cfg``, answer ``audio_cfg``
                            (``mower_state.audio``)
``non_work_hours``          ``job_do_not_disturb(unable_start_time, unable_end_time)`` with
                            minutes after midnight as strings, ``job_do_not_disturb_del`` for
                            none; read ``read_job_do_not_disturb``, answer
                            ``todev_unable_time_set`` (``device.non_work_hours``)
==========================  =================================================================

Only the LUBA 2 family declares settings (``DeviceType.is_luba_pro`` and not a Yuka): the
reply routing above (``nav_sys_param_cmd`` for the read/write parameters) is the one of these
models, and the owner's LUBA 2 AWD X is the one MowDeck is tested against. Left out on purpose:
the rain delay and rain protection modes (X5 series only), the charge limit and smart charging
(a read-modify-write of the off-peak charging window that resets it when sent unread),
``auto_change_direction`` (a start option of other models, excluded for the LUBA 2), and every
Yuka and Spino setting. None of these settings moves the mower.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from mowdeck_contracts.v1 import (
    SettingGroup,
    SettingSpec,
    SettingType,
    SettingValue,
    TimeWindow,
)

from mowdeck_control_mammotion.port import (
    RECHARGE_LEVEL_RANGE,
    RESUME_LEVEL_RANGE,
    SMART_CHARGE_LEVEL,
    DeviceType,
    MowerDevice,
    MulSex,
)

RW_REPLY: Final = "nav_sys_param_cmd"
"""Answer of the read/write parameters and the battery levels on the LUBA 2 family."""
SIDELIGHT_REPLY: Final = "todev_time_ctrl_light"
LAMP_REPLY: Final = "get_lamp_rsp"
AUDIO_REPLY: Final = "audio_cfg"
NON_WORK_REPLY: Final = "todev_unable_time_set"
NIGHT_LIGHT_IDS: Final = 1123
VOICE_ON_VOLUME: Final = 50
"""The volume ``voice_enabled`` switches to, as Mammotion-HA does."""

TURN_OPTIONS: Final = ("in_place", "multi_point")
TRAVERSAL_OPTIONS: Final = ("direct", "follow_perimeter")
WILDLIFE_OPTIONS: Final = ("off", "stop_mowing", "low_speed_mowing")
VOICE_GENDERS: Final = ("man", "woman")

Request = tuple[str, str, dict[str, Any]]
"""A read: ``(command, reply field, arguments)`` for :meth:`MammotionPort.send_and_wait`."""
Write = tuple[str, dict[str, Any]]
"""A write: ``(command, arguments)`` sent once with :meth:`MammotionPort.send`."""


def _rw_read(rw_id: int) -> Request:
    return ("read_write_device", RW_REPLY, {"rw_id": rw_id, "context": 1, "rw": 0})


class _Unknown:
    """Marker: the mower has not reported a value (the key stays out of the state)."""


@dataclass(frozen=True, slots=True)
class _Setting:
    spec: SettingSpec
    read: Sequence[Request]
    write: Callable[[SettingValue, MowerDevice], Sequence[Write]]
    value: Callable[[MowerDevice], SettingValue | type[_Unknown]]
    shares: str = ""
    """Settings answered by the same read share a name here (one read serves all of them)."""
    supported: Callable[[str, str], bool] = field(default=lambda _name, _firmware: True)


def _bool(value: SettingValue) -> bool:
    return bool(value)


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[call-overload,no-any-return]
    except (TypeError, ValueError):
        return 0


def _option(options: Sequence[str], index: object) -> SettingValue | type[_Unknown]:
    number = _int(index)
    return options[number] if 0 <= number < len(options) else _Unknown


def _level(level: int, levels: range) -> SettingValue | type[_Unknown]:
    if level in levels:
        return level
    return _Unknown


def _window(device: MowerDevice) -> SettingValue | type[_Unknown]:
    hours = device.non_work_hours
    start, end = str(hours.start_time or ""), str(hours.end_time or "")
    if not start.isdigit() or not end.isdigit() or start == end:
        return None
    clock = [f"{int(m) // 60 % 24:02d}:{int(m) % 60:02d}" for m in (start, end)]
    if clock[0] == clock[1]:
        return None
    return TimeWindow(start=clock[0], end=clock[1])


def _window_write(value: SettingValue, _device: MowerDevice) -> Sequence[Write]:
    if value is None:
        return [("job_do_not_disturb_del", {})]
    assert isinstance(value, TimeWindow)  # noqa: S101 - checked by the controller
    return [
        (
            "job_do_not_disturb",
            {
                "unable_start_time": str(TimeWindow.minutes(value.start)),
                "unable_end_time": str(TimeWindow.minutes(value.end)),
            },
        )
    ]


def _wildlife(device: MowerDevice) -> SettingValue | type[_Unknown]:
    protection = device.mower_state.animal_protection
    if _int(protection.status) == 0:
        return "off"
    return _option(WILDLIFE_OPTIONS, protection.mode)


def _smart(level: int) -> SettingValue | type[_Unknown]:
    if level == 0:
        return _Unknown
    return level == SMART_CHARGE_LEVEL


def _charge_levels(name: str, firmware: str) -> bool:
    return DeviceType.supports_charge_limit(name, firmware)


def _int_spec(key: str, group: SettingGroup, levels: range) -> SettingSpec:
    return SettingSpec(
        key=key, type=SettingType.INT, group=group, min=levels[0], max=levels[-1], step=1, unit="%"
    )


def _enum_spec(key: str, group: SettingGroup, options: Sequence[str]) -> SettingSpec:
    return SettingSpec(key=key, type=SettingType.ENUM, group=group, options=list(options))


def _bool_spec(key: str, group: SettingGroup) -> SettingSpec:
    return SettingSpec(key=key, type=SettingType.BOOL, group=group)


_READ_AUDIO: Final[Request] = ("get_car_audio_cfg", AUDIO_REPLY, {})
_READ_RECHARGE: Final[Request] = ("read_recharge_level", RW_REPLY, {})
_READ_RESUME: Final[Request] = ("read_resume_level", RW_REPLY, {})

SETTINGS: Final[tuple[_Setting, ...]] = (
    _Setting(
        spec=_bool_spec("rain_detection", SettingGroup.WEATHER),
        read=[_rw_read(3)],
        write=lambda v, _d: [
            ("read_write_device", {"rw_id": 3, "context": int(_bool(v)), "rw": 1})
        ],
        value=lambda d: bool(d.mower_state.rain_detection),
    ),
    _Setting(
        spec=_enum_spec("turn_style", SettingGroup.DRIVING, TURN_OPTIONS),
        read=[_rw_read(6)],
        write=lambda v, _d: [("turning_mode", {"context": TURN_OPTIONS.index(str(v))})],
        value=lambda d: _option(TURN_OPTIONS, d.mower_state.turning_mode),
    ),
    _Setting(
        spec=_enum_spec("traversal_mode", SettingGroup.DRIVING, TRAVERSAL_OPTIONS),
        read=[_rw_read(7)],
        write=lambda v, _d: [("traverse_mode", {"context": TRAVERSAL_OPTIONS.index(str(v))})],
        value=lambda d: _option(TRAVERSAL_OPTIONS, d.mower_state.traversal_mode),
    ),
    _Setting(
        spec=_bool_spec("side_led", SettingGroup.LIGHTS),
        read=[("read_and_set_sidelight", SIDELIGHT_REPLY, {"is_sidelight": False, "operate": 1})],
        write=lambda v, _d: [("read_and_set_sidelight", {"is_sidelight": _bool(v), "operate": 0})],
        value=lambda d: _int(d.mower_state.side_led.enable) == 0,
    ),
    _Setting(
        spec=_bool_spec("night_light", SettingGroup.LIGHTS),
        read=[("get_car_light", LAMP_REPLY, {"ids": NIGHT_LIGHT_IDS})],
        write=lambda v, _d: [("set_car_light", {"on_off": _bool(v)})],
        value=lambda d: bool(d.mower_state.lamp_info.night_light),
        supported=lambda name, _fw: DeviceType.is_support_fill_light(name),
    ),
    _Setting(
        spec=_enum_spec("wildlife_safety", SettingGroup.SAFETY, WILDLIFE_OPTIONS),
        read=[
            ("read_animal_protection_mode", RW_REPLY, {}),
            ("read_animal_protection_status", RW_REPLY, {}),
        ],
        write=lambda v, _d: [
            ("set_animal_protection_mode", {"mode": WILDLIFE_OPTIONS.index(str(v))})
        ],
        value=_wildlife,
        supported=lambda name, fw: DeviceType.supports_wildlife_safety(name, fw),
    ),
    _Setting(
        spec=_int_spec("recharge_level", SettingGroup.BATTERY, RECHARGE_LEVEL_RANGE),
        read=[_READ_RECHARGE],
        write=lambda v, _d: [("set_recharge_level", {"level": _int(v)})],
        value=lambda d: _level(_int(d.mower_state.recharge_level), RECHARGE_LEVEL_RANGE),
        shares="recharge",
        supported=_charge_levels,
    ),
    _Setting(
        spec=_bool_spec("smart_recharge_level", SettingGroup.BATTERY),
        read=[_READ_RECHARGE],
        write=lambda v, _d: [
            (
                "set_recharge_level",
                {"level": SMART_CHARGE_LEVEL if v else RECHARGE_LEVEL_RANGE[-1]},
            )
        ],
        value=lambda d: _smart(_int(d.mower_state.recharge_level)),
        shares="recharge",
        supported=_charge_levels,
    ),
    _Setting(
        spec=_int_spec("resume_level", SettingGroup.BATTERY, RESUME_LEVEL_RANGE),
        read=[_READ_RESUME],
        write=lambda v, _d: [("set_resume_level", {"level": _int(v)})],
        value=lambda d: _level(_int(d.mower_state.resume_level), RESUME_LEVEL_RANGE),
        shares="resume",
        supported=_charge_levels,
    ),
    _Setting(
        spec=_bool_spec("smart_resume_level", SettingGroup.BATTERY),
        read=[_READ_RESUME],
        write=lambda v, _d: [
            ("set_resume_level", {"level": SMART_CHARGE_LEVEL if v else RESUME_LEVEL_RANGE[-1]})
        ],
        value=lambda d: _smart(_int(d.mower_state.resume_level)),
        shares="resume",
        supported=_charge_levels,
    ),
    _Setting(
        spec=_bool_spec("voice_enabled", SettingGroup.VOICE),
        read=[_READ_AUDIO],
        write=lambda v, _d: [("set_car_volume", {"volume": VOICE_ON_VOLUME if v else 0})],
        value=lambda d: _int(d.mower_state.audio.volume) > 0,
        shares="audio",
    ),
    _Setting(
        spec=SettingSpec(
            key="voice_volume",
            type=SettingType.INT,
            group=SettingGroup.VOICE,
            min=0,
            max=100,
            step=1,
            unit="%",
        ),
        read=[_READ_AUDIO],
        write=lambda v, _d: [("set_car_volume", {"volume": _int(v)})],
        value=lambda d: max(0, min(100, _int(d.mower_state.audio.volume))),
        shares="audio",
    ),
    _Setting(
        spec=_enum_spec("voice_gender", SettingGroup.VOICE, VOICE_GENDERS),
        read=[_READ_AUDIO],
        write=lambda v, _d: [("set_car_volume_sex", {"sex": MulSex[str(v).upper()]})],
        value=lambda d: _option(VOICE_GENDERS, d.mower_state.audio.sex),
        shares="audio",
    ),
    _Setting(
        spec=SettingSpec(
            key="non_work_hours", type=SettingType.TIME_WINDOW, group=SettingGroup.SCHEDULE
        ),
        read=[("read_job_do_not_disturb", NON_WORK_REPLY, {})],
        write=_window_write,
        value=_window,
    ),
)
"""Every setting the adapter knows, in the order a client offers them."""

BY_KEY: Final[Mapping[str, _Setting]] = {s.spec.key: s for s in SETTINGS}


def family_has_settings(device_name: str, product_key: str = "") -> bool:
    """True for the LUBA 2 family (see the module docstring)."""
    return DeviceType.is_luba_pro(device_name, product_key) and not DeviceType.is_yuka(device_name)


def settings_of(device_name: str, firmware: str, product_key: str = "") -> list[SettingSpec]:
    """The settings a mower declares (``MowerCapabilities.settings``)."""
    if not family_has_settings(device_name, product_key):
        return []
    return [s.spec for s in SETTINGS if s.supported(device_name, firmware)]


def reads_for(keys: Sequence[str]) -> list[Request]:
    """The reads that answer ``keys``, each once, in the order of :data:`SETTINGS`."""
    reads: list[Request] = []
    for key in keys:
        setting = BY_KEY.get(key)
        if setting is None:
            continue
        for request in setting.read:
            if request not in reads:
                reads.append(request)
    return reads


def answered_keys(request: Request, declared: Sequence[str]) -> set[str]:
    """The declared keys a successful ``request`` reports."""
    return {k for k in declared if k in BY_KEY and request in BY_KEY[k].read}


def writes_for(key: str, value: SettingValue, device: MowerDevice) -> Sequence[Write]:
    """The commands that change ``key`` to ``value`` (already checked by the controller)."""
    return BY_KEY[key].write(value, device)


def values_of(device: MowerDevice, reported: set[str]) -> dict[str, SettingValue]:
    """``MowerState.settings``: the values of the ``reported`` keys (the adapter read them),
    without the ones whose answer is not a value (a level of 0 before the first answer, a
    smart level for the percentage key)."""
    values: dict[str, SettingValue] = {}
    for key in sorted(reported):
        setting = BY_KEY.get(key)
        if setting is None:
            continue
        value = setting.value(device)
        if value is not _Unknown:
            values[key] = value  # type: ignore[assignment]
    return values


RAIN_SELF_CHECK: Final = 20
"""``report_data.dev.self_check_status`` 20 ("Mowing is disabled on rainy days"): the rain
sensor stops the mower from starting. Mammotion-HA's ``rain_detected`` binary sensor
(``binary_sensor.py``) reads exactly this, for every mower."""


def rain_detected_of(device: MowerDevice, *, has_report: bool = True) -> bool | None:
    """``MowerState.rain_detected`` for the LUBA 2 family: True while the self-check reports
    rain (:data:`RAIN_SELF_CHECK`); None without a report. The mower reports it only while the
    setting ``rain_detection`` is on (the sensor then blocks a start), so with the setting off
    rain reads as False."""
    if not has_report:
        return None
    return _int(device.report_data.dev.self_check_status) == RAIN_SELF_CHECK


__all__ = [
    "BY_KEY",
    "RAIN_SELF_CHECK",
    "SETTINGS",
    "answered_keys",
    "family_has_settings",
    "rain_detected_of",
    "reads_for",
    "settings_of",
    "values_of",
    "writes_for",
]
