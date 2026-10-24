import { beforeEach, describe, expect, it } from 'vitest';

import { setLocale } from '$lib/paraglide/runtime';

import {
  crossesMidnight,
  groupSettings,
  quietSettingResult,
  reportedValue,
  sameValue,
  settingSpecs,
  settingsBlocker,
  valueProblem,
  type SettingSpec,
} from './device-settings';
import {
  blockerText,
  groupLabel,
  optionLabel,
  readableName,
  settingHint,
  settingLabel,
} from './device-settings-labels';

beforeEach(() => setLocale('de', { reload: false }));

const spec = (over: Partial<SettingSpec> & Pick<SettingSpec, 'key' | 'type' | 'group'>) =>
  ({ options: [], ...over }) as SettingSpec;

const SIM: SettingSpec[] = [
  spec({ key: 'rain_detection', type: 'bool', group: 'weather' }),
  spec({
    key: 'recharge_level',
    type: 'int',
    group: 'battery',
    min: 15,
    max: 30,
    step: 1,
    unit: '%',
  }),
  spec({ key: 'night_light', type: 'bool', group: 'lights' }),
  spec({ key: 'turn_style', type: 'enum', group: 'driving', options: ['in_place', 'multi_point'] }),
  spec({ key: 'voice_volume', type: 'int', group: 'voice', min: 0, max: 100, step: 5 }),
  spec({ key: 'non_work_hours', type: 'time_window', group: 'schedule' }),
];

describe('settingSpecs', () => {
  it('reads the declaration of the capabilities', () => {
    expect(settingSpecs({ settings: SIM })).toHaveLength(6);
    expect(settingSpecs(null)).toEqual([]);
    expect(settingSpecs({})).toEqual([]);
    expect(settingSpecs({ settings: 'nope' })).toEqual([]);
  });

  it('drops what no control can fill', () => {
    const specs = settingSpecs({
      settings: [
        { key: 'Bad Key', type: 'bool', group: 'weather' },
        { key: 'a', type: 'colour', group: 'weather' },
        { key: 'b', type: 'int', group: 'battery' },
        { key: 'c', type: 'enum', group: 'driving', options: [] },
        { key: 'd', type: 'bool', group: 'lights' },
        { key: 'd', type: 'bool', group: 'lights' },
        null,
      ],
    });
    expect(specs.map((s) => s.key)).toEqual(['d']);
  });
});

describe('groupSettings', () => {
  it('orders the groups like the contract and keeps unknown groups last', () => {
    const shuffled = [
      SIM[5]!,
      SIM[0]!,
      spec({ key: 'future', type: 'bool', group: 'garden' as never }),
      SIM[3]!,
    ];
    expect(groupSettings(shuffled).map((s) => s.group)).toEqual([
      'weather',
      'driving',
      'schedule',
      'other',
    ]);
  });
});

describe('valueProblem', () => {
  it('checks each type like the controller', () => {
    const [bool, int, , enumSpec, stepped, window] = SIM;
    expect(valueProblem(bool!, true)).toBeNull();
    expect(valueProblem(bool!, 1)).toBe('type');
    expect(valueProblem(int!, 20)).toBeNull();
    expect(valueProblem(int!, 31)).toBe('range');
    expect(valueProblem(int!, 20.5)).toBe('type');
    expect(valueProblem(stepped!, 7)).toBe('step');
    expect(valueProblem(stepped!, 35)).toBeNull();
    expect(valueProblem(enumSpec!, 'multi_point')).toBeNull();
    expect(valueProblem(enumSpec!, 'spin')).toBe('option');
    expect(valueProblem(window!, null)).toBeNull();
    expect(valueProblem(window!, { start: '22:00', end: '06:00' })).toBeNull();
    expect(valueProblem(window!, { start: '22:00', end: '22:00' })).toBe('empty_window');
    expect(valueProblem(window!, { start: '24:00', end: '06:00' })).toBe('clock');
  });
});

describe('reportedValue and sameValue', () => {
  it('takes a reported value that fits the declaration', () => {
    const settings = { rain_detection: false, recharge_level: 99, non_work_hours: null };
    expect(reportedValue(settings, SIM[0]!)).toBe(false);
    expect(reportedValue(settings, SIM[1]!)).toBeUndefined();
    expect(reportedValue(settings, SIM[5]!)).toBeNull();
    expect(reportedValue(settings, SIM[2]!)).toBeUndefined();
    expect(reportedValue(null, SIM[0]!)).toBeUndefined();
  });

  it('compares windows by their times', () => {
    expect(sameValue({ start: '22:00', end: '06:00' }, { start: '22:00', end: '06:00' })).toBe(
      true,
    );
    expect(sameValue({ start: '22:00', end: '06:00' }, null)).toBe(false);
    expect(sameValue(20, 20)).toBe(true);
    expect(crossesMidnight({ start: '22:00', end: '06:00' })).toBe(true);
    expect(crossesMidnight({ start: '08:00', end: '12:00' })).toBe(false);
  });
});

describe('settingsBlocker', () => {
  const ok = { allowed: true, features: ['view', 'control'], restricted: false, offline: false };

  it('names the first reason in order: right, plan, link', () => {
    expect(settingsBlocker(ok)).toBeNull();
    expect(settingsBlocker({ ...ok, features: null })).toBeNull();
    expect(settingsBlocker({ ...ok, features: ['*'] })).toBeNull();
    expect(settingsBlocker({ ...ok, allowed: false, offline: true })).toBe('no_permission');
    expect(settingsBlocker({ ...ok, features: ['view'] })).toBe('plan');
    expect(settingsBlocker({ ...ok, restricted: true })).toBe('plan');
    expect(settingsBlocker({ ...ok, offline: true })).toBe('offline');
  });

  it('quiets only an executed set_setting', () => {
    expect(quietSettingResult({ kind: 'set_setting', status: 'executed' })).toBe(true);
    expect(quietSettingResult({ kind: 'set_setting', status: 'rejected' })).toBe(false);
    expect(quietSettingResult({ kind: 'set_light', status: 'executed' })).toBe(false);
  });
});

describe('labels', () => {
  it('translates known keys, options and groups', () => {
    expect(settingLabel('rain_detection')).toBe('Regenerkennung');
    expect(settingHint('rain_detection')).toContain('Regen');
    expect(optionLabel('turn_style', 'multi_point')).toBe('Mehrpunkt');
    expect(groupLabel('schedule')).toBe('Arbeitszeit');
    expect(groupLabel('other')).toBe('Weitere');
    expect(blockerText('offline')).toContain('offline');
  });

  it('falls back to readable names for what it does not know', () => {
    expect(settingLabel('mulch_mode')).toBe('Mulch mode');
    expect(settingHint('mulch_mode')).toBeNull();
    expect(settingLabel('constructor')).toBe('Constructor');
    expect(optionLabel('turn_style', 'zero_radius')).toBe('Zero radius');
    expect(readableName('__')).toBe('__');
  });
});
