import { beforeEach, describe, expect, it } from 'vitest';

import {
  correctionAge,
  correctionTone,
  gnssTone,
  hasReception,
  readableKey,
  signalBars,
  signalTone,
} from './reception';
import {
  errorCategoryLabel,
  gnssSolutionLabel,
  networkLabel,
  selfCheckReason,
} from './reception-labels';

import { setLocale } from '$lib/paraglide/runtime';

beforeEach(() => setLocale('de', { reload: false }));

describe('signalBars', () => {
  it.each([
    [-40, 4],
    [-55, 4],
    [-58, 3],
    [-67, 3],
    [-70, 2],
    [-80, 1],
    [-85, 1],
    [-90, 0],
  ])('Wi-Fi %s dBm has %s bars', (dbm, bars) => {
    expect(signalBars(dbm, 'wifi')).toBe(bars);
  });

  it.each([
    [-75, 4],
    [-85, 3],
    [-95, 2],
    [-105, 1],
    [-120, 0],
  ])('mobile %s dBm has %s bars', (dbm, bars) => {
    expect(signalBars(dbm, 'mobile')).toBe(bars);
  });

  it('is null when not reported', () => {
    expect(signalBars(null, 'wifi')).toBeNull();
    expect(signalBars(undefined, 'mobile')).toBeNull();
    expect(signalBars(Number.NaN, 'wifi')).toBeNull();
  });
});

describe('tones', () => {
  it('warns on weak signals', () => {
    expect(signalTone(null)).toBeNull();
    expect(signalTone(0)).toBe('error');
    expect(signalTone(1)).toBe('paused');
    expect(signalTone(3)).toBeNull();
  });

  it('follows the RTK solution', () => {
    expect(gnssTone('fixed')).toBe('mowing');
    expect(gnssTone('float')).toBe('paused');
    expect(gnssTone('single')).toBe('paused');
    expect(gnssTone('none')).toBe('error');
    expect(gnssTone('unknown')).toBeNull();
    expect(gnssTone(null)).toBeNull();
  });

  it('marks old corrections', () => {
    expect(correctionTone(1)).toBeNull();
    expect(correctionTone(10)).toBeNull();
    expect(correctionTone(11)).toBe('paused');
    expect(correctionTone(null)).toBeNull();
  });
});

describe('correctionAge', () => {
  it('reads seconds with a decimal below 10 s, then seconds, then minutes', () => {
    expect(correctionAge(1)).toEqual({ value: 1, digits: 1, unit: 's' });
    expect(correctionAge(42.4)).toEqual({ value: 42, digits: 0, unit: 's' });
    expect(correctionAge(600)).toEqual({ value: 10, digits: 0, unit: 'min' });
  });
});

describe('hasReception', () => {
  it('needs at least one reported value', () => {
    expect(hasReception({})).toBe(false);
    expect(hasReception({ gnss: null, signal: null, self_check: null })).toBe(false);
    expect(hasReception({ rain_detected: false })).toBe(true);
    expect(hasReception({ gnss: { satellites: 3, rtk_status: 'float' } })).toBe(true);
  });
});

describe('labels', () => {
  it('translates the enums (German is the base locale)', () => {
    expect(gnssSolutionLabel('fixed')).toBe('RTK Fix');
    expect(gnssSolutionLabel('single')).toBe('Nur GNSS');
    expect(networkLabel('mobile')).toBe('Mobilfunk');
    expect(errorCategoryLabel('trapped')).toBe('Festgefahren');
    expect(errorCategoryLabel('other')).toBe('Sonstiger Fehler');
  });

  it('gives the self-check reason in words', () => {
    expect(selfCheckReason({ result: 'ok' })).toBeNull();
    expect(selfCheckReason({ result: 'blocked', detail: 'rain' })).toBe('Regen erkannt');
    expect(selfCheckReason({ result: 'blocked', detail: 'safety_key_missing' })).toBe(
      'Sicherheitsschlüssel fehlt',
    );
    expect(selfCheckReason({ result: 'blocked', detail: 'other_77' })).toBe(
      'Unbekannter Grund (77)',
    );
    expect(selfCheckReason({ result: 'blocked', detail: 'brand_new_reason' })).toBe(
      'Brand new reason',
    );
    expect(selfCheckReason({ result: 'blocked', detail: 'toString' })).toBe('ToString');
    expect(selfCheckReason({ result: 'blocked', detail: null })).toBe('Grund nicht gemeldet');
  });

  it('makes unknown keys readable', () => {
    expect(readableKey('weak_rtk_signal')).toBe('Weak rtk signal');
    expect(readableKey('a-b')).toBe('A b');
    expect(readableKey('__')).toBe('__');
  });
});
