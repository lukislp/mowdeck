import { beforeEach, describe, expect, it } from 'vitest';

import { setLocale } from '$lib/paraglide/runtime';

import {
  DEFAULT_CHOICE,
  PRESETS,
  choiceKeys,
  displayOrder,
  impliedBy,
  needsView,
  permissionDescription,
  permissionShort,
  powerfulWarning,
  requestPermissions,
  toggle,
  tokenBody,
  tokenGrantable,
} from './token-permissions';
import { CATALOGUE, entry } from './token-permissions.fixture';

beforeEach(() => setLocale('de', { reload: false }));

describe('presets', () => {
  it('keeps view only, basics and the full Home Assistant set', () => {
    expect(PRESETS.view).toEqual(['mower.view']);
    expect(PRESETS.basic).toEqual(['mower.view', 'mower.control']);
    expect(PRESETS.full).toEqual([
      'mower.view',
      'mower.control',
      'mower.drive',
      'fleet.manage',
      'schedule.edit',
    ]);
  });

  it('defaults to the safer Home Assistant basics', () => {
    expect(DEFAULT_CHOICE).toBe('basic');
    expect(choiceKeys(DEFAULT_CHOICE, [])).toEqual(['mower.view', 'mower.control']);
  });

  it('a custom choice uses the selection', () => {
    expect(choiceKeys('custom', ['mower.drive', 'mower.view'])).toEqual([
      'mower.drive',
      'mower.view',
    ]);
    expect(choiceKeys('full', ['ignored'])).toEqual([...PRESETS.full]);
  });

  it('every preset key is grantable to a token on the account', () => {
    const grantable = new Set(tokenGrantable(CATALOGUE).map((e) => e.key));
    for (const keys of Object.values(PRESETS))
      for (const key of keys) expect(grantable).toContain(key);
  });
});

describe('tokenGrantable', () => {
  it('drops users-only keys and keys that cannot be held on the account', () => {
    const keys = tokenGrantable(CATALOGUE).map((e) => e.key);
    expect(keys).not.toContain('users.manage');
    expect(keys).not.toContain('billing.manage');
    expect(keys).not.toContain('audit.view');
    expect(keys).not.toContain('site.only');
  });

  it('lists the model rights in reading order, then the rest by key', () => {
    expect(tokenGrantable(CATALOGUE).map((e) => e.key)).toEqual([
      'mower.view',
      'mower.control',
      'schedule.edit',
      'mower.drive',
      'fleet.manage',
      'fleet.bulk_command',
      'device_gateway.view',
      'notifications.view',
      'scheduler.view',
      'telemetry.view',
      'vendor.thing',
    ]);
  });
});

describe('displayOrder', () => {
  it('reads like the presets, unknown keys last by key', () => {
    expect(
      displayOrder(['zeta.view', 'fleet.manage', 'mower.control', 'mower.view', 'alpha.view']),
    ).toEqual(['mower.view', 'mower.control', 'fleet.manage', 'alpha.view', 'zeta.view']);
  });
});

describe('impliedBy', () => {
  it('names the account keys mower.view brings along', () => {
    const implied = impliedBy(['mower.view'], CATALOGUE);
    expect([...implied.keys()].sort()).toEqual([
      'device_gateway.view',
      'notifications.view',
      'scheduler.view',
      'telemetry.view',
    ]);
    expect(implied.get('telemetry.view')).toEqual(['mower.view']);
  });

  it('needs every implying key and never implies a model key', () => {
    expect(impliedBy(['mower.control'], CATALOGUE).size).toBe(0);
    expect(impliedBy(['mower.view', 'mower.control'], CATALOGUE).has('vendor.thing')).toBe(true);
    expect(impliedBy([], [entry('mower.view', { implied_by: ['x.y'], in_model: true })]).size).toBe(
      0,
    );
  });
});

describe('toggle', () => {
  it('adds mower.view with a mower or schedule right', () => {
    expect(toggle([], 'mower.drive', true)).toEqual(['mower.drive', 'mower.view']);
    expect(toggle([], 'schedule.edit', true)).toEqual(['mower.view', 'schedule.edit']);
    expect(toggle([], 'fleet.manage', true)).toEqual(['fleet.manage']);
  });

  it('keeps mower.view while a right needs it, otherwise removes', () => {
    expect(toggle(['mower.control', 'mower.view'], 'mower.view', false)).toEqual([
      'mower.control',
      'mower.view',
    ]);
    expect(toggle(['fleet.manage', 'mower.view'], 'mower.view', false)).toEqual(['fleet.manage']);
    expect(toggle(['mower.control', 'mower.view'], 'mower.control', false)).toEqual(['mower.view']);
  });

  it('needsView covers mower and schedule keys only', () => {
    expect(needsView('mower.view')).toBe(false);
    expect(needsView('mower.control')).toBe(true);
    expect(needsView('schedule.edit')).toBe(true);
    expect(needsView('fleet.manage')).toBe(false);
  });
});

describe('requestPermissions and tokenBody', () => {
  it('sends the chosen keys without the implied ones, sorted', () => {
    expect(
      requestPermissions(['mower.view', 'telemetry.view', 'mower.control'], CATALOGUE),
    ).toEqual(['mower.control', 'mower.view']);
    // Without mower.view an account key is a choice of its own.
    expect(requestPermissions(['telemetry.view'], CATALOGUE)).toEqual(['telemetry.view']);
    expect(requestPermissions([], CATALOGUE)).toEqual([]);
  });

  it('builds one account grant with a trimmed name', () => {
    expect(tokenBody('  Home Assistant ', 'acc-1', [...PRESETS.full])).toEqual({
      name: 'Home Assistant',
      grants: [
        {
          level: { type: 'account', id: 'acc-1' },
          permissions: [
            'mower.view',
            'mower.control',
            'mower.drive',
            'fleet.manage',
            'schedule.edit',
          ],
        },
      ],
    });
  });
});

describe('words', () => {
  it('translates known keys and falls back to the catalogue, then the key', () => {
    expect(permissionShort('mower.drive')).toBe('Fahren');
    expect(permissionShort('vendor.thing')).toBe('vendor.thing');
    expect(permissionDescription('fleet.manage')).toContain('Firmware');
    expect(permissionDescription('vendor.thing', entry('vendor.thing'))).toBe(
      'vendor.thing (catalogue)',
    );
    expect(permissionDescription('vendor.thing')).toBe('vendor.thing');
    setLocale('en', { reload: false });
    expect(permissionShort('fleet.manage')).toBe('Manage devices');
  });

  it('warns about driving and device management only', () => {
    expect(powerfulWarning('mower.drive')).toContain('Mäher');
    expect(powerfulWarning('fleet.manage')).toContain('Firmware');
    expect(powerfulWarning('mower.control')).toBeUndefined();
  });
});
