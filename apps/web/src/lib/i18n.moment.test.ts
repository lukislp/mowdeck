import { describe, expect, it } from 'vitest';

import { formatMoment } from '#lib/i18n.js';

/** Local times (the helper compares calendar days in the browser's time zone). */
const at = (day: number, hour: number, minute = 0) => new Date(2026, 9, day, hour, minute);

describe('formatMoment', () => {
  it('shows only the time on the same day', () => {
    expect(formatMoment(at(31, 23, 59), at(31, 9), 'de')).toBe('23:59');
    expect(formatMoment(at(31, 0, 5).toISOString(), at(31, 23).getTime(), 'en')).toMatch(
      /^12:05\sAM$/,
    );
  });

  it('adds the date on another day, so tomorrow never reads as today', () => {
    const tomorrow = formatMoment(at(31, 23, 59), at(30, 22), 'de');
    expect(tomorrow).toContain('23:59');
    expect(tomorrow).toContain('31.10.');
    const yesterday = formatMoment(at(30, 23, 50), at(31, 0, 10), 'en');
    expect(yesterday).toContain('10/30/26');
  });
});
