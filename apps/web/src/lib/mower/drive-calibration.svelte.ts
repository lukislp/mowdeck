/**
 * The drive calibration block's flow, without any markup (ADR 0042): the free-room confirmation,
 * the start (`drive_calibrate`), the STOP (`spot_stop`, which also ends a calibration), storing a
 * result in Fleet and removing the stored one. The live progress comes from
 * `MowerState.drive_calibration`; the page passes it in through `status()`. The page constructs
 * the flow with the real calls; `drive-calibration.test.ts` drives it with fakes.
 */
import { asProblem, type ApiProblem } from '$lib/api/problem';

import {
  calibrationPayload,
  isCalibrationActive,
  isResultStored,
  type DriveCalibration,
  type DriveCalibrationIn,
  type DriveCalibrationResult,
  type DriveCalibrationStatus,
} from './drive-calibration';

export interface DriveCalibrationDeps {
  /** Send a mower command; false when it did not go out (the sender toasts the reason). */
  command(command: { kind: 'drive_calibrate' } | { kind: 'spot_stop' }): Promise<boolean>;
  /** `PUT /mowers/{id}/drive-calibration`. */
  save(input: DriveCalibrationIn): Promise<DriveCalibration>;
  /** `DELETE /mowers/{id}/drive-calibration`. */
  remove(): Promise<void>;
  /** The live calibration status of the mower (`MowerState.drive_calibration`). */
  status(): DriveCalibrationStatus | null;
  /** The calibration Fleet stores (`MowerOut.drive_calibration`) when the flow starts. */
  stored(): DriveCalibration | null;
}

/** After a start, the block says "starting" this long at most until the status shows up. */
export const CALIB_LAUNCH_WAIT_MS = 20_000;

export class DriveCalibrationFlow {
  /** "About 2 m of free lawn ahead" was confirmed. */
  roomConfirmed = $state(false);
  starting = $state(false);
  stopping = $state(false);
  saving = $state(false);
  removing = $state(false);
  confirmRemove = $state(false);
  /** The last failed save or remove, shown next to its button. */
  problem = $state<ApiProblem | null>(null);
  /** The stored calibration, as Fleet answered last. */
  stored = $state<DriveCalibration | null>(null);
  /** The run id that was current when the start went out (to tell the new run from the old). */
  #before: string | null = null;
  #launchedAt = $state<number | null>(null);

  constructor(private readonly deps: DriveCalibrationDeps) {
    this.stored = deps.stored();
  }

  // Getters, not deriveds: the status comes from the caller's reactive state and is read live.
  get status(): DriveCalibrationStatus | null {
    return this.deps.status();
  }

  get active(): boolean {
    return isCalibrationActive(this.status);
  }

  /** The finished result, while the status still holds it. */
  get result(): DriveCalibrationResult | null {
    const status = this.status;
    return status?.state === 'done' ? (status.result ?? null) : null;
  }

  get resultStored(): boolean {
    return isResultStored(this.result?.calibration, this.stored);
  }

  /** A start went out and its run has not shown up yet (within the launch wait). */
  launching(now: number): boolean {
    if (this.#launchedAt === null) return false;
    const run = this.status;
    if (run && run.run_id !== this.#before) return false;
    return now - this.#launchedAt <= CALIB_LAUNCH_WAIT_MS;
  }

  /** Take Fleet's mower record again (after a reload). */
  syncStored(stored: DriveCalibration | null): void {
    this.stored = stored;
  }

  /** Start the calibration; the caller checks the conditions first. */
  async start(): Promise<void> {
    if (this.starting || this.active || !this.roomConfirmed) return;
    this.starting = true;
    this.#before = this.status?.run_id ?? null;
    try {
      const sent = await this.deps.command({ kind: 'drive_calibrate' });
      if (sent) {
        this.#launchedAt = Date.now();
        // The room is checked again before the next run.
        this.roomConfirmed = false;
      }
    } finally {
      this.starting = false;
    }
  }

  /** STOP: the controlled stop also ends a calibration. */
  async stop(): Promise<void> {
    if (this.stopping) return;
    this.stopping = true;
    try {
      await this.deps.command({ kind: 'spot_stop' });
      this.#launchedAt = null;
    } finally {
      this.stopping = false;
    }
  }

  /** Store the finished result in Fleet. */
  async save(): Promise<boolean> {
    const result = this.result;
    if (!result || this.saving) return false;
    this.saving = true;
    this.problem = null;
    try {
      this.stored = await this.deps.save(calibrationPayload(result.calibration));
      return true;
    } catch (error) {
      this.problem = asProblem(error);
      return false;
    } finally {
      this.saving = false;
    }
  }

  /** "Kalibrierung löschen": the first call asks, the second removes. */
  async remove(): Promise<boolean> {
    if (!this.stored || this.removing) return false;
    if (!this.confirmRemove) {
      this.confirmRemove = true;
      return false;
    }
    this.removing = true;
    this.problem = null;
    try {
      await this.deps.remove();
      this.stored = null;
      this.confirmRemove = false;
      return true;
    } catch (error) {
      this.problem = asProblem(error);
      return false;
    } finally {
      this.removing = false;
    }
  }

  cancelRemove(): void {
    this.confirmRemove = false;
  }
}
