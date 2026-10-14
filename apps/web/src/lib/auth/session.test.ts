import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  LOCK_TIMEOUT_MS,
  REFRESH_TIMEOUT_MS,
  SessionManager,
  type SessionDeps,
} from './session.svelte';

type Reply = { status: number; body?: unknown };

function json(reply: Reply): Response {
  return new Response(reply.body === undefined ? null : JSON.stringify(reply.body), {
    status: reply.status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const ME = {
  subject: 'u1',
  kind: 'user',
  account_id: 'a1',
  display_name: 'Lukas',
  roles: ['owner'],
  permissions: ['mower.view'],
};

class FakeChannel {
  static all: FakeChannel[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  constructor() {
    FakeChannel.all.push(this);
  }
  postMessage(message: unknown): void {
    for (const other of FakeChannel.all) {
      if (other !== this) other.onmessage?.({ data: message } as MessageEvent);
    }
  }
  close(): void {}
}

function setup(routes: Record<string, Reply | (() => Reply)>, extra: Partial<SessionDeps> = {}) {
  let now = 1_000_000;
  const calls: { url: string; init?: RequestInit }[] = [];
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    const route = routes[url];
    if (!route) return json({ status: 404 });
    return json(typeof route === 'function' ? route() : route);
  });
  const session = new SessionManager({
    fetch: fetch as unknown as typeof globalThis.fetch,
    now: () => now,
    locks: null,
    channel: null,
    ...extra,
  });
  return { session, fetch, calls, advance: (ms: number) => (now += ms), nowRef: () => now };
}

const TOKEN = { access_token: 'acc-1', expires_in: 600 };

describe('SessionManager', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeChannel.all = [];
  });
  afterEach(() => vi.useRealTimers());

  it('logs in with cookie-only refresh transport and loads /me', async () => {
    const env = setup({
      '/api/v1/identity/session': { status: 200, body: TOKEN },
      '/api/v1/identity/me': { status: 200, body: ME },
    });
    await env.session.login('lukas', 'secret-password');
    const login = env.calls[0];
    const headers = login?.init?.headers as Record<string, string>;
    expect(headers['X-Mowdeck-Refresh']).toBe('cookie-only');
    expect(headers['X-Requested-With']).toBeTruthy();
    expect(env.session.token).toBe('acc-1');
    expect(env.session.status).toBe('authenticated');
    expect(env.session.has('mower.view')).toBe(true);
    expect(env.session.has('users.manage')).toBe(false);
    const me = env.calls[1]?.init?.headers as Record<string, string>;
    expect(me.Authorization).toBe('Bearer acc-1');
  });

  it('throws the problem of a failed login', async () => {
    const env = setup({
      '/api/v1/identity/session': {
        status: 401,
        body: { type: 'urn:mowdeck:problem:unauthorized', status: 401 },
      },
    });
    await expect(env.session.login('x', 'y')).rejects.toMatchObject({ status: 401 });
    expect(env.session.token).toBeNull();
  });

  it('restores from the refresh cookie and refreshes 60 s before expiry', async () => {
    let n = 0;
    const env = setup({
      '/api/v1/identity/session/refresh': () => ({
        status: 200,
        body: { access_token: `acc-${++n}`, expires_in: 600 },
      }),
      '/api/v1/identity/me': { status: 200, body: ME },
    });
    expect(await env.session.restore()).toBe(true);
    expect(env.session.token).toBe('acc-1');
    const listener = vi.fn();
    env.session.onToken(listener);
    env.advance(540_000);
    await vi.advanceTimersByTimeAsync(540_000);
    expect(env.session.token).toBe('acc-2');
    expect(listener).toHaveBeenCalledWith('acc-2');
  });

  it('is anonymous when there is no refresh cookie', async () => {
    const env = setup({ '/api/v1/identity/session/refresh': { status: 422 } });
    expect(await env.session.restore()).toBe(false);
    expect(env.session.status).toBe('anonymous');
  });

  it('runs one refresh for concurrent callers', async () => {
    const env = setup({ '/api/v1/identity/session/refresh': { status: 200, body: TOKEN } });
    const [a, b] = await Promise.all([env.session.refresh(), env.session.refresh()]);
    expect(a).toBe('acc-1');
    expect(b).toBe('acc-1');
    expect(env.fetch).toHaveBeenCalledTimes(1);
  });

  it('serialises refreshes under the Web Lock', async () => {
    const order: string[] = [];
    const locks = {
      request: async <T>(name: string, _options: unknown, callback: () => Promise<T>) => {
        order.push(name);
        return callback();
      },
    };
    const env = setup(
      { '/api/v1/identity/session/refresh': { status: 200, body: TOKEN } },
      { locks },
    );
    await env.session.refresh();
    expect(order).toEqual(['mowdeck-refresh']);
  });

  it('adopts a token another tab just obtained instead of refreshing', async () => {
    const tabA = setup(
      {
        '/api/v1/identity/session/refresh': { status: 200, body: TOKEN },
        '/api/v1/identity/me': { status: 200, body: ME },
      },
      { channel: new FakeChannel() },
    );
    const tabB = setup({}, { channel: new FakeChannel() });
    await tabA.session.restore();
    const token = await tabB.session.refresh();
    expect(token).toBe('acc-1');
    expect(tabB.fetch).not.toHaveBeenCalled();
  });

  it('follows a logout of another tab', async () => {
    const routes = {
      '/api/v1/identity/session/refresh': { status: 200, body: TOKEN },
      '/api/v1/identity/me': { status: 200, body: ME },
      '/api/v1/identity/session/logout': { status: 204 },
    };
    const tabA = setup(routes, { channel: new FakeChannel() });
    const tabB = setup(routes, { channel: new FakeChannel() });
    await tabA.session.restore();
    await tabB.session.restore();
    await tabA.session.logout();
    expect(tabB.session.status).toBe('anonymous');
    expect(tabB.session.token).toBeNull();
  });

  it('keeps a still valid token instead of refreshing', async () => {
    const env = setup({
      '/api/v1/identity/session': { status: 200, body: TOKEN },
      '/api/v1/identity/me': { status: 200, body: ME },
    });
    await env.session.login('a', 'b');
    env.fetch.mockClear();
    expect(await env.session.validToken()).toBe('acc-1');
    expect(env.fetch).not.toHaveBeenCalled();
  });

  it('marks an expired session and clears it', async () => {
    const env = setup({
      '/api/v1/identity/session': { status: 200, body: TOKEN },
      '/api/v1/identity/me': { status: 200, body: ME },
    });
    await env.session.login('a', 'b');
    const listener = vi.fn();
    env.session.onToken(listener);
    env.session.expire();
    expect(env.session.ended).toBe('expired');
    expect(env.session.status).toBe('anonymous');
    expect(listener).toHaveBeenCalledWith(null);
  });

  it('keeps the token when the refresh fails for lack of network', async () => {
    const env = setup({
      '/api/v1/identity/session': { status: 200, body: TOKEN },
      '/api/v1/identity/me': { status: 200, body: ME },
    });
    await env.session.login('a', 'b');
    env.fetch.mockRejectedValueOnce(new TypeError('offline'));
    expect(await env.session.refresh()).toBe('acc-1');
  });

  it('keeps the session when Identity answers a server error', async () => {
    const env = setup({
      '/api/v1/identity/session': { status: 200, body: TOKEN },
      '/api/v1/identity/me': { status: 200, body: ME },
      '/api/v1/identity/session/refresh': { status: 503 },
    });
    await env.session.login('a', 'b');
    expect(await env.session.refresh()).toBe('acc-1');
    expect(env.session.status).toBe('authenticated');
  });

  it('aborts a hanging refresh request and keeps the token', async () => {
    const env = setup({
      '/api/v1/identity/session': { status: 200, body: TOKEN },
      '/api/v1/identity/me': { status: 200, body: ME },
    });
    await env.session.login('a', 'b');
    let aborted = false;
    env.fetch.mockImplementationOnce(
      (_input, init) =>
        new Promise((_, reject) => {
          init?.signal?.addEventListener('abort', () => {
            aborted = true;
            reject(new DOMException('aborted', 'AbortError'));
          });
        }),
    );
    const pending = env.session.refresh();
    await vi.advanceTimersByTimeAsync(REFRESH_TIMEOUT_MS);
    expect(await pending).toBe('acc-1');
    expect(aborted).toBe(true);
  });

  it('gives up a lock another tab never releases and refreshes without it', async () => {
    const locks = {
      // A frozen tab holds the lock: the request is granted never, only aborted.
      request: <T>(_name: string, options: { signal?: AbortSignal }) =>
        new Promise<T>((_, reject) => {
          options.signal?.addEventListener('abort', () =>
            reject(new DOMException('aborted', 'AbortError')),
          );
        }),
    };
    const env = setup(
      { '/api/v1/identity/session/refresh': { status: 200, body: TOKEN } },
      { locks },
    );
    const pending = env.session.refresh();
    await vi.advanceTimersByTimeAsync(LOCK_TIMEOUT_MS);
    expect(await pending).toBe('acc-1');
    expect(env.fetch).toHaveBeenCalledTimes(1);
  });

  it('always releases the lock, also when the refresh fails', async () => {
    let held = 0;
    const locks = {
      request: async <T>(_name: string, _options: unknown, callback: () => Promise<T>) => {
        held += 1;
        try {
          return await callback();
        } finally {
          held -= 1;
        }
      },
    };
    const env = setup({ '/api/v1/identity/session/refresh': { status: 401 } }, { locks });
    expect(await env.session.refresh()).toBeNull();
    expect(held).toBe(0);
  });

  it('takes the token another tab obtained while it waited for the lock', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const locks = {
      request: async <T>(_name: string, _options: unknown, callback: () => Promise<T>) => {
        await gate;
        return callback();
      },
    };
    const routes = {
      '/api/v1/identity/session': { status: 200, body: TOKEN },
      '/api/v1/identity/me': { status: 200, body: ME },
    };
    const tabA = setup(
      {
        ...routes,
        '/api/v1/identity/session/refresh': {
          status: 200,
          body: { access_token: 'acc-2', expires_in: 600 },
        },
      },
      { channel: new FakeChannel() },
    );
    const tabB = setup(routes, { channel: new FakeChannel(), locks });
    await tabA.session.login('a', 'b');
    await tabB.session.login('a', 'b');
    tabB.fetch.mockClear();
    // Both tabs' timers fire; B waits for the lock while A refreshes and broadcasts.
    const waiting = tabB.session.refresh();
    await tabA.session.refresh();
    release();
    expect(await waiting).toBe('acc-2');
    expect(tabB.fetch).not.toHaveBeenCalled();
  });

  it('answers a 401 for a replaced token with the new one, without refreshing', async () => {
    const env = setup({
      '/api/v1/identity/session': { status: 200, body: TOKEN },
      '/api/v1/identity/me': { status: 200, body: ME },
      '/api/v1/identity/session/refresh': {
        status: 200,
        body: { access_token: 'acc-2', expires_in: 600 },
      },
    });
    await env.session.login('a', 'b');
    expect(await env.session.validToken(true, 'acc-1')).toBe('acc-2');
    env.fetch.mockClear();
    // A late 401 of a request that still carried acc-1.
    expect(await env.session.validToken(true, 'acc-1')).toBe('acc-2');
    expect(env.fetch).not.toHaveBeenCalled();
    // The new token itself refused: that needs a real refresh.
    await env.session.validToken(true, 'acc-2');
    expect(env.fetch).toHaveBeenCalledTimes(1);
  });
});
