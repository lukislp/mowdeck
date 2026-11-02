// @vitest-environment node
/** Where the tour keeps its outcome (ADR 0073), with demo sandboxes (ADR 0076). */
import { describe, expect, it, vi } from 'vitest';

import type { TourContext, TourOutcome } from './steps';
import { browserFirst, pickStore } from './stores';
import type { TourStore } from './tour.svelte';

const context: TourContext = {
  mowers: 3,
  mowerId: 'm1',
  capabilities: null,
  canManage: true,
  modules: new Set(),
  demo: true,
};

function fake(outcome: TourOutcome | null | Error): TourStore & {
  save: ReturnType<typeof vi.fn>;
} {
  return {
    load: vi.fn(async () => {
      if (outcome instanceof Error) throw outcome;
      return outcome;
    }),
    save: vi.fn(async () => {}),
  };
}

describe('browserFirst (demo sandboxes)', () => {
  it("takes the browser's outcome over a fresh sandbox user's", async () => {
    for (const outcome of ['seen', 'done', 'skipped'] as const) {
      const user = fake('new');
      expect(await browserFirst(fake(outcome), user).load()).toBe(outcome);
      expect(user.load).not.toHaveBeenCalled();
    }
  });

  it("falls back to the user's outcome when the browser has none", async () => {
    expect(await browserFirst(fake('new'), fake('done')).load()).toBe('done');
    expect(await browserFirst(fake(null), fake('skipped')).load()).toBe('skipped');
    expect(await browserFirst(fake(new Error('blocked')), fake('seen')).load()).toBe('seen');
    expect(await browserFirst(fake('new'), fake('new')).load()).toBe('new');
    expect(await browserFirst(fake('new'), fake(null)).load()).toBeNull();
  });

  it('saves to both, and a failing one does not stop the other', async () => {
    const browser = fake('new');
    const user = fake('new');
    await browserFirst(browser, user).save('done', context);
    expect(browser.save).toHaveBeenCalledWith('done', context);
    expect(user.save).toHaveBeenCalledWith('done', context);

    const broken = fake('new');
    broken.save.mockRejectedValueOnce(new Error('offline'));
    const other = fake('new');
    await expect(browserFirst(broken, other).save('skipped', context)).resolves.toBeUndefined();
    expect(other.save).toHaveBeenCalledWith('skipped', context);
  });
});

describe('pickStore', () => {
  it('uses the user store on a regular installation', async () => {
    const user = fake('done');
    const browser = fake('skipped');
    const store = pickStore(() => 'user', { user, browser });
    expect(await store.load()).toBe('done');
    await store.save('seen', context);
    expect(user.save).toHaveBeenCalledOnce();
    expect(browser.save).not.toHaveBeenCalled();
  });

  it('keeps the shared demo per browser only', async () => {
    const user = fake('done');
    const browser = fake('new');
    const store = pickStore(() => 'shared-demo', { user, browser });
    expect(await store.load()).toBe('new');
    await store.save('skipped', context);
    expect(browser.save).toHaveBeenCalledWith('skipped', context);
    expect(user.load).not.toHaveBeenCalled();
    expect(user.save).not.toHaveBeenCalled();
  });

  it('keeps a sandbox in both, the browser first', async () => {
    const user = fake('new');
    const browser = fake('skipped');
    const store = pickStore(() => 'sandbox', { user, browser });
    expect(await store.load()).toBe('skipped');
    await store.save('done', context);
    expect(browser.save).toHaveBeenCalledWith('done', context);
    expect(user.save).toHaveBeenCalledWith('done', context);
  });

  it('decides when used, not when built (the demo config loads later)', async () => {
    let mode: 'user' | 'shared-demo' | 'sandbox' = 'user';
    const user = fake('done');
    const browser = fake('seen');
    const store = pickStore(() => mode, { user, browser });
    expect(await store.load()).toBe('done');
    mode = 'shared-demo';
    expect(await store.load()).toBe('seen');
  });
});
