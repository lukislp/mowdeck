import { describe, expect, it } from 'vitest';

import { dockChoiceOffered } from './dock-choice';

describe('dock choice', () => {
  it('is offered only for a running job on a mower that keeps it', () => {
    expect(dockChoiceOffered({ dock_keeps_job: true }, true)).toBe(true);
    expect(dockChoiceOffered({ dock_keeps_job: true }, false)).toBe(false);
    expect(dockChoiceOffered({ dock_keeps_job: false }, true)).toBe(false);
    expect(dockChoiceOffered({}, true)).toBe(false);
    expect(dockChoiceOffered(null, true)).toBe(false);
  });
});
