import { beforeEach, describe, expect, it } from 'vitest';

import { setLocale } from '#lib/paraglide/runtime.js';

import {
  DEFAULT_LIFETIME,
  LIFETIMES,
  MAX_LIFETIME,
  SCOPES,
  apiBase,
  daysLeft,
  expiresSoon,
  isActive,
  lifeLeft,
  personalTokenBody,
  quickStart,
  scopeName,
  toggleScope,
  viewLocked,
  type PersonalToken,
} from './personal-tokens';

beforeEach(() => setLocale('de', { reload: false }));

const DAY = 86_400_000;
const NOW = Date.parse('2026-10-28T12:00:00Z');

function token(overrides: Partial<PersonalToken> = {}): PersonalToken {
  return {
    id: '00000000-0000-4000-8000-000000000001',
    name: 'Script',
    prefix: 'mdk_abcdefgh',
    scopes: ['mower.view'],
    level: { type: 'account', id: '00000000-0000-4000-8000-000000000002' },
    created_at: new Date(NOW - 10 * DAY).toISOString(),
    expires_at: new Date(NOW + 30 * DAY).toISOString(),
    last_used_at: null,
    revoked_at: null,
    token: null,
    ...overrides,
  };
}

describe('scopes', () => {
  it('offers the curated list only, read first', () => {
    expect(SCOPES).toEqual(['mower.view', 'mower.control', 'schedule.edit']);
  });

  it('adds read with any other scope and keeps it while needed', () => {
    expect(toggleScope([], 'mower.control', true)).toEqual(['mower.view', 'mower.control']);
    expect(viewLocked(['mower.view', 'mower.control'])).toBe(true);
    expect(toggleScope(['mower.view', 'mower.control'], 'mower.view', false)).toEqual([
      'mower.view',
      'mower.control',
    ]);
    expect(toggleScope(['mower.view', 'mower.control'], 'mower.control', false)).toEqual([
      'mower.view',
    ]);
    expect(toggleScope(['mower.view'], 'mower.view', false)).toEqual([]);
  });

  it('names scopes in German and falls back to the key', () => {
    expect(scopeName('mower.control')).toBe('Steuern');
    expect(scopeName('something.else')).toBe('something.else');
  });
});

describe('lifetime', () => {
  it('offers 30 days to a year, 90 by default', () => {
    expect(LIFETIMES).toEqual([30, 90, 180, 365]);
    expect(DEFAULT_LIFETIME).toBe(90);
    expect(MAX_LIFETIME).toBe(365);
  });

  it('counts the days left and the share of the lifetime', () => {
    const t = token();
    expect(daysLeft(t.expires_at, NOW)).toBe(30);
    expect(lifeLeft(t, NOW)).toBeCloseTo(0.75);
    expect(lifeLeft(t, NOW + 40 * DAY)).toBe(0);
  });

  it('marks expired, revoked and soon expiring tokens', () => {
    expect(isActive(token(), NOW)).toBe(true);
    expect(isActive(token({ revoked_at: new Date(NOW).toISOString() }), NOW)).toBe(false);
    expect(isActive(token(), NOW + 31 * DAY)).toBe(false);
    expect(expiresSoon(token(), NOW)).toBe(false);
    expect(expiresSoon(token(), NOW + 20 * DAY)).toBe(true);
  });
});

describe('request and quick start', () => {
  it('builds the body with a trimmed name', () => {
    expect(personalTokenBody('  Rain script ', ['mower.view'], 30)).toEqual({
      name: 'Rain script',
      scopes: ['mower.view'],
      expires_in_days: 30,
    });
  });

  it('points at this installation and never contains a secret', () => {
    expect(apiBase('https://mow.example/')).toBe('https://mow.example/api/v1');
    const example = quickStart('https://mow.example');
    expect(example).toContain('export MOWDECK_URL=https://mow.example/api/v1');
    expect(example).toContain('$MOWDECK_URL/identity/token');
    expect(example).toContain('$MOWDECK_TOKEN');
    expect(example).not.toContain('mdk_');
  });
});
