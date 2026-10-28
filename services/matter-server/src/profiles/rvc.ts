/**
 * Profile "rvc": the mower as a Matter Robotic Vacuum Cleaner (device type 0x74) with RVC Run
 * Mode (start/stop), RVC Operational State (pause, resume, go home, state) and Power Source
 * (battery). Apple Home and Google Home render it as a robot vacuum: start/stop, pause, return to
 * the dock, status and battery. Matter defines no mower type up to 1.5 (ADR 0063).
 *
 * Every command is forwarded to the profile's {@link IntentHandler}; only once MowDeck accepted
 * it does the endpoint show the new mode. The real state follows from the next poll.
 */
import { Endpoint } from '@matter/main';
import { PowerSourceServer } from '@matter/main/behaviors/power-source';
import { RvcOperationalStateServer } from '@matter/main/behaviors/rvc-operational-state';
import { RvcRunModeServer } from '@matter/main/behaviors/rvc-run-mode';
import type { ModeBase } from '@matter/main/clusters/mode-base';
import { PowerSource } from '@matter/main/clusters/power-source';
import type { RvcOperationalState } from '@matter/main/clusters/rvc-operational-state';
import { RoboticVacuumCleanerDevice } from '@matter/main/devices/robotic-vacuum-cleaner';

import type { Intent, MowerSnapshot } from '../model.ts';

import type { DeviceProfile, IntentHandler, IntentResult, ProfileLabels } from './profile.ts';
import {
  ErrorId,
  ModeStatus,
  OpState,
  RVC_DEVICE_TYPE,
  RunMode,
  RunModeTag,
  SourceStatus,
  batteryView,
  commandErrorId,
  modeChangeStatus,
  operationalView,
  runModeOf,
} from './rvc-mapping.ts';

/** The handler of each endpoint this profile created (behaviors only see their endpoint). */
const handlers = new WeakMap<Endpoint, IntentHandler>();

async function dispatch(endpoint: Endpoint, intent: Intent): Promise<IntentResult> {
  const handler = handlers.get(endpoint);
  if (!handler) return { ok: false, reason: 'failed' };
  try {
    return await handler(intent);
  } catch {
    return { ok: false, reason: 'failed' };
  }
}

/** Start (Mowing) and stop (Idle) arrive as mode changes. */
export class MowerRunModeServer extends RvcRunModeServer {
  override async changeToMode({
    newMode,
  }: ModeBase.ChangeToModeRequest): Promise<ModeBase.ChangeToModeResponse> {
    if (!this.state.supportedModes.some((mode) => mode.mode === newMode)) {
      return { status: ModeStatus.UnsupportedMode, statusText: 'Unsupported mode' };
    }
    const result = await dispatch(
      this.endpoint,
      newMode === RunMode.Mowing ? 'start' : 'stop',
    );
    if (!result.ok) {
      return { status: modeChangeStatus(result.reason), statusText: result.reason };
    }
    this.state.currentMode = newMode;
    return { status: ModeStatus.Success, statusText: '' };
  }
}

type OperationalResponse = RvcOperationalState.OperationalCommandResponse;

function answer(result: IntentResult): OperationalResponse {
  return {
    commandResponseState: {
      errorStateId: result.ok ? ErrorId.NoError : commandErrorId(result.reason),
    },
  };
}

/** Pause, resume and go home. */
export class MowerOperationalStateServer extends RvcOperationalStateServer {
  override async pause(): Promise<OperationalResponse> {
    const result = await dispatch(this.endpoint, 'pause');
    if (result.ok) this.state.operationalState = OpState.Paused;
    return answer(result);
  }

  override async resume(): Promise<OperationalResponse> {
    const result = await dispatch(this.endpoint, 'resume');
    if (result.ok) this.state.operationalState = OpState.Running;
    return answer(result);
  }

  override async goHome(): Promise<OperationalResponse> {
    const result = await dispatch(this.endpoint, 'dock');
    if (result.ok && this.state.operationalState !== OpState.Docked) {
      this.state.operationalState = OpState.SeekingCharger;
    }
    return answer(result);
  }
}

const MowerPowerSourceServer = PowerSourceServer.with(
  PowerSource.Feature.Battery,
  PowerSource.Feature.Rechargeable,
);

const MowerDevice = RoboticVacuumCleanerDevice.with(
  MowerRunModeServer,
  MowerOperationalStateServer,
  MowerPowerSourceServer,
);

function operational(snapshot: MowerSnapshot) {
  const view = operationalView(snapshot);
  return {
    operationalState: view.operationalState,
    operationalError: { errorStateId: view.errorStateId },
  };
}

function power(snapshot: MowerSnapshot) {
  const view = batteryView(snapshot);
  return {
    batPercentRemaining: view.batPercentRemaining,
    batChargeLevel: view.batChargeLevel,
    batChargeState: view.batChargeState,
  };
}

export const rvcProfile: DeviceProfile = {
  name: 'rvc',
  deviceTypeId: RVC_DEVICE_TYPE,

  createEndpoint(snapshot: MowerSnapshot, labels: ProfileLabels, onIntent: IntentHandler) {
    const endpoint = new Endpoint(MowerDevice, {
      id: 'mower',
      rvcRunMode: {
        supportedModes: [
          { label: labels.idle, mode: RunMode.Idle, modeTags: [{ value: RunModeTag.Idle }] },
          {
            label: labels.mowing,
            mode: RunMode.Mowing,
            modeTags: [{ value: RunModeTag.Cleaning }, { value: RunModeTag.Auto }],
          },
        ],
        currentMode: runModeOf(snapshot),
      },
      rvcOperationalState: {
        operationalStateList: Object.values(OpState).map((id) => ({ operationalStateId: id })),
        phaseList: null,
        currentPhase: null,
        ...operational(snapshot),
      },
      powerSource: {
        status: SourceStatus.Active,
        order: 0,
        description: labels.battery,
        batReplacementNeeded: false,
        batReplaceability: PowerSource.BatReplaceability.NotReplaceable,
        batFunctionalWhileCharging: false,
        ...power(snapshot),
      },
    });
    handlers.set(endpoint, onIntent);
    return endpoint;
  },

  async apply(endpoint: Endpoint, snapshot: MowerSnapshot) {
    await endpoint.set({
      rvcRunMode: { currentMode: runModeOf(snapshot) },
      rvcOperationalState: operational(snapshot),
      powerSource: power(snapshot),
    } as never);
  },
};
