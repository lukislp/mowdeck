import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiProblem } from '$lib/api/problem';
import { setLocale } from '$lib/paraglide/runtime';

import {
  calibrationConditions,
  calibrationPayload,
  calibrationProgress,
  calibrationReasonLabel,
  calibrationStateLabel,
  canStartCalibration,
  currentCalibrationStep,
  driveSpeedCommand,
  driveSpeedLocked,
  fitFacts,
  formatCommand,
  formatDriveSpeed,
  formatMeasured,
  isCalibrationActive,
  isCalibrationFinal,
  isResultStored,
  snapDriveSpeed,
  speedSettingDiffers,
  stepRows,
  storedCalibrationOf,
  type CalibConditionState,
  type DriveCalibration,
  type DriveCalibrationStatus,
  type DriveCalibrationStep,
} from './drive-calibration';
import { DriveCalibrationFlow, type DriveCalibrationDeps } from './drive-calibration.svelte';

const NOW = Date.parse('2026-10-15T10:00:00Z');
const RUN = '0b9e5a8e-8a3c-4f55-9c0b-3d3f0f6a1c01';

const CALIBRATION: DriveCalibration = {
  full_speed_mps: 0.42,
  full_yaw_rate_dps: 48.5,
  dead_zone: 0.08,
  turn_dead_zone: 0.12,
  drive_speed_setting: 0.4,
  calibrated_at: '2026-10-15T09:58:00Z',
};

function step(partial: Partial<DriveCalibrationStep>): DriveCalibrationStep {
  return {
    kind: 'forward',
    command: 0.3,
    duration_s: 2.5,
    measured: 0.091,
    reports: 5,
    report_interval_s: 0.5,
    distance_m: 0.23,
    turned_deg: 0,
    skipped: false,
    ...partial,
  };
}

function status(partial: Partial<DriveCalibrationStatus>): DriveCalibrationStatus {
  return {
    run_id: RUN,
    state: 'measuring',
    step_index: 0,
    steps_total: 10,
    steps: [],
    reason: null,
    result: null,
    started_at: '2026-10-15T09:57:00Z',
    ...partial,
  };
}

function mowerState(partial: Partial<CalibConditionState> = {}): CalibConditionState {
  return {
    activity: 'idle',
    rtk_fix: 'fixed',
    position_source: 'rtk',
    battery_percent: 80,
    observed_at: new Date(NOW - 5_000).toISOString(),
    heartbeat_at: new Date(NOW - 2_000).toISOString(),
    connected: true,
    spot_run: null,
    drive_calibration: null,
    ...partial,
  };
}

beforeEach(() => {
  setLocale('de', { reload: false });
});

describe('calibration state and reasons', () => {
  it('tells active from final runs', () => {
    expect(isCalibrationActive(null)).toBe(false);
    for (const state of ['preparing', 'measuring', 'stopping'] as const) {
      expect(isCalibrationActive(status({ state }))).toBe(true);
      expect(isCalibrationFinal(status({ state }))).toBe(false);
    }
    for (const state of ['done', 'aborted', 'failed'] as const) {
      expect(isCalibrationActive(status({ state }))).toBe(false);
      expect(isCalibrationFinal(status({ state }))).toBe(true);
    }
    expect(calibrationStateLabel('measuring')).toBe('Misst');
  });

  it('translates every known reason in both languages and keeps unknown ones', () => {
    const codes = [
      'stopped_by_user',
      'emergency_stop',
      'sensor_stop',
      'geofence',
      'rtk_lost',
      'activity_change',
      'link_lost',
      'frame_rejected',
      'no_fit',
      'turn_direction',
    ];
    for (const locale of ['de', 'en'] as const) {
      setLocale(locale, { reload: false });
      const labels = codes.map((code) => calibrationReasonLabel(code));
      for (const [i, label] of labels.entries()) expect(label).not.toBe(codes[i]);
      expect(new Set(labels).size).toBe(codes.length);
    }
    setLocale('de', { reload: false });
    expect(calibrationReasonLabel('stopped_by_user')).toBe('per Stopp beendet');
    expect(calibrationReasonLabel('something_new')).toBe('something_new');
    expect(calibrationReasonLabel(null)).toBeNull();
    expect(calibrationReasonLabel('  ')).toBeNull();
  });

  it('keeps the detail the gateway appends to a code', () => {
    expect(calibrationReasonLabel('no_fit: only 1 forward step')).toBe(
      'zu wenige Positionsmeldungen für ein Ergebnis (only 1 forward step)',
    );
    expect(calibrationReasonLabel('weird: thing')).toBe('weird: thing');
  });
});

