import { beforeEach, describe, expect, it } from 'vitest';

import { setLocale } from '#lib/paraglide/runtime.js';

import {
  MAINTENANCE_ORDER,
  maintenanceBlocker,
  neededRight,
  offeredActions,
  resultTone,
  type MaintenanceInput,
} from './maintenance';
import { actionText, maintenanceBlockerText } from './maintenance-labels';

beforeEach(() => setLocale('de', { reload: false }));

function input(over: Partial<MaintenanceInput> = {}, activity = 'docked'): MaintenanceInput {
  return {
    state: { activity, job: null },
    offline: false,
    canControl: true,
    canManage: true,
    features: ['view', 'control'],
    restricted: false,
    driveSession: false,
    pending: new Set(),
    ...over,
  };
}

describe('offeredActions', () => {
  it('lists the declared actions in page order', () => {
    expect(
      offeredActions({ maintenance: ['sync_map', 'restart_mower', 'leave_dock', 'x'] }),
    ).toEqual(['leave_dock', 'sync_map', 'restart_mower']);
    expect(offeredActions({})).toEqual([]);
    expect(offeredActions(null)).toEqual([]);
  });

  it('asks can_control only for leaving the dock', () => {
    expect(MAINTENANCE_ORDER.map(neededRight)).toEqual([
      'can_control',
      'can_manage_mowers',
      'can_manage_mowers',
      'can_manage_mowers',
      'can_manage_mowers',
    ]);
  });
});

describe('maintenanceBlocker', () => {
  it('allows everything on a docked mower', () => {
    for (const action of MAINTENANCE_ORDER) expect(maintenanceBlocker(action, input())).toBeNull();
  });

  it('checks rights per action, then the plan, then the link', () => {
    const operator = input({ canManage: false });
    expect(maintenanceBlocker('leave_dock', operator)).toBeNull();
    expect(maintenanceBlocker('restart_mower', operator)).toBe('no_permission');
    expect(maintenanceBlocker('leave_dock', input({ canControl: false }))).toBe('no_permission');
    expect(maintenanceBlocker('sync_map', input({ features: ['view'] }))).toBe('plan');
    expect(maintenanceBlocker('sync_map', input({ features: ['*'] }))).toBeNull();
    expect(maintenanceBlocker('sync_map', input({ restricted: true }))).toBe('plan');
    expect(maintenanceBlocker('sync_map', input({ offline: true }))).toBe('offline');
    expect(maintenanceBlocker('sync_map', input({ state: null }))).toBe('offline');
    expect(maintenanceBlocker('sync_map', input({}, 'offline'))).toBe('offline');
  });

  it('follows the safety rules per activity', () => {
    expect(maintenanceBlocker('restart_mower', input({}, 'idle'))).toBeNull();
    expect(maintenanceBlocker('restart_mower', input({}, 'mowing'))).toBe('not_at_rest');
    expect(maintenanceBlocker('restart_mower', input({}, 'error'))).toBe('error');
    expect(maintenanceBlocker('relocate_dock', input({}, 'idle'))).toBe('not_docked');
    expect(maintenanceBlocker('leave_dock', input({}, 'charging'))).toBeNull();
    expect(maintenanceBlocker('leave_dock', input({}, 'returning'))).toBe('not_docked');
    expect(maintenanceBlocker('sync_map', input({}, 'mowing'))).toBeNull();
    expect(maintenanceBlocker('resync_rtk', input({}, 'error'))).toBeNull();
    expect(
      maintenanceBlocker('relocate_dock', input({ state: { activity: 'docked', job: {} } })),
    ).toBe('job');
  });

  it('keeps an open drive session for what may not interrupt it', () => {
    const driving = input({ driveSession: true }, 'idle');
    expect(maintenanceBlocker('sync_map', driving)).toBe('drive_session');
    expect(maintenanceBlocker('restart_mower', driving)).toBe('drive_session');
    expect(maintenanceBlocker('resync_rtk', driving)).toBeNull();
  });

  it('refuses everything while the mower is busy', () => {
    expect(maintenanceBlocker('resync_rtk', input({}, 'updating'))).toBe('busy');
    const spot = input({ state: { activity: 'idle', spot_run: { state: 'spot_mowing' } } });
    expect(maintenanceBlocker('resync_rtk', spot)).toBe('busy');
    const spotDone = input({ state: { activity: 'idle', spot_run: { state: 'done' } } });
    expect(maintenanceBlocker('resync_rtk', spotDone)).toBeNull();
    const calibrating = input({
      state: { activity: 'idle', drive_calibration: { state: 'measuring' } },
    });
    expect(maintenanceBlocker('sync_map', calibrating)).toBe('busy');
    const installing = input({
      state: { activity: 'docked', firmware_info: { update_state: 'installing' } },
    });
    expect(maintenanceBlocker('sync_map', installing)).toBe('busy');
  });

  it('runs one unconfirmed command at a time and no restart during a map sync', () => {
    expect(maintenanceBlocker('sync_map', input({ pending: new Set(['sync_map']) }))).toBe(
      'pending',
    );
    expect(maintenanceBlocker('leave_dock', input({ pending: new Set(['restart_mower']) }))).toBe(
      'pending',
    );
    expect(maintenanceBlocker('restart_mower', input({ pending: new Set(['sync_map']) }))).toBe(
      'pending',
    );
    expect(
      maintenanceBlocker('resync_rtk', input({ pending: new Set(['restart_mower']) })),
    ).toBeNull();
  });
});

describe('results and words', () => {
  it('reads accepted and executed as done', () => {
    expect(resultTone('accepted')).toBe('ok');
    expect(resultTone('executed')).toBe('ok');
    expect(resultTone('rejected')).toBe('error');
    expect(resultTone('expired')).toBe('error');
    expect(resultTone('pending')).toBe('pending');
  });

  it('explains each action and each reason', () => {
    for (const action of MAINTENANCE_ORDER) {
      const text = actionText(action);
      expect(text.label && text.short && text.body && text.rule).toBeTruthy();
    }
    expect(actionText('restart_mower').label).toBe('Mäher neu starten');
    expect(maintenanceBlockerText('no_permission', 'leave_dock')).toContain('steuern');
    expect(maintenanceBlockerText('no_permission', 'sync_map')).toContain('verwalten');
    expect(maintenanceBlockerText('not_at_rest', 'restart_mower')).toContain('Auftrag');
  });
});
