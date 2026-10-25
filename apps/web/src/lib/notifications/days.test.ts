import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import { dayKey, relativeDay } from './days';

// The browser's zone decides the day; pin it to one with DST changes.
let zone: string | undefined;
beforeAll(() => {
  zone = process.env.TZ;
  process.env.TZ = 'Europe/Berlin';
});
afterAll(() => {
  if (zone === undefined) delete process.env.TZ;
  else process.env.TZ = zone;
});

describe('inbox day headings', () => {
  it('runs in Europe/Berlin', () => {
    expect(new Date('2026-07-01T00:00:00Z').getHours()).toBe(2);
  });

  it('labels today and yesterday on ordinary days', () => {
    const now = Date.parse('2026-10-14T10:00:00Z');
    expect(relativeDay('2026-10-14T05:00:00Z', now)).toBe('today');
    expect(relativeDay('2026-10-13T21:59:00Z', now)).toBe('yesterday'); // 23:59 CEST
    expect(relativeDay('2026-10-12T21:59:00Z', now)).toBeNull();
  });

  it('finds yesterday after the spring change (a 23 hour day)', () => {
    // Monday 30 March 2026, 00:30 CEST; 24 hours earlier is still Saturday 23:30 CET.
    const now = Date.parse('2026-03-29T22:30:00Z');
    expect(relativeDay('2026-03-29T10:00:00Z', now)).toBe('yesterday'); // Sunday
    expect(relativeDay('2026-03-28T22:30:00Z', now)).toBeNull(); // Saturday 23:30 CET
  });

  it('finds yesterday late on the autumn change day (a 25 hour day)', () => {
    // Sunday 25 October 2026, 23:30 CET; 24 hours earlier is Sunday 00:30 CEST.
    const now = Date.parse('2026-10-25T22:30:00Z');
    expect(relativeDay('2026-10-24T20:00:00Z', now)).toBe('yesterday'); // Saturday 22:00
    expect(relativeDay('2026-10-24T23:00:00Z', now)).toBe('today'); // Sunday 01:00 CEST
    expect(dayKey('2026-10-25T01:30:00Z')).toBe(dayKey('2026-10-25T00:30:00Z')); // both 02:30
  });
});