describe('progress and the current step', () => {
  it('counts the step being driven from 1 and fills by measured steps', () => {
    expect(calibrationProgress(status({ step_index: 2, steps: [step({}), step({})] }))).toEqual({
      i: 3,
      n: 10,
      percent: 20,
    });
    expect(calibrationProgress(status({ step_index: 12 })).i).toBe(10);
    expect(calibrationProgress(status({ state: 'done', step_index: 9 })).percent).toBe(100);
    expect(calibrationProgress(status({ steps_total: 0 }))).toEqual({ i: 0, n: 0, percent: 0 });
  });

  it('reads the current step from the default plan', () => {
    expect(currentCalibrationStep(status({ step_index: 0 }))).toEqual({
      kind: 'forward',
      command: 0.3,
    });
    expect(currentCalibrationStep(status({ step_index: 4 }))).toEqual({
      kind: 'turn',
      command: 0.3,
    });
    expect(currentCalibrationStep(status({ step_index: 9 }))).toEqual({
      kind: 'turn',
      command: -1,
    });
    expect(currentCalibrationStep(status({ state: 'preparing' }))).toBeNull();
    expect(currentCalibrationStep(status({ state: 'done' }))).toBeNull();
    expect(currentCalibrationStep(status({ steps_total: 7 }))).toBeNull();
  });
});

describe('formatting and rows', () => {
  it('signs stick values and adds units', () => {
    expect(formatCommand(0.5)).toBe('+0,50');
    expect(formatCommand(-1)).toBe('−1,00');
    expect(formatCommand(0)).toBe('0,00');
    expect(formatMeasured({ kind: 'forward', measured: 0.1234 })).toBe('0,123 m/s');
    expect(formatMeasured({ kind: 'turn', measured: -45.25 })).toBe('-45,3 °/s');
    expect(formatMeasured({ kind: 'turn', measured: null })).toBe('zu wenige Meldungen');
  });

  it('builds one table row per step', () => {
    const rows = stepRows([
      step({}),
      step({ kind: 'turn', command: -0.6, measured: null, report_interval_s: null, reports: 1 }),
      step({ command: 1, skipped: true, duration_s: 0, measured: null, reports: 0 }),
    ]);
    expect(rows.map((r) => r.index)).toEqual([1, 2, 3]);
    expect(rows[0]).toMatchObject({
      kindLabel: 'Geradeaus',
      command: '+0,30',
      measured: '0,091 m/s',
      reports: '5',
      interval: '0,50 s',
      missing: false,
      skipped: false,
    });
    expect(rows[1]).toMatchObject({
      kindLabel: 'Drehen',
      command: '−0,60',
      measured: 'zu wenige Meldungen',
      interval: '–',
      missing: true,
    });
    expect(rows[2]).toMatchObject({ measured: '–', skipped: true, missing: true });
    expect(stepRows(undefined)).toEqual([]);
  });

  it('lists the fit with the speed setting and the distance when known', () => {
    const facts = fitFacts(CALIBRATION, 1.234);
    expect(facts.map((f) => f.id)).toEqual([
      'full_speed',
      'full_yaw',
      'dead_zone',
      'turn_dead_zone',
      'speed_setting',
      'forward',
    ]);
    expect(facts[0]?.value).toBe('0,420 m/s');
    expect(facts[1]?.value).toBe('48,5 °/s');
    expect(facts[4]?.value).toBe('0,40 m/s');
    expect(facts[5]?.value).toBe('1,23 m');
    const noSetting = fitFacts({ ...CALIBRATION, drive_speed_setting: null });
    expect(noSetting.find((f) => f.id === 'speed_setting')?.value).toBe('unbekannt');
    expect(noSetting.some((f) => f.id === 'forward')).toBe(false);
  });
});

