/** The tour's demo notes with the shared demo user (ADR 0068) and with sandboxes (ADR 0076). */
import { beforeEach, describe, expect, it } from 'vitest';

import { setLocale } from '#lib/paraglide/runtime.js';

import { tourText } from './labels';

describe('tour demo notes', () => {
  beforeEach(() => setLocale('de', { reload: false }));

  it('says the shared demo is shared', () => {
    const context = { canManage: true, demo: true };
    expect(tourText('welcome', context).demo).toMatch(/geteilt mit allen Besuchern/);
    expect(tourText('overview', context).demo).toMatch(/allen Besuchern/);
  });

  it("calls a sandbox the visitor's own and claims no sharing", () => {
    const context = { canManage: true, demo: true, sandbox: true };
    const welcome = tourText('welcome', context).demo ?? '';
    const overview = tourText('overview', context).demo ?? '';
    expect(welcome).toMatch(/öffentliche Demo/);
    expect(welcome).toMatch(/eigenen Sandbox/);
    expect(overview).toMatch(/in der Demo gesperrt/);
    for (const text of [welcome, overview]) expect(text).not.toMatch(/geteilt|teilst/);
  });

  it('shows no note outside the demo', () => {
    expect(tourText('welcome', { canManage: true, demo: false, sandbox: true }).demo).toBeNull();
  });
});
