import { describe, expect, it } from 'vitest';

import {
  installProgress,
  updateBlocker,
  updateOffered,
  type FirmwareInfo,
  type UpdateConditions,
} from './firmware';

const offered: FirmwareInfo = {
  installed: '1.0.0',
  available: '1.1.0',
  update_state: 'available',
};

function conditions(override: Partial<UpdateConditions> = {}): UpdateConditions {
  return {
    info: offered,
    activity: 'charging',
    charging: true,
    batteryPercent: 80,
    stale: false,
    allowed: true,
    ...override,
  };
}

describe('updateBlocker', () => {
  it('allows the update when docked, charging, full enough and idle', () => {
    expect(updateBlocker(conditions())).toBeNull();
    expect(updateBlocker(conditions({ activity: 'docked' }))).toBeNull();
    expect(updateBlocker(conditions({ batteryPercent: 50 }))).toBeNull();
  });

  it('names the first unmet condition in the order the user can act on it', () => {
    expect(updateBlocker(conditions({ allowed: false }))).toBe('no_permission');
    expect(updateBlocker(conditions({ info: null }))).toBe('no_update');
    expect(updateBlocker(conditions({ info: { ...offered, update_state: 'idle' } }))).toBe(
      'no_update',
    );
    expect(updateBlocker(conditions({ info: { ...offered, available: null } }))).toBe('no_update');
    expect(updateBlocker(conditions({ info: { ...offered, update_state: 'installing' } }))).toBe(
      'installing',
    );
    expect(updateBlocker(conditions({ activity: 'updating' }))).toBe('installing');
    expect(updateBlocker(conditions({ activity: 'offline' }))).toBe('offline');
    expect(updateBlocker(conditions({ activity: null }))).toBe('offline');
    expect(updateBlocker(conditions({ stale: true }))).toBe('offline');
    expect(updateBlocker(conditions({ activity: 'mowing' }))).toBe('job');
    expect(updateBlocker(conditions({ activity: 'paused' }))).toBe('job');
    expect(updateBlocker(conditions({ activity: 'idle', charging: false }))).toBe('not_docked');
    expect(updateBlocker(conditions({ activity: 'docked', charging: false }))).toBe('not_charging');
    expect(updateBlocker(conditions({ batteryPercent: 49 }))).toBe('battery');
    expect(updateBlocker(conditions({ batteryPercent: null }))).toBe('battery');
  });

  it('offers a retry after a failed update with the version still available', () => {
    const failed: FirmwareInfo = { ...offered, update_state: 'failed', failure_reason: 'x' };
    expect(updateOffered(failed)).toBe(true);
    expect(updateBlocker(conditions({ info: failed }))).toBeNull();
    expect(updateOffered({ ...offered, update_state: 'unknown' })).toBe(false);
    expect(updateOffered(null)).toBe(false);
  });
});

describe('installProgress', () => {
  it('draws only while installing and clamps the percent', () => {
    expect(installProgress(offered)).toBeNull();
    expect(installProgress({ ...offered, update_state: 'installing' })).toBe(0);
    expect(installProgress({ ...offered, update_state: 'installing', progress: 42 })).toBe(42);
    expect(installProgress({ ...offered, update_state: 'installing', progress: 140 })).toBe(100);
  });
});
