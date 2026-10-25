import { describe, expect, it, vi } from 'vitest';

import {
  appleEnabled,
  appleLinkResult,
  appleSignInResult,
  linkApple,
  signInWithApple,
} from './apple';
import { githubEnabled, linkResult, signInResult } from './github';

function json(status: number, body?: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const AUTHORIZE = 'https://appleid.apple.com/auth/authorize?client_id=x&state=y';

describe('appleEnabled', () => {
  it('reads only its own flag of the login methods and never rejects', async () => {
    const both = async () => json(200, { password: true, github: false, apple: true });
    expect(await appleEnabled(both)).toBe(true);
    expect(await githubEnabled(both)).toBe(false);
    expect(await appleEnabled(async () => json(200, { apple: 'yes' }))).toBe(false);
    expect(await appleEnabled(async () => json(500))).toBe(false);
    expect(
      await appleEnabled(async () => {
        throw new TypeError('offline');
      }),
    ).toBe(false);
  });
});

describe('signInWithApple', () => {
  it('starts the round trip with the page query and leaves for Apple', async () => {
    const fetch = vi.fn(async () => json(200, { authorize_url: AUTHORIZE, expires_in: 600 }));
    const navigate = vi.fn();
    await signInWithApple(new URLSearchParams('next=/mowers&evil=1'), { fetch, navigate });
    expect(fetch).toHaveBeenCalledWith('/api/v1/identity/oauth/apple/start', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ return_query: 'next=%2Fmowers' }),
    });
    expect(navigate).toHaveBeenCalledWith(AUTHORIZE);
  });

  it('throws the problem when Apple is off, and never follows a non-http URL', async () => {
    const navigate = vi.fn();
    const off = vi.fn(async () =>
      json(404, { type: 'urn:mowdeck:problem:not-found', title: 'Not found', status: 404 }),
    );
    await expect(
      signInWithApple(new URLSearchParams(), { fetch: off, navigate }),
    ).rejects.toMatchObject({ status: 404 });
    const hostile = vi.fn(async () => json(200, { authorize_url: 'javascript:alert(1)' }));
    await expect(
      signInWithApple(new URLSearchParams(), { fetch: hostile, navigate }),
    ).rejects.toThrow(TypeError);
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe('linkApple', () => {
  it('leaves for Apple after the password call', async () => {
    const navigate = vi.fn();
    await linkApple(async () => ({ authorize_url: AUTHORIZE }), { navigate });
    expect(navigate).toHaveBeenCalledWith(AUTHORIZE);
  });
});

describe('results of the round trip', () => {
  it('reads the sign-in result of Apple only', () => {
    expect(appleSignInResult(new URLSearchParams('signed_in_with=apple'))).toEqual({
      signedIn: true,
    });
    expect(
      appleSignInResult(new URLSearchParams('sign_in_error=cancelled&provider=apple')),
    ).toEqual({ error: 'cancelled' });
    expect(appleSignInResult(new URLSearchParams('sign_in_error=x&provider=apple'))).toEqual({
      error: 'denied',
    });
    // The other provider's result is not Apple's, and the other way round.
    expect(appleSignInResult(new URLSearchParams('signed_in_with=github'))).toBeNull();
    expect(signInResult(new URLSearchParams('signed_in_with=apple'))).toBeNull();
    expect(
      appleSignInResult(new URLSearchParams('sign_in_error=denied&provider=github')),
    ).toBeNull();
  });

  it('reads the link result of Apple only', () => {
    expect(appleLinkResult(new URLSearchParams('linked=apple'))).toEqual({ linked: true });
    expect(appleLinkResult(new URLSearchParams('link_error=taken&provider=apple'))).toEqual({
      error: 'taken',
    });
    expect(linkResult(new URLSearchParams('linked=apple'))).toBeNull();
    expect(appleLinkResult(new URLSearchParams('linked=github'))).toBeNull();
  });
});
