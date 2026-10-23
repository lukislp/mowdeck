/**
 * The mower's own settings (ADR 0046) as the settings page shows them: the declaration in
 * `MowerCapabilities.settings` decides which rows exist, their control and range; the values come
 * from `MowerState.settings`; a change is the command `set_setting {key, value}`. Pure and unit
 * tested (`device-settings.test.ts`); the words are in `device-settings-labels.ts`.
 */
import type { components } from '$lib/generated/telemetry';

type Schemas = components['schemas'];
export type SettingSpec = Schemas['SettingSpec'];
export type SettingGroup = Schemas['SettingGroup'];
export type TimeWindow = Schemas['TimeWindow'];
export type SettingValue = boolean | number | string | TimeWindow | null;

/** Groups in the order the page shows them (the contract's order). */
export const GROUP_ORDER: readonly SettingGroup[] = [
  'weather',
  'battery',
  'lights',
  'driving',
  'safety',
  'voice',
  'schedule',
];

const TYPES = new Set(['bool', 'int', 'enum', 'time_window']);
const KEY = /^[a-z][a-z0-9_]{0,47}$/;

/**
 * The declared settings of the capabilities, checked: unknown types, bad keys, int settings
 * without a range and enum settings without options are dropped (the page never offers a control
 * it cannot fill). Takes the capabilities as the API sends them; null or none give [].
 */
export function settingSpecs(capabilities: object | null | undefined): SettingSpec[] {
  const raw = (capabilities as { settings?: unknown } | null | undefined)?.settings;
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const specs: SettingSpec[] = [];
  for (const item of raw as Partial<SettingSpec>[]) {
    if (!item || typeof item.key !== 'string' || !KEY.test(item.key) || seen.has(item.key))
      continue;
    if (!TYPES.has(item.type as string)) continue;
    if (item.type === 'int' && (typeof item.min !== 'number' || typeof item.max !== 'number'))
      continue;
    if (item.type === 'enum' && !(Array.isArray(item.options) && item.options.length > 0)) continue;
    seen.add(item.key);
    specs.push(item as SettingSpec);
  }
  return specs;
}

export interface SettingSection {
  group: SettingGroup | 'other';
  specs: SettingSpec[];
}

/** The settings by group in `GROUP_ORDER`, declaration order inside a group; empty groups left out. */
export function groupSettings(specs: readonly SettingSpec[]): SettingSection[] {
  const known = new Set<string>(GROUP_ORDER);
  const sections: SettingSection[] = GROUP_ORDER.map((group) => ({
    group,
    specs: specs.filter((s) => s.group === group),
  }));
  sections.push({ group: 'other', specs: specs.filter((s) => !known.has(s.group)) });
  return sections.filter((section) => section.specs.length > 0);
}

/** `HH:MM` on a 24-hour clock. */
const CLOCK = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Why `value` does not fit `spec`, or null when it does (the controller's rule, mirrored). */
export function valueProblem(
  spec: SettingSpec,
  value: SettingValue,
): 'type' | 'range' | 'step' | 'option' | 'clock' | 'empty_window' | null {
  switch (spec.type) {
    case 'bool':
      return typeof value === 'boolean' ? null : 'type';
    case 'int': {
      if (typeof value !== 'number' || !Number.isInteger(value)) return 'type';
      const min = spec.min ?? Number.NEGATIVE_INFINITY;
      const max = spec.max ?? Number.POSITIVE_INFINITY;
      if (value < min || value > max) return 'range';
      if (spec.step && spec.min != null && (value - spec.min) % spec.step !== 0) return 'step';
      return null;
    }
    case 'enum':
      return typeof value === 'string' && (spec.options ?? []).includes(value) ? null : 'option';
    case 'time_window': {
      if (value === null) return null;
      if (typeof value !== 'object' || !CLOCK.test(value.start) || !CLOCK.test(value.end))
        return 'clock';
      return value.start === value.end ? 'empty_window' : null;
    }
  }
}

/** The reported value of a declared setting; undefined while the mower has not reported it. */
export function reportedValue(
  settings: Record<string, unknown> | null | undefined,
  spec: SettingSpec,
): SettingValue | undefined {
  if (!settings || !Object.hasOwn(settings, spec.key)) return undefined;
  const value = settings[spec.key];
  if (value === undefined) return undefined;
  return valueProblem(spec, value as SettingValue) === null ? (value as SettingValue) : undefined;
}

/** Two values are the same setting (a window by its clock times). */
export function sameValue(a: SettingValue | undefined, b: SettingValue | undefined): boolean {
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    return a.start === b.start && a.end === b.end;
  }
  return a === b;
}

/** Why the settings cannot be changed now; null when they can. */
export type SettingsBlocker = 'no_permission' | 'plan' | 'offline';

export function settingsBlocker(input: {
  /** `can_manage_mowers` on the mower. */
  allowed: boolean;
  /** The plan's features (`GET /entitlements/me`); null while unknown or without Entitlements. */
  features: readonly string[] | null;
  /** The plan is in grace or expired: only viewing and the emergency stop remain. */
  restricted: boolean;
  /** No live link to the mower (or no state at all). */
  offline: boolean;
}): SettingsBlocker | null {
  if (!input.allowed) return 'no_permission';
  if (input.restricted) return 'plan';
  if (input.features && !input.features.includes('*') && !input.features.includes('control'))
    return 'plan';
  if (input.offline) return 'offline';
  return null;
}

/** Minutes of a day of an `HH:MM` value. */
export function clockMinutes(clock: string): number {
  const [h, m] = clock.split(':');
  return Number(h) * 60 + Number(m);
}

/** True when the window crosses midnight (`22:00` to `06:00`). */
export function crossesMidnight(window: TimeWindow): boolean {
  return clockMinutes(window.end) < clockMinutes(window.start);
}

/**
 * A `set_setting` the mower executed needs no toast: its row says "applied by the mower". A
 * refused or failed one still toasts (the row may be scrolled away).
 */
export function quietSettingResult(command: { kind: string; status: string }): boolean {
  return command.kind === 'set_setting' && command.status === 'executed';
}
