import { beforeEach, describe, expect, it } from 'vitest';

import { setLocale } from '$lib/paraglide/runtime';

import {
  correctionLabel,
  baseStationProblemText,
  baseUpdateBlockerText,
  firmwareStateLabel,
  solutionLabel,
} from './base-station-labels';
import {
  baseStationDevices,
  baseUpdateBlocker,
  canServe,
  commandSettled,
  formatLatLon,
  newerInfo,
  reportOf,
  updateProgress,
  type BaseStationState,
} from './base-stations';

beforeEach(() => setLocale('de', { reload: false }));

const cloudBase = { source: 'cloud' as const, linked_account_id: 'acc-1' };
const simBase = { source: 'simulated' as const, linked_account_id: null };

describe('canServe', () => {
  it('pairs a cloud base station with cloud mowers of its own account', () => {
    expect(canServe(cloudBase, { connection_kind: 'cloud', linked_account_id: 'acc-1' })).toBe(
      true,
    );
    expect(canServe(cloudBase, { connection_kind: 'cloud', linked_account_id: 'acc-2' })).toBe(
      false,
    );
    expect(canServe(cloudBase, { connection_kind: 'ble_bridge', linked_account_id: 'acc-1' })).toBe(
      false,
    );
    expect(
      canServe(
        { source: 'cloud', linked_account_id: null },
        {
          connection_kind: 'cloud',
          linked_account_id: null,
        },
      ),
    ).toBe(false);
  });

  it('pairs the simulated base station with simulated mowers', () => {
    expect(canServe(simBase, { connection_kind: 'simulated', linked_account_id: null })).toBe(true);
    expect(canServe(simBase, { connection_kind: 'cloud', linked_account_id: 'acc-1' })).toBe(false);
  });
});

describe('baseStationDevices', () => {
  it('keeps the RTK base stations, free ones first', () => {
    const device = (name: string, type: string, registered = false) =>
      ({
        device_name: name,
        device_type: type,
        registered,
        registered_elsewhere: false,
      }) as never;
    const found = baseStationDevices([
      device('Luba-1', 'mower'),
      device('RTK-B', 'rtk_base', true),
      device('RTK-C', 'rtk_base'),
      device('Tracker', 'other'),
      device('RTK-A', 'rtk_base'),
    ]);
    expect(found.map((d: { device_name: string }) => d.device_name)).toEqual([
      'RTK-A',
      'RTK-C',
      'RTK-B',
    ]);
  });
});

describe('reportOf', () => {
  const base = { id: 'b1', mower_ids: ['m1', 'm2', 'm3'] };
  const state = (at: string, id = 'b1'): BaseStationState => ({
    base_station_id: id,
    details_at: at,
    satellites: 30,
    correction_link: 'lora',
    rtk_status: 'fixed',
  });

  it('takes the newest report of a linked mower about this base station', () => {
    const states: Record<string, { base_station?: BaseStationState | null }> = {
      m1: { base_station: state('2026-10-23T10:00:00Z') },
      m2: { base_station: state('2026-10-23T11:00:00Z') },
      m3: { base_station: state('2026-10-23T12:00:00Z', 'other') },
    };
    expect(reportOf(base, (id) => states[id])?.mowerId).toBe('m2');
  });

  it('is null without a report', () => {
    expect(reportOf(base, () => ({ base_station: null }))).toBeNull();
    expect(reportOf(base, () => undefined)).toBeNull();
  });
});