describe('storing a result', () => {
  it('builds the Fleet body with defaults for missing fields', () => {
    expect(calibrationPayload(CALIBRATION)).toEqual({
      full_speed_mps: 0.42,
      full_yaw_rate_dps: 48.5,
      dead_zone: 0.08,
      turn_dead_zone: 0.12,
      drive_speed_setting: 0.4,
      calibrated_at: '2026-10-15T09:58:00Z',
    });
    const sparse = {
      full_speed_mps: 0.3,
      full_yaw_rate_dps: 30,
      calibrated_at: '2026-10-15T09:00:00Z',
    } as DriveCalibration;
    expect(calibrationPayload(sparse)).toMatchObject({
      dead_zone: 0,
      turn_dead_zone: 0,
      drive_speed_setting: null,
    });
  });

  it('reads the stored calibration of a mower record', () => {
    expect(storedCalibrationOf({ drive_calibration: CALIBRATION })).toEqual(CALIBRATION);
    expect(storedCalibrationOf({ drive_calibration: null })).toBeNull();
    expect(storedCalibrationOf({})).toBeNull();
    expect(storedCalibrationOf(null)).toBeNull();
    expect(storedCalibrationOf({ drive_calibration: { full_speed_mps: 1 } })).toBeNull();
  });

  it('knows when the shown result is the stored one', () => {
    expect(isResultStored(CALIBRATION, { ...CALIBRATION })).toBe(true);
    expect(
      isResultStored(CALIBRATION, { ...CALIBRATION, calibrated_at: '2026-10-15T09:58:00.000Z' }),
    ).toBe(true);
    expect(
      isResultStored(CALIBRATION, { ...CALIBRATION, calibrated_at: '2026-10-01T09:00:00Z' }),
    ).toBe(false);
    expect(isResultStored(CALIBRATION, null)).toBe(false);
  });

  it('flags a speed setting that changed since the calibration', () => {
    expect(speedSettingDiffers(0.4, CALIBRATION)).toBe(false);
    expect(speedSettingDiffers(0.402, CALIBRATION)).toBe(false);
    expect(speedSettingDiffers(0.6, CALIBRATION)).toBe(true);
    expect(speedSettingDiffers(null, CALIBRATION)).toBe(false);
    expect(speedSettingDiffers(0.6, { ...CALIBRATION, drive_speed_setting: null })).toBe(false);
    expect(speedSettingDiffers(0.6, null)).toBe(false);
  });
});

