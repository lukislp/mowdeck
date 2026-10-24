# ADR 0046: Device settings as capabilities

## Status

Accepted (2026-10-23); amends [ADR 0043](0043-turn-styles.md) (the mower's own turn setting may now be written). Amended 2026-10-25 (Yuka family, see below).

## Context

A mower keeps settings of its own that change how it behaves, not what it does right now: whether its rain sensor stops it, the battery levels at which it returns and resumes, its lights, its voice, the hours it must not work. The vendor app and the community Home Assistant integration for Mammotion expose them; MowDeck exposed none, so an owner still needed the vendor app, and MowDeck's own Home Assistant integration (mowdeck-hacs) had nothing to offer for them.

Which settings exist differs per manufacturer, model and firmware: the LUBA 2 has rain detection but not the rain delay of the X5 series; the battery levels need firmware 2.1.1.5; wildlife safety depends on model and firmware; only some models have the night light of the camera's fill light. A typed command per setting (like `set_light` or `set_drive_speed`) would add a contract change, a permission entry, a simulator path and a web control for every key, and still not tell a client which of them the connected mower has.

The mower also reports when its rain sensor stops it from starting; clients want that as a plain "it is raining" signal (a binary sensor in Home Assistant), not as one of many self-check codes.

## Decision

**Settings are declared, not hard-coded.** `MowerCapabilities.settings` lists what the connection can read and change, one `SettingSpec` per setting: a manufacturer-neutral `key`, a `type` (`bool`, `int`, `enum`, `time_window`), a `group` for clients (`weather`, `battery`, `lights`, `driving`, `mowing`, `safety`, `voice`, `schedule`; `mowing` since 2026-10-25), and for `int` its `min`, `max`, `step` and `unit`, for `enum` its `options`. The known keys and what they mean are `SETTING_KEYS` in the contracts; an adapter may only declare those, so clients can label every setting without knowing the vendor. A `time_window` value is `{start, end}` (`HH:MM`, `end` before `start` crosses midnight) or null for "none".

**Current values in the state.** `MowerState.settings` maps each declared key to its value as the mower reported it. A key the mower has not answered yet is missing (never a guessed default), so a client shows "unknown" instead of a wrong value.

**One generic command.** `SetSetting {kind: "set_setting", key, value}`. The controller (rule 14) refuses an undeclared key (after reloading the capabilities once: the declaration may depend on the model or firmware the mower reported after connecting) and a value that does not fit the declaration (`SettingSpec.value_problem`: type, range, step, options) before anything reaches the adapter. The adapter implements the optional `SettingsCapable` protocol (`set_setting(key, value) -> Outcome`): `executed` when the mower's answer shows the new value, `accepted` when it was sent but not confirmed.

**Safety: a setting never moves the mower.** An adapter declares no key whose change starts the wheels or the blades. A setting therefore keeps an open drive session and pending confirmations and is allowed in every activity, also during an error (like the light); it is refused, like every command, while a firmware update, a spot run or a drive calibration runs.

**Permission and plan.** `set_setting` needs `can_manage_mowers` on the mower (it changes the device itself, like `firmware_update`) and the plan feature `control`. The Fleet command record is its audit. It may be sent to a site or group (each mower checks its own declaration).

**Rain sensor.** `MowerState.rain_detected` (`bool`, null when the connection does not report the sensor) says that the mower's own rain sensor reports rain. The existing `self_check` keeps the detail.

**Considered and rejected.** A typed command per setting (see Context). A separate settings endpoint with its own read model: the state already flows to every client over the realtime socket and the telemetry API, and the capabilities endpoint already serves the declaration. Free-form vendor keys passed through: clients could not label them, and the controller could not check them.

## Manufacturer independence

Generic: the contracts (`SettingSpec`, `SettingType`, `SettingGroup`, `TimeWindow`, `SETTING_KEYS`, `SetSetting`, `MowerCapabilities.settings`, `MowerState.settings`, `MowerState.rain_detected`), the controller's rule 14, the `SettingsCapable` protocol, the Fleet permission and plan rule, and the simulator (`SimulationConfig.settings`, one setting of each type; `raining` and `set_raining` for the sensor, which with `rain_detection` on blocks the self-check with `rain` and refuses a start).

Mammotion (`mowdeck_control_mammotion.settings`, the commands and replies Mammotion-HA uses on PyMammotion 0.10.9): only the LUBA 2 family declares settings (`DeviceType.is_luba_pro`, not a Yuka), because the reply routing of the read/write parameters is that family's and the owner's LUBA 2 AWD X is what MowDeck is tested against.

| Key | Vendor command | Gate |
| --- | --- | --- |
| `rain_detection` | `read_write_device` parameter 3 | LUBA 2 family |
| `turn_style` (`in_place`, `multi_point`) | `turning_mode`, parameter 6 | LUBA 2 family |
| `traversal_mode` (`direct`, `follow_perimeter`) | `traverse_mode`, parameter 7 | LUBA 2 family |
| `side_led` | `read_and_set_sidelight` | LUBA 2 family |
| `night_light` | `set_car_light`, read `get_car_light(1123)` | `is_support_fill_light` |
| `wildlife_safety` (`off`, `stop_mowing`, `low_speed_mowing`) | `set_animal_protection_mode` | `supports_wildlife_safety` (model, firmware) |
| `recharge_level` (15 to 30 %), `smart_recharge_level` | `set_recharge_level` (-1 is smart) | `supports_charge_limit` (firmware 2.1.1.5) |
| `resume_level` (40 to 100 %), `smart_resume_level` | `set_resume_level` | `supports_charge_limit` |
| `voice_enabled`, `voice_volume`, `voice_gender` (`man`, `woman`) | `set_car_volume`, `set_car_volume_sex` | LUBA 2 family |
| `non_work_hours` | `job_do_not_disturb`, `job_do_not_disturb_del` | LUBA 2 family |

Writes go out once with user priority, then the setting is read back; the declared settings are read at connect with the device information (background priority, again while unanswered). `rain_detected` is `report_data.dev.self_check_status == 20` ("Mowing is disabled on rainy days"), which the mower reports only while `rain_detection` is on.

Left out on purpose: the rain delay and the rain protection modes (X5 series only), the charge limit and smart charging (a read-modify-write of the off-peak charging window, which the vendor resets to 00:00 to 00:00 when it is sent unread; and not available on the owner's firmware), `auto_change_direction` (a start option that the vendor excludes for the LUBA 2), blade speed, and every Yuka and Spino setting.

## Amendment 2026-10-25: Yuka family

Owner decision: the Yuka models get every setting PyMammotion 0.10.9 supports for them, with PyMammotion's own gates per model and firmware ("what PyMammotion offers, the Yuka can do; its author tested it, so accept his assumptions"). The reason for leaving them out above no longer holds: PyMammotion routes the read/write parameters of every `DeviceType.is_luba_pro` model over `nav_sys_param_cmd` (`MammotionCommand.read_write_device`, the same check as Mammotion-HA's `_rw_expected_field`), and that includes every Yuka. Mammotion-HA (at PyMammotion 0.10.9) creates the same entities for the Yuka.

- The Mammotion adapter now declares settings for every `is_luba_pro` model (`DeviceType.is_yuka` included): `rain_detection`, `turn_style`, `traversal_mode`, `side_led`, `voice_*` and `non_work_hours` on every Yuka; `night_light` where `is_support_fill_light` holds and the model is not the Yuka MV (the app hides it there; this gate was missing above); `wildlife_safety` by `supports_wildlife_safety` (not the Yuka MV and Ezy VT, firmware 1.13); the charge levels by `supports_charge_limit` (firmware 2.1.1.5).
- New key `blade_speed` (`enum`: `slow`, `normal`, `fast`; group `mowing`, new): `set_cutter_mode` (PyMammotion's `CuttingSpeedMode` 1, 0, 2), read `get_cutter_mode`, answer `current_cutter_mode`. Declared for the Yuka models of `DeviceType.is_support_blade_speed` (every Yuka but the original one). PyMammotion offers it for several LUBA models as well (the LUBA 2 AWD X among them); for them it stays left out as decided above until the owner decides otherwise.
- Still left out: the Yuka's grass collector. The manual sweep and dump (`manual_grass_collection`, `manual_pour_grass`) move the collector, which a setting must not do; the dump interval (`collect_grass_frequency`) and the mow, dump and edge switches are job options of a start (Mammotion-HA's `operation_settings`), which belong to `start_options` (ADR 0030), not here. Spino: pool cleaners are no `is_luba_pro` model and PyMammotion has no mower setting for them, so none.
- Not verified on a Yuka: MowDeck has none; the declaration, reads and writes follow PyMammotion and Mammotion-HA and are tested with the fake port.

## Consequences

- A new setting is one entry in `SETTING_KEYS` and one entry in an adapter; no new command, permission or API.
- mowdeck-hacs can build its switches, numbers and selects from `MowerCapabilities.settings` and read their states from `MowerState.settings`, and add a rain binary sensor from `rain_detected`.
- The web app shows the new command kind in the command history; a settings page in the web app is a follow-up.
- The vendor's turn setting, read only since ADR 0043, can now be changed through `turn_style`; spot runs keep using it as their default.
- None of the Mammotion commands above was sent to the real mower by this change; they are the ones Mammotion-HA sends, verified against PyMammotion 0.10.9 with the fake port. The first hardware test should check the read back of each key, especially `side_led` (Mammotion-HA reads "on" from `enable == 0`).
