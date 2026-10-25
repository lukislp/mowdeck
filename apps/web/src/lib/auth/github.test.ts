import { describe, expect, it, vi } from 'vitest';

import {
  githubEnabled,
  linkGitHub,
  linkResult,
  returnQuery,
  signInResult,
  signInWithGitHub,
  withoutResult,
} from './github';

function json(status: number, body?: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const AUTHORIZE = 'https://github.com/login/oauth/authorize?client_id=x&state=y';

describe('githubEnabled', () => {
  it('reads only the flag of the login methods and never rejects', async () => {
    const on = vi.fn(async () => json(200, { password: true, passkey: false, github: true }));
    expect(await githubEnabled(on)).toBe(true);
    expect(on).toHaveBeenCalledWith('/api/v1/identity/login-methods', { credentials: 'omit' });
    expect(await githubEnabled(async () => json(200, { github: 'yes' }))).toBe(false);
    expect(await githubEnabled(async () => json(500))).toBe(false);
    expect(
      await githubEnabled(async () => {
        throw new TypeError('offline');
      }),
    ).toBe(false);
  });
});

describe('returnQuery', () => {
  it('keeps next and the app hand-off, drops everything else', () => {
    const params = new URLSearchParams(
      'next=/schedules&app_request=r1&app_state=s1&sign_in_error=denied&provider=github&x=1',
    );
    expect(returnQuery(params)).toBe('next=%2Fschedules&app_request=r1&app_state=s1');
    expect(returnQuery(new URLSearchParams('sign_in_error=denied'))).toBeNull();
  });
});

describe('signInWithGitHub', () => {
  it('starts the round trip with the page query and leaves for GitHub', async () => {
    const fetch = vi.fn(async () => json(200, { authorize_url: AUTHORIZE, expires_in: 600 }));
    const navigate = vi.fn();
    await signInWithGitHub(new URLSearchParams('next=/mowers&evil=1'), { fetch, navigate });
    expect(fetch).toHaveBeenCalledWith('/api/v1/identity/oauth/github/start', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ return_query: 'next=%2Fmowers' }),
    });
    expect(navigate).toHaveBeenCalledWith(AUTHORIZE);
  });

  it('throws the problem when GitHub is off, and never follows a non-http URL', async () => {
    const navigate = vi.fn();
    const off = vi.fn(async () =>
      json(404, { type: 'urn:mowdeck:problem:not-found', title: 'Not found', status: 404 }),
    );
    await expect(
      signInWithGitHub(new URLSearchParams(), { fetch: off, navigate }),
    ).rejects.toMatchObject({ status: 404 });
    const hostile = vi.fn(async () => json(200, { authorize_url: 'javascript:alert(1)' }));
    await expect(
      signInWithGitHub(new URLSearchParams(), { fetch: hostile, navigate }),
    ).rejects.toThrow(TypeError);
    const empty = vi.fn(async () => json(200, {}));
    await expect(
      signInWithGitHub(new URLSearchParams(), { fetch: empty, navigate }),
    ).rejects.toThrow(TypeError);
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe('linkGitHub', () => {
  it('runs the password call and leaves for GitHub', async () => {
    const navigate = vi.fn();
    await linkGitHub(async () => ({ authorize_url: AUTHORIZE }), { navigate });
    expect(navigate).toHaveBeenCalledWith(AUTHORIZE);
    const failing = linkGitHub(
      async () => {
        throw new Error('401');
      },
      { navigate },
    );
    await expect(failing).rejects.toThrow('401');
    expect(navigate).toHaveBeenCalledTimes(1);
  });
});

describe('results of the round trip', () => {
  it('reads the sign-in result for GitHub only', () => {
    expect(signInResult(new URLSearchParams('signed_in_with=github'))).toEqual({ signedIn: true });
    expect(signInResult(new URLSearchParams('sign_in_error=cancelled&provider=github'))).toEqual({
      error: 'cancelled',
    });
    expect(signInResult(new URLSearchParams('sign_in_error=<script>&provider=github'))).toEqual({
      error: 'denied',
    });
    expect(signInResult(new URLSearchParams('sign_in_error=denied&provider=gitlab'))).toBeNull();
    expect(signInResult(new URLSearchParams('next=/x'))).toBeNull();
  });

  it('reads the link result', () => {
    expect(linkResult(new URLSearchParams('linked=github'))).toEqual({ linked: true });
    expect(linkResult(new URLSearchParams('link_error=taken&provider=github'))).toEqual({
      error: 'taken',
    });
    expect(linkResult(new URLSearchParams('link_error=rate_limited&provider=github'))).toEqual({
      error: 'denied',
    });
    expect(linkResult(new URLSearchParams())).toBeNull();
  });

  it('removes the result from the address, keeps the rest', () => {
    const url = new URL(
      'https://m.example/login?next=%2Fx&app_request=r&signed_in_with=github&provider=github',
    );
    expect(withoutResult(url)).toBe('/login?next=%2Fx&app_request=r');
    expect(withoutResult(new URL('https://m.example/settings?linked=github#profile'))).toBe(
      '/settings#profile',
    );
  });
});