describe('start conditions', () => {
  function failing(
    state: CalibConditionState | null,
    manualDrive: boolean | null = true,
  ): string[] {
    return calibrationConditions({ state, manualDrive, now: NOW })
      .filter((c) => !c.ok)
      .map((c) => c.id);
  }

  it('is all green for an idle, fixed, fresh mower that drives manually', () => {
    expect(failing(mowerState())).toEqual([]);
  });

  it('turns the matching rows red', () => {
    expect(failing(mowerState({ activity: 'docked' }))).toEqual(['idle']);
    expect(failing(mowerState({ activity: 'charging' }))).toEqual(['idle']);
    expect(failing(mowerState({ rtk_fix: 'float' }))).toEqual(['rtk']);
    expect(failing(mowerState({ position_source: 'gps' }))).toEqual(['rtk']);
    expect(failing(mowerState({ battery_percent: 20 }))).toEqual(['battery']);
    expect(failing(mowerState(), false)).toEqual(['manual']);
    expect(failing(mowerState(), null)).toEqual(['manual']);
    expect(failing(mowerState({ observed_at: new Date(NOW - 61_000).toISOString() }))).toEqual([
      'fresh',
    ]);
    expect(failing(mowerState({ connected: false }))).toEqual(['fresh']);
    expect(failing(mowerState({ drive_calibration: status({}) }))).toEqual(['free']);
    expect(failing(mowerState({ drive_calibration: status({ state: 'done' }) }))).toEqual([]);
    const run = { state: 'spot_mowing' } as CalibConditionState['spot_run'];
    expect(failing(mowerState({ spot_run: run }))).toEqual(['free']);
  });

  it('is all red without a state but the capability', () => {
    expect(failing(null)).toEqual(['idle', 'rtk', 'fresh', 'battery', 'free']);
  });

  it('starts only with green rows, the room confirmed and the permission', () => {
    const green = calibrationConditions({ state: mowerState(), manualDrive: true, now: NOW });
    expect(canStartCalibration(green, true, true)).toBe(true);
    expect(canStartCalibration(green, false, true)).toBe(false);
    expect(canStartCalibration(green, true, false)).toBe(false);
    const red = calibrationConditions({
      state: mowerState({ battery_percent: 5 }),
      manualDrive: true,
      now: NOW,
    });
    expect(canStartCalibration(red, true, true)).toBe(false);
  });
});

describe('driving speed', () => {
  const setting = { min_mps: 0.2, max_mps: 0.6, step_mps: 0.1 };

  it('snaps to the grid within the range', () => {
    expect(snapDriveSpeed(0.34, setting)).toBe(0.3);
    expect(snapDriveSpeed(0.36, setting)).toBe(0.4);
    expect(snapDriveSpeed(0.05, setting)).toBe(0.2);
    expect(snapDriveSpeed(9, setting)).toBe(0.6);
    expect(snapDriveSpeed(Number.NaN, setting)).toBe(0.2);
    // The grid starts at the minimum.
    expect(snapDriveSpeed(0.31, { min_mps: 0.15, max_mps: 0.55, step_mps: 0.1 })).toBe(0.35);
    expect(snapDriveSpeed(0.29, { min_mps: 0.15, max_mps: 0.55, step_mps: 0.1 })).toBe(0.25);
    expect(snapDriveSpeed(0.33, { min_mps: 0.1, max_mps: 1, step_mps: 0.05 })).toBe(0.35);
  });

  it('formats the value, or says unknown', () => {
    expect(formatDriveSpeed(0.4, setting)).toBe('0,40 m/s');
    expect(formatDriveSpeed(0.35, { ...setting, step_mps: 0.05 })).toBe('0,35 m/s');
    expect(formatDriveSpeed(0.125, { ...setting, step_mps: 0.005 })).toBe('0,125 m/s');
    expect(formatDriveSpeed(null)).toBe('unbekannt');
    setLocale('en', { reload: false });
    expect(formatDriveSpeed(0.4, setting)).toBe('0.40 m/s');
    expect(formatDriveSpeed(undefined)).toBe('unknown');
  });

  it('sends a snapped command only for a change', () => {
    expect(driveSpeedCommand(0.47, 0.3, setting)).toEqual({
      kind: 'set_drive_speed',
      speed_mps: 0.5,
    });
    expect(driveSpeedCommand(0.31, 0.3, setting)).toBeNull();
    expect(driveSpeedCommand(0.3, null, setting)).toEqual({
      kind: 'set_drive_speed',
      speed_mps: 0.3,
    });
  });

  it('locks during a spot run or a calibration', () => {
    expect(driveSpeedLocked(null)).toBe(false);
    expect(driveSpeedLocked({ spot_run: null, drive_calibration: null })).toBe(false);
    expect(driveSpeedLocked({ drive_calibration: status({}) })).toBe(true);
    expect(driveSpeedLocked({ drive_calibration: status({ state: 'failed' }) })).toBe(false);
    const run = { state: 'transit' } as CalibConditionState['spot_run'];
    expect(driveSpeedLocked({ spot_run: run })).toBe(true);
  });
});

