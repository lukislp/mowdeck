import { describe, expect, it } from 'vitest';

import { commandLabel, patternLabel, patternSummary } from '$lib/i18n';
import { setLocale } from '$lib/paraglide/runtime';

describe('pattern labels', () => {
  it('summarises a start in German', () => {
    setLocale('de', { reload: false });
    expect(patternLabel('checkerboard')).toBe('Schachbrett');
    expect(patternSummary({ name: 'stripes', angle_deg: 45, border_laps: 1 })).toBe(
      'Streifen 45° · 1 Randrunde',
    );
    expect(patternSummary({ name: 'diagonal', angle_deg: 0, border_laps: 0 })).toBe(
      'Diagonal 0° · ohne Randrunde',
    );
    expect(patternSummary({ name: 'edge_only', angle_deg: 30, border_laps: 0 })).toBe(
      'Nur Rand · 1 Randrunde',
    );
    expect(
      patternSummary({ name: 'random_angle', angle_deg: 0, border_laps: 3, spacing_m: 0.3 }),
    ).toBe('Zufallswinkel · 3 Randrunden · 30 cm Bahnabstand');
    expect(
      commandLabel('start_mow', null, { name: 'stripes', angle_deg: 45, border_laps: 1 }),
    ).toBe('Mähen starten · Streifen 45° · 1 Randrunde');
    expect(commandLabel('start_mow')).toBe('Mähen starten');
  });

  it('summarises a start in English', () => {
    setLocale('en', { reload: false });
    expect(patternSummary({ name: 'checkerboard', angle_deg: 90, border_laps: 2 })).toBe(
      'Checkerboard 90° · 2 border laps',
    );
    setLocale('de', { reload: false });
  });
});
