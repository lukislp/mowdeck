/**
 * Words of the mower's own settings (de and en through Paraglide): the known keys of the
 * contract (`SETTING_KEYS`) with a hint, their options and the groups. A key, option or group the
 * app does not know yet (a newer adapter) is shown readable from its name.
 */
import { m } from '#lib/paraglide/messages.js';

import type { SettingGroup, SettingsBlocker } from './device-settings';

type Text = () => string;

const KEYS: Record<string, readonly [Text, Text]> = {
  rain_detection: [m.devset_key_rain_detection, m.devset_hint_rain_detection],
  recharge_level: [m.devset_key_recharge_level, m.devset_hint_recharge_level],
  smart_recharge_level: [m.devset_key_smart_recharge_level, m.devset_hint_smart_recharge_level],
  resume_level: [m.devset_key_resume_level, m.devset_hint_resume_level],
  smart_resume_level: [m.devset_key_smart_resume_level, m.devset_hint_smart_resume_level],
  night_light: [m.devset_key_night_light, m.devset_hint_night_light],
  side_led: [m.devset_key_side_led, m.devset_hint_side_led],
  turn_style: [m.devset_key_turn_style, m.devset_hint_turn_style],
  traversal_mode: [m.devset_key_traversal_mode, m.devset_hint_traversal_mode],
  wildlife_safety: [m.devset_key_wildlife_safety, m.devset_hint_wildlife_safety],
  voice_enabled: [m.devset_key_voice_enabled, m.devset_hint_voice_enabled],
  voice_volume: [m.devset_key_voice_volume, m.devset_hint_voice_volume],
  voice_gender: [m.devset_key_voice_gender, m.devset_hint_voice_gender],
  non_work_hours: [m.devset_key_non_work_hours, m.devset_hint_non_work_hours],
};

const OPTIONS: Record<string, Text> = {
  'turn_style:in_place': m.devset_opt_turn_style_in_place,
  'turn_style:multi_point': m.devset_opt_turn_style_multi_point,
  'traversal_mode:direct': m.devset_opt_traversal_mode_direct,
  'traversal_mode:follow_perimeter': m.devset_opt_traversal_mode_follow_perimeter,
  'wildlife_safety:off': m.devset_opt_wildlife_safety_off,
  'wildlife_safety:stop_mowing': m.devset_opt_wildlife_safety_stop_mowing,
  'wildlife_safety:low_speed_mowing': m.devset_opt_wildlife_safety_low_speed_mowing,
  'voice_gender:man': m.devset_opt_voice_gender_man,
  'voice_gender:woman': m.devset_opt_voice_gender_woman,
};

const lookup = <T>(table: Record<string, T>, key: string): T | undefined =>
  Object.hasOwn(table, key) ? table[key] : undefined;

/** `low_speed_mowing` -> "Low speed mowing": a name the app has no words for, readable. */
export function readableName(name: string): string {
  const words = name.replace(/[_-]+/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : name;
}

export function settingLabel(key: string): string {
  return lookup(KEYS, key)?.[0]() ?? readableName(key);
}

/** The hint under a known setting; null for one the app does not know. */
export function settingHint(key: string): string | null {
  return lookup(KEYS, key)?.[1]() ?? null;
}

export function optionLabel(key: string, option: string): string {
  return lookup(OPTIONS, `${key}:${option}`)?.() ?? readableName(option);
}

export function groupLabel(group: SettingGroup | 'other' | string): string {
  switch (group) {
    case 'weather':
      return m.devset_group_weather();
    case 'battery':
      return m.devset_group_battery();
    case 'lights':
      return m.devset_group_lights();
    case 'driving':
      return m.devset_group_driving();
    case 'safety':
      return m.devset_group_safety();
    case 'voice':
      return m.devset_group_voice();
    case 'schedule':
      return m.devset_group_schedule();
    default:
      return m.devset_group_other();
  }
}

export function blockerText(blocker: SettingsBlocker): string {
  switch (blocker) {
    case 'no_permission':
      return m.devset_block_no_permission();
    case 'plan':
      return m.devset_block_plan();
    case 'offline':
      return m.devset_block_offline();
  }
}