describe('baseUpdateBlocker', () => {
  const ok = {
    allowed: true,
    features: ['control'],
    restricted: false,
    relay: { activity: 'docked', offline: false, capable: true, driveSession: false },
    info: { update_state: 'available' as const, available: '1.1' },
  };

  it('allows a resting, capable mower to relay the offered version', () => {
    expect(baseUpdateBlocker(ok)).toBeNull();
    expect(baseUpdateBlocker({ ...ok, relay: { ...ok.relay, activity: 'idle' } })).toBeNull();
  });

  it('names the first reason in order', () => {
    expect(baseUpdateBlocker({ ...ok, allowed: false })).toBe('no_permission');
    expect(baseUpdateBlocker({ ...ok, features: ['view'] })).toBe('plan');
    expect(baseUpdateBlocker({ ...ok, restricted: true })).toBe('plan');
    expect(baseUpdateBlocker({ ...ok, relay: null })).toBe('no_mower');
    expect(baseUpdateBlocker({ ...ok, relay: { ...ok.relay, offline: true } })).toBe('offline');
    expect(baseUpdateBlocker({ ...ok, relay: { ...ok.relay, capable: false } })).toBe(
      'unsupported',
    );
    expect(baseUpdateBlocker({ ...ok, info: null })).toBe('no_check');
    expect(baseUpdateBlocker({ ...ok, info: { update_state: 'installing' } })).toBe('installing');
    expect(baseUpdateBlocker({ ...ok, info: { update_state: 'idle' } })).toBe('no_update');
    for (const activity of ['mowing', 'paused', 'returning', 'updating']) {
      expect(baseUpdateBlocker({ ...ok, relay: { ...ok.relay, activity } })).toBe('mower_busy');
    }
    expect(baseUpdateBlocker({ ...ok, relay: { ...ok.relay, driveSession: true } })).toBe(
      'drive_session',
    );
  });
});

describe('firmware helpers', () => {
  it('reads the progress only while installing', () => {
    expect(updateProgress({ update_state: 'installing', progress: 42.4 })).toBe(42);
    expect(updateProgress({ update_state: 'installing', progress: 140 })).toBe(100);
    expect(updateProgress({ update_state: 'available', progress: 42 })).toBeNull();
    expect(updateProgress(null)).toBeNull();
  });

  it('keeps the newer of the report and the check', () => {
    const old = {
      checked_at: '2026-10-23T10:00:00Z',
      available: 'a',
      update_state: 'available' as const,
    };
    const fresh = {
      checked_at: '2026-10-23T11:00:00Z',
      available: 'b',
      update_state: 'available' as const,
    };
    expect(newerInfo(old, fresh)).toBe(fresh);
    expect(newerInfo(fresh, old)).toBe(fresh);
    expect(newerInfo(null, old)).toBe(old);
    expect(newerInfo(old, undefined)).toBe(old);
    expect(newerInfo(null, null)).toBeNull();
  });

  it('settles a command when a result arrived', () => {
    expect(commandSettled({ status: 'pending', result_at: null })).toBe(false);
    expect(commandSettled({ status: 'accepted', result_at: null })).toBe(false);
    expect(commandSettled({ status: 'accepted', result_at: '2026-10-23T10:00:00Z' })).toBe(true);
    expect(commandSettled({ status: 'rejected', result_at: null })).toBe(true);
  });

  it('formats a position to about 10 cm', () => {
    expect(formatLatLon({ lat: 48.1371306266, lon: 11.5760904013 })).toBe('48.137131, 11.576090');
  });
});

describe('labels', () => {
  it('translates links, solutions, states and reasons', () => {
    expect(correctionLabel('lora')).toBe('LoRa-Funk');
    expect(correctionLabel('unknown')).toBe('unbekannt');
    expect(solutionLabel('fixed')).toBe('RTK Fix');
    expect(firmwareStateLabel('installing')).toBe('Update läuft');
    expect(firmwareStateLabel(undefined)).toBe('Noch nicht geprüft');
    expect(baseUpdateBlockerText('mower_busy')).toContain('ruht');
  });

  it('words Fleet refusals', () => {
    const problem = (slug: string) => ({ slug, status: 409 }) as never;
    expect(baseStationProblemText(problem('base-station-not-applicable'))).toContain('passt nicht');
    expect(baseStationProblemText(problem('base-station-already-registered'))).toContain('bereits');
  });
});
