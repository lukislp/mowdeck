import { describe, expect, it, vi } from 'vitest';

import { appleEnabled } from './apple';
import { githubEnabled } from './github';
import { loginMethods, methodEnabled } from './login-methods';
import { passkeysEnabled } from './passkey';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

describe('login methods', () => {
  it('asks once for the checks the sign-in page makes at the same time', async () => {
    const fetchFn = vi.fn(async () => json({ passkey: true, github: false, apple: true }));
    const [passkey, github, apple] = await Promise.all([
      passkeysEnabled(fetchFn),
      githubEnabled(fetchFn),
      appleEnabled(fetchFn),
    ]);
    expect([passkey, github, apple]).toEqual([true, false, true]);
    expect(fetchFn).toHaveBeenCalledOnce();
    expect(fetchFn).toHaveBeenCalledWith('/api/v1/identity/login-methods', {
      credentials: 'omit',
    });
  });

  it('keeps nothing once answered: a later check asks again', async () => {
    const fetchFn = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json({ passkey: false }))
      .mockResolvedValueOnce(json({ passkey: true }));
    expect(await methodEnabled('passkey', fetchFn)).toBe(false);
    expect(await methodEnabled('passkey', fetchFn)).toBe(true);
    expect(fetchFn).toHaveBeenCalledTimes(2);
  });

  it('never rejects', async () => {
    expect(await loginMethods(vi.fn(async () => json({}, 503)))).toBeNull();
    expect(await loginMethods(vi.fn(async () => new Response('not json')))).toBeNull();
    const broken = vi.fn(() => {
      throw new TypeError('offline');
    });
    expect(await methodEnabled('github', broken)).toBe(false);
    expect(await methodEnabled('github', broken)).toBe(false);
    expect(broken).toHaveBeenCalledTimes(2);
  });
});
