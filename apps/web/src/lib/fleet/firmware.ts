/**
 * Mower firmware (ADR 0038): the check through Fleet (`POST /mowers/{id}/firmware/check`, which
 * answers the fresh `FirmwareInfo`; a connection that cannot ask is a 409
 * `firmware-check-unsupported`, a mower or vendor that does not answer a 503) and the pure rules
 * of the "Update installieren" button. The gateway decides for real (docked and charging,
 * battery, no job, no drive session, not already installing); this only avoids offering the
 * obvious refusal and names the unmet condition.
 */
import { fleetApi, unwrap } from '$lib/api/client';
import type { ApiProblem } from '$lib/api/problem';
import type { components } from '$lib/generated/telemetry';
import { problemMessage } from '$lib/i18n';
import { m } from '$lib/paraglide/messages';

export type FirmwareInfo = components['schemas']['FirmwareInfo'];
export type FirmwareUpdateState = FirmwareInfo['update_state'];

export function checkFirmware(mowerId: string): Promise<FirmwareInfo> {
  return unwrap(
    fleetApi.POST('/api/v1/fleet/mowers/{mower_id}/firmware/check', {
      params: { path: { mower_id: mowerId } },
    }),
  );
}

/** The text of a failed check: the connection cannot ask, nobody answered, or the problem. */
export function firmwareCheckProblemText(problem: ApiProblem): string {
  if (problem.slug === 'firmware-check-unsupported') return m.firmware_unsupported();
  if (problem.status === 503 || problem.status === 504) {
    return m.firmware_check_error_unavailable();
  }
  return problemMessage(problem);
}

/** The lowest battery the gateway installs with (design 2, firmware safety). */
export const UPDATE_MIN_BATTERY = 50;

/** Why "Update installieren" is disabled; null when the update may be sent. */
export type UpdateBlocker =
  | 'no_permission'
  | 'no_update'
  | 'installing'
  | 'offline'
  | 'job'
  | 'not_docked'
  | 'not_charging'
  | 'battery';

export interface UpdateConditions {
  info: FirmwareInfo | null;
  activity: string | null;
  charging: boolean;
  batteryPercent: number | null;
  /** No live link to the mower (ADR 0040) or no state: treated as offline. */
  stale: boolean;
  /** The user may control the mower (`can_control`). */
  allowed: boolean;
}

const RESTING: ReadonlySet<string> = new Set(['docked', 'charging']);
const JOB: ReadonlySet<string> = new Set(['mowing', 'paused', 'returning']);

/**
 * The first unmet precondition, in the order the user can act on it: the right, a version,
 * no update running, the mower reachable, no job, docked, charging, battery.
 */
export function updateBlocker(c: UpdateConditions): UpdateBlocker | null {
  if (!c.allowed) return 'no_permission';
  if (!c.info?.available || c.info.update_state === 'idle') return 'no_update';
  if (c.info.update_state === 'installing' || c.activity === 'updating') return 'installing';
  if (c.activity === null || c.activity === 'offline' || c.stale) return 'offline';
  if (JOB.has(c.activity)) return 'job';
  if (!RESTING.has(c.activity)) return 'not_docked';
  if (!c.charging) return 'not_charging';
  if (c.batteryPercent === null || c.batteryPercent < UPDATE_MIN_BATTERY) return 'battery';
  return null;
}

/** The percent to draw while installing (0 when the mower reports none yet). */
export function installProgress(info: FirmwareInfo | null): number | null {
  if (info?.update_state !== 'installing') return null;
  const value = info.progress ?? 0;
  return Math.max(0, Math.min(100, value));
}

/** True when the panel may offer "Update installieren" at all (a version is offered). */
export function updateOffered(info: FirmwareInfo | null): boolean {
  return !!info?.available && (info.update_state === 'available' || info.update_state === 'failed');
}
