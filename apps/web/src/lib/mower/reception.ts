/**
 * The reception and health panel of the mower page: satellite positioning (`MowerState.gnss`),
 * the mower's own radio signal (`signal`), its start self-check (`self_check`), the rain sensor
 * (`rain_detected`) and the error category (`error_category`). Pure and unit tested
 * (`reception.test.ts`); the words are in `reception-labels.ts`.
 */
import type { components } from '$lib/generated/telemetry';

type Schemas = components['schemas'];
export type GnssStatus = Schemas['GnssStatus'];
export type GnssSolution = Schemas['GnssSolution'];
export type SignalStatus = Schemas['SignalStatus'];
export type NetworkKind = Schemas['NetworkKind'];
export type SelfCheck = Schemas['SelfCheck'];
export type ErrorCategory = Schemas['MowerErrorCategory'];

/** A tone of the instrument panel (`--md-state-*`); null is neutral ink. */
export type Tone = 'mowing' | 'paused' | 'error' | null;

/** Bars of the signal glyph. */
export const SIGNAL_BARS = 4;

/**
 * Lower bounds in dBm for 4, 3, 2 and 1 bars. Wi-Fi after the usual RSSI bands (-55 excellent,
 * -67 good for streaming, -75 usable, -85 the edge); mobile after LTE RSRP bands (-80 excellent,
 * -90 good, -100 fair, -110 the edge). Below the last bound: no bar.
 */
const BOUNDS: Record<'wifi' | 'mobile', readonly [number, number, number, number]> = {
  wifi: [-55, -67, -75, -85],
  mobile: [-80, -90, -100, -110],
};

/** Lit bars (0..4) for a signal in dBm; null when not reported. */
export function signalBars(
  dbm: number | null | undefined,
  radio: 'wifi' | 'mobile',
): number | null {
  if (typeof dbm !== 'number' || !Number.isFinite(dbm)) return null;
  const index = BOUNDS[radio].findIndex((bound) => dbm >= bound);
  return index === -1 ? 0 : SIGNAL_BARS - index;
}

/** Tone of a signal by its bars: one bar or none is a warning, none in use an error. */
export function signalTone(bars: number | null): Tone {
  if (bars === null) return null;
  if (bars === 0) return 'error';
  if (bars === 1) return 'paused';
  return null;
}

/** Tone of the RTK solution: fixed is good, float and single are degraded, none is bad. */
export function gnssTone(solution: GnssSolution | null | undefined): Tone {
  switch (solution) {
    case 'fixed':
      return 'mowing';
    case 'float':
    case 'single':
      return 'paused';
    case 'none':
      return 'error';
    default:
      return null;
  }
}

/**
 * Corrections older than this are stale for an RTK fix (the receiver keeps a fix for a few
 * seconds without new corrections, then drops to float).
 */
export const CORRECTION_STALE_S = 10;

export function correctionTone(ageS: number | null | undefined): Tone {
  if (typeof ageS !== 'number' || !Number.isFinite(ageS)) return null;
  return ageS > CORRECTION_STALE_S ? 'paused' : null;
}

/** Whole seconds below a minute with one decimal under 10 s ("1,0 s" reads as a live link). */
export function correctionAge(ageS: number): { value: number; digits: number; unit: 's' | 'min' } {
  if (ageS < 10) return { value: Math.max(0, ageS), digits: 1, unit: 's' };
  if (ageS < 120) return { value: Math.round(ageS), digits: 0, unit: 's' };
  return { value: Math.round(ageS / 60), digits: 0, unit: 'min' };
}

/** Whether the panel has anything to show for this state. */
export function hasReception(state: {
  gnss?: GnssStatus | null;
  signal?: SignalStatus | null;
  self_check?: SelfCheck | null;
  rain_detected?: boolean | null;
}): boolean {
  return (
    !!state.gnss || !!state.signal || !!state.self_check || typeof state.rain_detected === 'boolean'
  );
}

/** A key the app has no words for, readable: `weak_rtk_signal` -> "Weak rtk signal". */
export function readableKey(key: string): string {
  const words = key.replace(/[_-]+/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key;
}
