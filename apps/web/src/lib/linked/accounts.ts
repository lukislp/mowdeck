/**
 * Linked vendor cloud accounts (Fleet's `/linked-accounts`, ADR 0034): which devices of an
 * account can be added, the selection and the per-mower drafts of the add-mower flow, the body
 * of the bulk registration, and the account problems mapped to the app's texts. Pure functions,
 * no DOM and no requests.
 */
import type { ApiProblem } from '$lib/api/problem';
import type { components } from '$lib/generated/fleet';
import { problemMessage } from '$lib/i18n';
import { m } from '$lib/paraglide/messages';

export type LinkedAccount = components['schemas']['LinkedAccountOut'];
export type LinkedAccountDetail = components['schemas']['LinkedAccountDetail'];
export type LinkedDevice = components['schemas']['LinkedDeviceOut'];
export type LinkedFailure = components['schemas']['LinkedMowerFailure'];
export type LinkedMowersIn = components['schemas']['LinkedMowersIn'];
export type LinkedMowersOut = components['schemas']['LinkedMowersOut'];

/** At most this many mowers per bulk registration (Fleet's limit; a test pins it to the schema). */
export const BULK_LIMIT = 50;

/**
 * What the flow may do with a device: add it, or show why not (already a mower of this account,
 * a mower of another MowDeck account, or no mower at all such as an RTK base station).
 */
export type DeviceState = 'addable' | 'registered' | 'elsewhere' | 'not_mower';

export function deviceState(device: LinkedDevice): DeviceState {
  if (!device.is_mower) return 'not_mower';
  if (device.registered) return 'registered';
  if (device.registered_elsewhere) return 'elsewhere';
  return 'addable';
}

export type OnlineState = 'online' | 'offline' | 'unknown';

export function onlineState(device: LinkedDevice): OnlineState {
  if (device.online === true) return 'online';
  if (device.online === false) return 'offline';
  return 'unknown';
}

const STATE_ORDER: Record<DeviceState, number> = {
  addable: 0,
  registered: 1,
  elsewhere: 2,
  not_mower: 3,
};

/** Addable mowers first, then the added, the foreign and the non-mowers; by name within. */
export function sortDevices(devices: readonly LinkedDevice[]): LinkedDevice[] {
  return [...devices].sort(
    (a, b) =>
      STATE_ORDER[deviceState(a)] - STATE_ORDER[deviceState(b)] ||
      a.device_name.localeCompare(b.device_name),
  );
}

export function addableNames(devices: readonly LinkedDevice[]): string[] {
  return devices.filter((d) => deviceState(d) === 'addable').map((d) => d.device_name);
}

/** The selection with `name` switched on or off (order kept, no duplicates). */
export function toggleSelection(selected: readonly string[], name: string, on: boolean): string[] {
  const without = selected.filter((n) => n !== name);
  return on ? [...without, name] : without;
}

/** Only the selected names that are still addable (after a re-read or a partial success). */
export function pruneSelection(
  selected: readonly string[],
  devices: readonly LinkedDevice[],
): string[] {
  const addable = new Set(addableNames(devices));
  return selected.filter((n) => addable.has(n));
}

/** A selected mower before it is added: its display name and site (null: the default site). */
export interface MowerDraft {
  name: string;
  siteId: string | null;
}

/** Drafts for the selection: kept ones stay, new ones start with the device name. */
export function syncDrafts(
  selected: readonly string[],
  drafts: Readonly<Record<string, MowerDraft>>,
  siteId: string | null,
): Record<string, MowerDraft> {
  const next: Record<string, MowerDraft> = {};
  for (const name of selected) next[name] = drafts[name] ?? { name, siteId };
  return next;
}

/** The body of `POST /linked-accounts/{id}/mowers` for the selection, in selection order. */
export function bulkBody(
  selected: readonly string[],
  drafts: Readonly<Record<string, MowerDraft>>,
): LinkedMowersIn {
  return {
    mowers: selected.map((device) => {
      const draft = drafts[device];
      const name = draft?.name.trim() ?? '';
      return {
        device_name: device,
        // An empty name defaults to the device name on the server.
        ...(name ? { name } : {}),
        ...(draft?.siteId ? { site_id: draft.siteId } : {}),
      };
    }),
  };
}

/** The message of a device that could not be added. */
export function failureMessage(failure: LinkedFailure): string {
  if (failure.type.endsWith(':mower-already-registered')) return m.linked_failure_registered();
  return failure.detail;
}

/** After the bulk answer: the failures by device and the selection to keep (the failed ones). */
export function bulkOutcome(result: LinkedMowersOut): {
  failures: Record<string, string>;
  keep: string[];
} {
  const failures: Record<string, string> = {};
  for (const failure of result.failed) failures[failure.device_name] = failureMessage(failure);
  return { failures, keep: result.failed.map((f) => f.device_name) };
}

/** Where a problem of linking or re-reading an account belongs. */
export interface AccountIssue {
  /** The input the message belongs to; null for a notice above the form. */
  field: 'email' | 'password' | null;
  message: string;
  /** Fleet's English detail, shown under a notice (503: no key, no gateway, cloud down). */
  detail: string | null;
}

export function accountIssue(problem: ApiProblem): AccountIssue {
  if (problem.slug === 'linked-account-login-failed') {
    return { field: 'password', message: m.linked_login_failed(), detail: null };
  }
  if (problem.status === 409) {
    return { field: 'email', message: m.linked_already_linked(), detail: null };
  }
  if (problem.status === 422) {
    const email = problem.fieldError('email');
    if (email) return { field: 'email', message: m.linked_err_email(), detail: null };
  }
  if (problem.status === 503 || problem.status === 502) {
    return { field: null, message: m.linked_unavailable(), detail: problem.detail };
  }
  return { field: null, message: problemMessage(problem), detail: problem.detail };
}

/** The mowers named by a 409 `linked-account-in-use` (`#/mowers/<id>`, detail the name). */
export function inUseMowers(problem: ApiProblem): { id: string; name: string }[] {
  if (problem.slug !== 'linked-account-in-use') return [];
  return problem.errors.flatMap((error) => {
    const match = /^#?\/mowers\/(.+)$/.exec(error.pointer ?? '');
    return match ? [{ id: match[1]!, name: error.message }] : [];
  });
}

/** A device name as part of an element id (ids stay valid for any vendor name). */
export function idPart(deviceName: string): string {
  return deviceName.replace(/[^A-Za-z0-9_-]/g, '_');
}

/** A cloud mower registered before linked accounts: its login lives in its own options. */
export function isLegacyCloud(mower: {
  connection_kind: string;
  linked_account_id?: string | null;
}): boolean {
  return mower.connection_kind === 'cloud' && !mower.linked_account_id;
}

/** A plain check that `email` looks like an address (the cloud decides the rest). */
export function emailLooksValid(email: string): boolean {
  return /^[^\s@]+@[^\s@]+$/.test(email.trim());
}
