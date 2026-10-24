/**
 * Pure parts of the Bluetooth bridge sheet: the form checks, the order and preselection of the
 * scanned mowers, and the texts of a scan that gave no result (design 3). No DOM, no runes, so
 * `bridge-link.test.ts` covers them with the flow.
 */
import type { ApiProblem } from '#lib/api/problem.js';
import type { BridgeScanError, ScannedDevice } from '#lib/fleet/links.js';
import { problemMessage } from '#lib/i18n.js';
import { m } from '#lib/paraglide/messages.js';

export const DEFAULT_PORT = 6053;

export interface BridgeForm {
  host: string;
  /** The port as typed; empty means the default. */
  port: string;
  key: string;
}

export type BridgeFormField = 'host' | 'port' | 'key';
export type BridgeFormIssue = 'required' | 'range';

/** The port number of the form, null when it is not a whole number in 1..65535. */
export function parsePort(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === '') return DEFAULT_PORT;
  if (!/^\d{1,5}$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return value >= 1 && value <= 65535 ? value : null;
}

/** Field errors of the form; `keyStored` when a saved link keeps its key (then it may stay empty). */
export function formIssues(
  form: BridgeForm,
  keyStored = false,
): Partial<Record<BridgeFormField, BridgeFormIssue>> {
  const issues: Partial<Record<BridgeFormField, BridgeFormIssue>> = {};
  if (form.host.trim() === '') issues.host = 'required';
  if (parsePort(form.port) === null) issues.port = 'range';
  if (form.key.trim() === '' && !keyStored) issues.key = 'required';
  return issues;
}

export function issueText(field: BridgeFormField, issue: BridgeFormIssue): string {
  if (issue === 'range') return m.conn_error_range({ min: '1', max: '65535' });
  return m.conn_error_required();
}

function sameName(a: string, b: string | null): boolean {
  return b !== null && a.trim().toLowerCase() === b.trim().toLowerCase();
}

/** True when `device` is the mower the registration names (its cloud device name). */
export function isExpected(device: ScannedDevice, expectedName: string | null): boolean {
  return sameName(device.name, expectedName);
}

/** The expected mower first, then the strongest signal first; duplicates by address dropped. */
export function sortDevices(
  devices: readonly ScannedDevice[],
  expectedName: string | null,
): ScannedDevice[] {
  const seen = new Set<string>();
  const unique = devices.filter((d) => {
    const key = d.address.toUpperCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return unique.sort((a, b) => {
    const ea = isExpected(a, expectedName) ? 1 : 0;
    const eb = isExpected(b, expectedName) ? 1 : 0;
    return eb - ea || b.rssi - a.rssi;
  });
}

/**
 * The address to preselect: the expected mower when the bridge saw it; a single other mower
 * when it is the only one; otherwise nothing (the user must choose).
 */
export function preselect(
  devices: readonly ScannedDevice[],
  expectedName: string | null,
): string | null {
  const expected = devices.find((d) => isExpected(d, expectedName));
  if (expected) return expected.address;
  return devices.length === 1 ? (devices[0]?.address ?? null) : null;
}

/** "−62 dBm" with a real minus sign. */
export function rssiText(rssi: number | null | undefined): string | null {
  if (rssi === null || rssi === undefined) return null;
  const sign = rssi < 0 ? '−' : '';
  return `${sign}${Math.abs(Math.round(rssi))} dBm`;
}

/** The text of a scan error, per design 3 (never echoes the key). */
export function scanErrorText(error: BridgeScanError, host: string, port: number): string {
  switch (error.code) {
    case 'unreachable':
      return m.bridge_error_unreachable({ host, port: String(port) });
    case 'auth_failed':
      return m.bridge_error_auth_failed();
    case 'timeout':
      return m.bridge_error_timeout();
    case 'busy':
      return m.bridge_error_busy();
    case 'already_connected':
      return error.held_by
        ? m.bridge_error_already_connected({ name: error.held_by })
        : m.bridge_error_already_connected_unnamed();
    case 'unsupported':
      return m.bridge_error_unsupported();
    case 'not_found':
      return m.bridge_error_not_found();
    case 'forbidden':
      return m.problem_forbidden();
    default:
      return m.bridge_error_other({ code: String(error.code) });
  }
}

/**
 * The text of a failed request: Fleet answers 409 `bridge-scan-unsupported` (the connection
 * cannot scan) or `link-not-applicable` (the kind carries no bridge) and 503 when no gateway
 * answered within its deadline; anything else is the ordinary problem text.
 */
export function scanProblemText(problem: ApiProblem): string {
  if (problem.slug === 'bridge-scan-unsupported' || problem.slug === 'link-not-applicable') {
    return m.bridge_error_unsupported();
  }
  if (problem.status === 503 || problem.status === 504) return m.bridge_error_gateway();
  return problemMessage(problem);
}

/** Why the bridge firmware counts as outdated, in words. */
export function outdatedText(reason: string | null | undefined): string {
  switch (reason) {
    case 'esphome_version':
      return m.bridge_outdated_esphome_version();
    case 'project_version':
      return m.bridge_outdated_project_version();
    case 'no_project':
      return m.bridge_outdated_no_project();
    default:
      return m.bridge_outdated_other({ reason: reason ?? '?' });
  }
}
