import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { ModeBase } from '@matter/main/clusters/mode-base';
import { PowerSource } from '@matter/main/clusters/power-source';
import { RvcOperationalState } from '@matter/main/clusters/rvc-operational-state';
import { RvcRunMode } from '@matter/main/clusters/rvc-run-mode';
import { RoboticVacuumCleanerDevice } from '@matter/main/devices/robotic-vacuum-cleaner';

import type { Activity, MowerSnapshot } from '../src/model.ts';
import {
  ChargeLevel,
  ChargeState,
  ErrorId,
  ModeStatus,
  OpState,
  RVC_DEVICE_TYPE,
  RunMode,
  RunModeTag,
  batteryView,
  commandErrorId,
  modeChangeStatus,
  operationalView,
  runModeOf,
} from '../src/profiles/rvc-mapping.ts';

const snap = (activity: Activity, batteryPercent: number | null = 50, charging = false) =>
  ({ activity, batteryPercent, charging }) satisfies MowerSnapshot;

describe('the constants equal matter.js', () => {
  it('device type, states, errors, tags and statuses', () => {
    assert.equal(RVC_DEVICE_TYPE, RoboticVacuumCleanerDevice.deviceType);
    const S = RvcOperationalState.OperationalState;
    assert.deepEqual(OpState, {
      Stopped: S.Stopped,
      Running: S.Running,
      Paused: S.Paused,
      Error: S.Error,
      SeekingCharger: S.SeekingCharger,
      Charging: S.Charging,
      Docked: S.Docked,
    });
    const E = RvcOperationalState.ErrorState;
    assert.equal(ErrorId.NoError, E.NoError);
    assert.equal(ErrorId.UnableToStartOrResume, E.UnableToStartOrResume);
    assert.equal(ErrorId.UnableToCompleteOperation, E.UnableToCompleteOperation);
    assert.equal(ErrorId.CommandInvalidInState, E.CommandInvalidInState);
    assert.equal(ErrorId.Stuck, E.Stuck);
    assert.equal(RunModeTag.Idle, RvcRunMode.ModeTag.Idle);
    assert.equal(RunModeTag.Cleaning, RvcRunMode.ModeTag.Cleaning);
    assert.equal(RunModeTag.Auto, ModeBase.ModeTag.Auto);
    assert.equal(ModeStatus.InvalidInMode, ModeBase.ModeChangeStatus.InvalidInMode);
    assert.equal(ModeStatus.GenericFailure, ModeBase.ModeChangeStatus.GenericFailure);
    assert.equal(ChargeLevel.Critical, PowerSource.BatChargeLevel.Critical);
    assert.equal(ChargeState.IsAtFullCharge, PowerSource.BatChargeState.IsAtFullCharge);
    assert.equal(ChargeState.IsNotCharging, PowerSource.BatChargeState.IsNotCharging);
  });
});

describe('operationalView', () => {
  const cases: [MowerSnapshot, number, number][] = [
    [snap('mowing'), OpState.Running, ErrorId.NoError],
    [snap('paused'), OpState.Paused, ErrorId.NoError],
    [snap('returning'), OpState.SeekingCharger, ErrorId.NoError],
    [snap('charging', 40, true), OpState.Charging, ErrorId.NoError],
    [snap('docked'), OpState.Docked, ErrorId.NoError],
    [snap('docked', 40, true), OpState.Charging, ErrorId.NoError],
    [snap('updating'), OpState.Docked, ErrorId.NoError],
    [snap('idle'), OpState.Stopped, ErrorId.NoError],
    [snap('error'), OpState.Error, ErrorId.Stuck],
    [snap('offline'), OpState.Error, ErrorId.UnableToStartOrResume],
    [snap('unknown', null), OpState.Error, ErrorId.UnableToStartOrResume],
  ];
  for (const [snapshot, state, error] of cases) {
    it(`${snapshot.activity}${snapshot.charging ? ' (charging)' : ''}`, () => {
      assert.deepEqual(operationalView(snapshot), { operationalState: state, errorStateId: error });
    });
  }
});

describe('runModeOf', () => {
  it('is Mowing while a job runs or waits, Idle otherwise', () => {
    assert.equal(runModeOf(snap('mowing')), RunMode.Mowing);
    assert.equal(runModeOf(snap('paused')), RunMode.Mowing);
    for (const activity of ['returning', 'docked', 'charging', 'idle', 'error'] as Activity[]) {
      assert.equal(runModeOf(snap(activity)), RunMode.Idle, activity);
    }
  });
});

describe('batteryView', () => {
  it('counts half percent and grades the level', () => {
    assert.deepEqual(batteryView(snap('mowing', 80)), {
      batPercentRemaining: 160,
      batChargeLevel: ChargeLevel.Ok,
      batChargeState: ChargeState.IsNotCharging,
    });
    assert.equal(batteryView(snap('mowing', 19)).batChargeLevel, ChargeLevel.Warning);
    assert.equal(batteryView(snap('mowing', 9)).batChargeLevel, ChargeLevel.Critical);
  });

  it('knows charging, full and unknown', () => {
    assert.equal(batteryView(snap('charging', 60, true)).batChargeState, ChargeState.IsCharging);
    assert.equal(batteryView(snap('docked', 100)).batChargeState, ChargeState.IsAtFullCharge);
    assert.deepEqual(batteryView(snap('unknown', null)), {
      batPercentRemaining: null,
      batChargeLevel: ChargeLevel.Ok,
      batChargeState: ChargeState.Unknown,
    });
  });
});

describe('command answers', () => {
  it('maps refusals and failures', () => {
    assert.equal(commandErrorId('invalid_in_state'), ErrorId.CommandInvalidInState);
    assert.equal(commandErrorId('offline'), ErrorId.UnableToStartOrResume);
    assert.equal(commandErrorId('failed'), ErrorId.UnableToCompleteOperation);
    assert.equal(modeChangeStatus('failed'), ModeStatus.GenericFailure);
    assert.equal(modeChangeStatus('error'), ModeStatus.InvalidInMode);
  });
});