describe('calibration flow', () => {
  function setup(overrides: Partial<DriveCalibrationDeps> = {}) {
    let live: DriveCalibrationStatus | null = null;
    const deps: DriveCalibrationDeps = {
      command: vi.fn(async () => true),
      save: vi.fn(async (input) => ({ ...input })),
      remove: vi.fn(async () => undefined),
      status: () => live,
      stored: () => null,
      ...overrides,
    };
    const flow = new DriveCalibrationFlow(deps);
    return {
      flow,
      deps,
      setStatus: (next: DriveCalibrationStatus | null) => {
        live = next;
      },
    };
  }

  it('starts only after the room was confirmed, then asks again next time', async () => {
    const { flow, deps } = setup();
    await flow.start();
    expect(deps.command).not.toHaveBeenCalled();
    flow.roomConfirmed = true;
    await flow.start();
    expect(deps.command).toHaveBeenCalledWith({ kind: 'drive_calibrate' });
    expect(flow.roomConfirmed).toBe(false);
    expect(flow.launching(Date.now())).toBe(true);
    expect(flow.launching(Date.now() + 60_000)).toBe(false);
  });

  it('keeps the confirmation when the command did not go out', async () => {
    const { flow } = setup({ command: vi.fn(async () => false) });
    flow.roomConfirmed = true;
    await flow.start();
    expect(flow.roomConfirmed).toBe(true);
    expect(flow.launching(Date.now())).toBe(false);
  });

  it('stops with spot_stop', async () => {
    const { flow, deps } = setup();
    await flow.stop();
    expect(deps.command).toHaveBeenCalledWith({ kind: 'spot_stop' });
    expect(flow.stopping).toBe(false);
  });

  it('saves a finished result and marks it as stored', async () => {
    const { flow, deps, setStatus } = setup();
    expect(await flow.save()).toBe(false);
    setStatus(
      status({ state: 'done', result: { calibration: CALIBRATION, steps: [], forward_m: 1.2 } }),
    );
    // A plain object outside a reactive context: the deriveds read the current deps.
    expect(flow.result?.calibration).toEqual(CALIBRATION);
    expect(flow.resultStored).toBe(false);
    expect(await flow.save()).toBe(true);
    expect(deps.save).toHaveBeenCalledWith(calibrationPayload(CALIBRATION));
    expect(flow.stored).toEqual(calibrationPayload(CALIBRATION));
    expect(flow.resultStored).toBe(true);
  });

  it('shows a failed save as a problem', async () => {
    const { flow, setStatus } = setup({
      save: vi.fn(async () => {
        throw new ApiProblem({ status: 409, title: 'Conflict' });
      }),
    });
    setStatus(
      status({ state: 'done', result: { calibration: CALIBRATION, steps: [], forward_m: 1 } }),
    );
    expect(await flow.save()).toBe(false);
    expect(flow.problem?.status).toBe(409);
    expect(flow.saving).toBe(false);
  });

  it('removes the stored calibration after a confirmation', async () => {
    const { flow, deps } = setup({ stored: () => CALIBRATION });
    expect(flow.stored).toEqual(CALIBRATION);
    expect(await flow.remove()).toBe(false);
    expect(flow.confirmRemove).toBe(true);
    expect(deps.remove).not.toHaveBeenCalled();
    flow.cancelRemove();
    expect(flow.confirmRemove).toBe(false);
    await flow.remove();
    expect(await flow.remove()).toBe(true);
    expect(deps.remove).toHaveBeenCalledOnce();
    expect(flow.stored).toBeNull();
  });
});
