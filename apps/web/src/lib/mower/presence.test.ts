import { describe, expect, it } from 'vitest';

import type { components } from '#lib/generated/telemetry.js';

import {
  OFFLINE_AFTER_MS,
  VALUES_OLD_AFTER_MS,
  isNewer,
  isOnline,
  linkSeenAt,
  oldValuesAge,
} from './presence';

type MowerState = components['schemas']['MowerState'];

const T0 = Date.parse('2026-10-15T10:00:00Z');

function state(fields: Partial<MowerState> = {}): MowerState {
  return {
    mower_id: 'm1',
    account_id: 'a1',
    observed_at: '2026-10-15T10:00:00Z',
    activity: 'docked',
    battery_percent: 90,
    connection: 'cloud',
    connected: true,
    ...fields,
  } as MowerState;
}

describe('presence', () => {
  it('keeps a docked mower with old values online while heartbeats arrive', () => {
    const docked = state({ heartbeat_at: '2026-10-15T10:20:00Z' });
    const now = Date.parse('2026-10-15T10:20:30Z');
    expect(isOnline(docked, now)).toBe(true);
    expect(oldValuesAge(docked, now)).toBe(now - T0);
  });

  it('goes offline when the heartbeats stop', () => {
    const beat = Date.parse('2026-10-15T10:20:00Z');
    const docked = state({ heartbeat_at: '2026-10-15T10:20:00Z' });
    expect(isOnline(docked, beat + OFFLINE_AFTER_MS)).toBe(true);
    expect(isOnline(docked, beat + OFFLINE_AFTER_MS + 1)).toBe(false);
    expect(oldValuesAge(docked, beat + OFFLINE_AFTER_MS + 1)).toBeNull();
  });

  it('is offline at once when the gateway reports the link lost', () => {
    const lost = state({ heartbeat_at: '2026-10-15T10:00:05Z', connected: false });
    expect(isOnline(lost, T0 + 6_000)).toBe(false);
  });

  it('falls back to observed_at for states without a heartbeat', () => {
    const old = state({ heartbeat_at: undefined, connected: undefined } as Partial<MowerState>);
    expect(linkSeenAt(old)).toBe(T0);
    expect(isOnline(old, T0 + OFFLINE_AFTER_MS)).toBe(true);
    expect(isOnline(old, T0 + OFFLINE_AFTER_MS + 1)).toBe(false);
    expect(linkSeenAt(state({ heartbeat_at: 'not a time' }))).toBe(T0);
  });

  it('points out old values only after a few minutes', () => {
    const fresh = state({ heartbeat_at: '2026-10-15T10:03:00Z' });
    expect(oldValuesAge(fresh, T0 + VALUES_OLD_AFTER_MS)).toBeNull();
    expect(oldValuesAge(fresh, T0 + VALUES_OLD_AFTER_MS + 1)).toBe(VALUES_OLD_AFTER_MS + 1);
  });

  it('orders by report, then by heartbeat', () => {
    const report = state();
    const beat = state({ heartbeat_at: '2026-10-15T10:00:10Z' });
    const later = state({ observed_at: '2026-10-15T10:00:20Z' });
    expect(isNewer(report, beat)).toBe(true);
    expect(isNewer(beat, report)).toBe(false);
    expect(isNewer(beat, beat)).toBe(true);
    expect(isNewer(beat, later)).toBe(true);
    expect(isNewer(later, beat)).toBe(false);
  });
});
