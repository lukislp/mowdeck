/**
 * Maintenance actions of a mower (ADR 0048): `leave_dock`, `sync_map`, `resync_rtk`,
 * `relocate_dock` and `restart_mower`, each a command without parameters. The connection lists
 * the ones it can send (`MowerCapabilities.maintenance`); the page offers only those, and says
 * why one cannot run now. The rules mirror `mowdeck_control.safety.maintenance_problem` and
 * Fleet's rights, so a refusal is explained before it happens; the server checks them again.
 * Pure and unit tested (`maintenance.test.ts`).
 */

/** The actions in the order the page lists them: harmless first, the restart last. */
export const MAINTENANCE_ORDER = [
  'leave_dock',
  'sync_map',
  'resync_rtk',
  'relocate_dock',
  'restart_mower',
] as const;

export type MaintenanceAction = (typeof MAINTENANCE_ORDER)[number];

/** The declared actions of the capabilities, in page order (unknown names are left out). */
export function offeredActions(capabilities: object | null | undefined): MaintenanceAction[] {
  const raw = (capabilities as { maintenance?: unknown } | null | undefined)?.maintenance;
  if (!Array.isArray(raw)) return [];
  return MAINTENANCE_ORDER.filter((action) => raw.includes(action));
}

/** Leaving the dock is what a start does first (`can_control`); the rest change the device. */
export function neededRight(action: MaintenanceAction): 'can_control' | 'can_manage_mowers' {
  return action === 'leave_dock' ? 'can_control' : 'can_manage_mowers';
}

export type MaintenanceBlocker =
  | 'no_permission'
  | 'plan'
  | 'offline'
  | 'busy'
  | 'pending'
  | 'error'
  | 'not_at_rest'
  | 'not_docked'
  | 'job'
  | 'drive_session';

/** What the rules read of the mower state (`MowerState`, structurally). */
export interface MaintenanceState {
  activity: string;
  job?: unknown;
  firmware_info?: { update_state?: string | null } | null;
  spot_run?: { state: string } | null;
  drive_calibration?: { state: string } | null;
}

export interface MaintenanceInput {
  state: MaintenanceState | null;
  /** No live link to the mower. */
  offline: boolean;
  canControl: boolean;
  canManage: boolean;
  /** The plan's features; null while unknown or without Entitlements. */
  features: readonly string[] | null;
  /** The plan is in grace or expired. */
  restricted: boolean;
  /** This browser drives the mower manually right now. */
  driveSession: boolean;
  /** Kinds of this mower's commands still on their way. */
  pending: ReadonlySet<string>;
}

const AT_REST = new Set(['docked', 'charging', 'idle']);
const IN_DOCK = new Set(['docked', 'charging']);
const SESSION_BLOCKED = new Set<MaintenanceAction>(['restart_mower', 'relocate_dock', 'sync_map']);
/** Commands the mower answers without confirmation; one at a time (the controller's rule). */
const CONFIRMATION_BLOCKED = new Set<MaintenanceAction>([
  'restart_mower',
  'relocate_dock',
  'leave_dock',
]);
const SPOT_DONE = new Set(['done', 'aborted', 'failed']);
const CALIBRATION_DONE = new Set(['done', 'aborted', 'failed']);

/** Why `action` cannot run now, or null when it may; the first reason in the order below. */
export function maintenanceBlocker(
  action: MaintenanceAction,
  input: MaintenanceInput,
): MaintenanceBlocker | null {
  const allowed = neededRight(action) === 'can_control' ? input.canControl : input.canManage;
  if (!allowed) return 'no_permission';
  if (input.restricted) return 'plan';
  if (input.features && !input.features.includes('*') && !input.features.includes('control'))
    return 'plan';
  const state = input.state;
  if (!state || input.offline || state.activity === 'offline') return 'offline';
  if (input.pending.has(action)) return 'pending';
  // A firmware update, a spot run or a calibration refuses every command.
  if (
    state.activity === 'updating' ||
    state.firmware_info?.update_state === 'installing' ||
    (state.spot_run && !SPOT_DONE.has(state.spot_run.state)) ||
    (state.drive_calibration && !CALIBRATION_DONE.has(state.drive_calibration.state))
  )
    return 'busy';
  if (CONFIRMATION_BLOCKED.has(action)) {
    if (state.activity === 'error') return 'error';
    if (action === 'restart_mower' && !AT_REST.has(state.activity)) return 'not_at_rest';
    if (action !== 'restart_mower' && !IN_DOCK.has(state.activity)) return 'not_docked';
  }
  if (action === 'relocate_dock' && state.job != null) return 'job';
  if (input.driveSession && SESSION_BLOCKED.has(action)) return 'drive_session';
  if (
    CONFIRMATION_BLOCKED.has(action) &&
    [...CONFIRMATION_BLOCKED].some((other) => other !== action && input.pending.has(other))
  )
    return 'pending';
  if (action === 'restart_mower' && input.pending.has('sync_map')) return 'pending';
  return null;
}

/** The last result of an action as the page shows it. */
export type ResultTone = 'pending' | 'ok' | 'error';

/**
 * `accepted` is the final answer of a maintenance command the mower does not confirm (restart,
 * dock relocation, leaving the dock) and of a map sync or RTK resync that timed out: it reached
 * the mower. `executed` means the mower answered.
 */
export function resultTone(status: string): ResultTone {
  if (status === 'executed' || status === 'accepted') return 'ok';
  if (status === 'rejected' || status === 'failed' || status === 'expired') return 'error';
  return 'pending';
}
