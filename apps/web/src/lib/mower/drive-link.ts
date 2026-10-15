/**
 * The drive link of spot runs and the drive calibration when it is the Bluetooth bridge: the
 * mower's signal at the bridge as a start condition (green, red or neutral while unknown) and
 * the words for a run that ended because the link did not come up or dropped (`link_lost`).
 * Pure, so `drive-link.test.ts` covers it; the spot sheet and the calibration panel use it.
 */
import { m } from '$lib/paraglide/messages';

import { rssiText } from './bridge-link';

/**
 * Weakest signal of the mower at the bridge (dBm) that still carries a drive session. Mirrors
 * `LINK_MIN_RSSI_DBM` of `mowdeck_contracts.v1.links`, which the gateway's start checks apply:
 * at -97 dBm the Bluetooth link did not come up, at -74 dBm it did.
 */
export const LINK_MIN_RSSI_DBM = -90;

/** Link kind of the Bluetooth bridge (`LINK_BLE_BRIDGE`). */
const BRIDGE = 'ble_bridge';

/** The parts of a mower state the signal reads (`MowerState` of Telemetry). */
export interface DriveLinkState {
  connection: string;
  links?: Record<string, { connected: boolean; rssi?: number | null } | undefined> | null;
}

export type SignalLevel = 'ok' | 'weak' | 'unknown';

export interface BridgeSignal {
  /** dBm, or null while the bridge reports none. */
  rssi: number | null;
  level: SignalLevel;
}

/**
 * The mower drives over the bridge: it reports a bridge link (`links.ble_bridge`, also while it
 * is down) or the connection itself is the bridge. `links_supported` alone says only that a
 * bridge could be added (the simulator lists it without one).
 */
export function drivesOverBridge(state: DriveLinkState | null): boolean {
  if (!state) return false;
  return !!state.links?.[BRIDGE] || state.connection === BRIDGE;
}

/** The mower's signal at the bridge, or null when the drive link is not the bridge. */
export function bridgeSignal(state: DriveLinkState | null): BridgeSignal | null {
  if (!state || !drivesOverBridge(state)) return null;
  const reported = state.links?.[BRIDGE]?.rssi;
  const rssi = typeof reported === 'number' ? reported : null;
  if (rssi === null) return { rssi, level: 'unknown' };
  return { rssi, level: rssi >= LINK_MIN_RSSI_DBM ? 'ok' : 'weak' };
}

/** A start condition row for the signal: green or red with the value, neutral while unknown
 * (it never blocks then, the gateway checks again). */
export interface SignalCondition {
  id: 'signal';
  ok: boolean;
  neutral?: boolean;
  rssi?: number | null;
}

export function signalCondition(signal: BridgeSignal): SignalCondition {
  return signal.level === 'unknown'
    ? { id: 'signal', ok: true, neutral: true, rssi: null }
    : { id: 'signal', ok: signal.level === 'ok', rssi: signal.rssi };
}

/** "Bluetooth-Signal ausreichend: −74 dBm (ab −90 dBm)", or the unknown wording. */
export function signalConditionText(condition: Pick<SignalCondition, 'rssi'>): string {
  const value = rssiText(condition.rssi);
  if (value === null) return m.link_signal_unknown();
  return m.link_signal_ok({ rssi: value, min: rssiText(LINK_MIN_RSSI_DBM) ?? '' });
}

/** The line under the row: what to do when it is weak, why it does not block when unknown. */
export function signalConditionHint(condition: SignalCondition): string | null {
  if (condition.neutral) return m.link_signal_unknown_hint();
  return condition.ok ? null : m.link_signal_weak_hint();
}

/** `link_lost` in words: the bridge when the mower drives over it, else the generic text. */
export function linkLostLabel(bridge: boolean, generic: () => string): string {
  return bridge ? m.link_lost_bridge() : generic();
}
