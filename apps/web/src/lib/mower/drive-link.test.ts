import { beforeEach, describe, expect, it } from 'vitest';

import { setLocale } from '#lib/paraglide/runtime.js';

import {
  LINK_MIN_RSSI_DBM,
  bridgeSignal,
  drivesOverBridge,
  linkLostLabel,
  signalCondition,
  signalConditionHint,
  signalConditionText,
} from './drive-link';

const generic = (): string => 'generic';

function bridge(rssi: number | null | undefined, connected = true) {
  return { connection: 'cloud', links: { ble_bridge: { connected, rssi } } };
}

beforeEach(() => {
  setLocale('de', { reload: false });
});

describe('drive link over the bridge', () => {
  it('mirrors the gateway threshold', () => {
    expect(LINK_MIN_RSSI_DBM).toBe(-90);
  });

  it('knows when the bridge carries the driving', () => {
    expect(drivesOverBridge(bridge(-70))).toBe(true);
    expect(drivesOverBridge(bridge(null, false))).toBe(true); // down, still the drive link
    expect(drivesOverBridge({ connection: 'ble_bridge', links: null })).toBe(true);
    // a connection that only could open a bridge (the simulator lists one) drives without it
    expect(drivesOverBridge({ connection: 'simulated', links: {} })).toBe(false);
    expect(drivesOverBridge(null)).toBe(false);
  });

  it('rates the signal: green from -90 dBm, red below, unknown without a value', () => {
    expect(bridgeSignal(bridge(-74))).toEqual({ rssi: -74, level: 'ok' });
    expect(bridgeSignal(bridge(-90))).toEqual({ rssi: -90, level: 'ok' });
    expect(bridgeSignal(bridge(-91))).toEqual({ rssi: -91, level: 'weak' });
    expect(bridgeSignal(bridge(-97))).toEqual({ rssi: -97, level: 'weak' });
    expect(bridgeSignal(bridge(null))).toEqual({ rssi: null, level: 'unknown' });
    expect(bridgeSignal(bridge(undefined))).toEqual({ rssi: null, level: 'unknown' });
    expect(bridgeSignal({ connection: 'ble_bridge', links: null })?.level).toBe('unknown');
    expect(bridgeSignal({ connection: 'simulated', links: null })).toBeNull();
  });

  it('makes a row: green or red with the value, neutral and not blocking while unknown', () => {
    expect(signalCondition({ rssi: -74, level: 'ok' })).toEqual({
      id: 'signal',
      ok: true,
      rssi: -74,
    });
    expect(signalCondition({ rssi: -97, level: 'weak' })).toEqual({
      id: 'signal',
      ok: false,
      rssi: -97,
    });
    expect(signalCondition({ rssi: null, level: 'unknown' })).toEqual({
      id: 'signal',
      ok: true,
      neutral: true,
      rssi: null,
    });
  });

  it('words the row and its hint in both languages', () => {
    const weak = signalCondition({ rssi: -97, level: 'weak' });
    const good = signalCondition({ rssi: -74, level: 'ok' });
    const unknown = signalCondition({ rssi: null, level: 'unknown' });
    expect(signalConditionText(good)).toBe('Bluetooth-Signal ausreichend: −74 dBm (ab −90 dBm)');
    expect(signalConditionText(weak)).toBe('Bluetooth-Signal ausreichend: −97 dBm (ab −90 dBm)');
    expect(signalConditionText(unknown)).toBe('Bluetooth-Signal ausreichend: noch unbekannt');
    expect(signalConditionHint(good)).toBeNull();
    expect(signalConditionHint(weak)).toContain('näher an den Mäher');
    expect(signalConditionHint(unknown)).toContain('der Start bleibt möglich');
    setLocale('en', { reload: false });
    expect(signalConditionText(good)).toBe('Bluetooth signal sufficient: −74 dBm (from −90 dBm)');
    expect(signalConditionText(unknown)).toBe('Bluetooth signal sufficient: not known yet');
    expect(signalConditionHint(weak)).toContain('closer to the mower');
  });

  it('names the bridge for a lost drive link only when the mower drives over it', () => {
    expect(linkLostLabel(true, generic)).toBe(
      'Bluetooth-Bridge nicht erreichbar oder zu schwaches Signal',
    );
    expect(linkLostLabel(false, generic)).toBe('generic');
    setLocale('en', { reload: false });
    expect(linkLostLabel(true, generic)).toBe('Bluetooth bridge unreachable or signal too weak');
  });
});
