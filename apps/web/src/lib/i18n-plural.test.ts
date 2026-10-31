import { describe, expect, it } from 'vitest';

import { m } from '#lib/paraglide/messages.js';

describe('plural messages', () => {
  it('picks the singular for one hour in both languages', () => {
    expect(m.tracking_share_hours_option({ hours: '1' }, { locale: 'de' })).toBe('1 Stunde');
    expect(m.tracking_share_hours_option({ hours: '1' }, { locale: 'en' })).toBe('1 hour');
  });

  it('picks the plural for every other count', () => {
    expect(m.tracking_share_hours_option({ hours: '6' }, { locale: 'de' })).toBe('6 Stunden');
    expect(m.tracking_share_hours_option({ hours: 48 }, { locale: 'en' })).toBe('48 hours');
  });
});
