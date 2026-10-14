/**
 * The access token expires in the middle of a session (owner report: pages stuck loading after a
 * silent refresh). Session, API fetch wrapper and Realtime socket together, with a fake clock, a
 * fake server and a fake socket: every pending request completes with the new token after
 * exactly one refresh, the socket re-authenticates and resubscribes, the next proactive refresh
 * is scheduled from the new expiry, a refused refresh ends the session (the app layout then
 * sends the user to the login), and no request waits forever.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { authFetch, REQUEST_TIMEOUT_MS } from '$lib/api/client';
import { RealtimeClient, type SocketLike } from '$lib/realtime/client.svelte';

import { SessionManager } from './session.svelte';

const READY = {
  type: 'ready',
  connection_id: 'c1',
  principal: {},
  server_time: '2026-10-10T12:00:00Z',
  token_expires_at: '2026-10-10T12:10:00Z',
};

class FakeSocket implements SocketLike {
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  send(data: string): void {
    const message = JSON.parse(data) as Record<string, unknown>;
    this.sent.push(message);
    // A healthy server answers every ping.
    if (message.type === 'ping') {
      queueMicrotask(() => this.receive({ type: 'pong', request_id: message.request_id }));
    }
  }
  close(): void {
    this.readyState = 3;
  }
  open(): void {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }
  receive(message: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(message) } as MessageEvent<string>);
  }
  drop(code: number): void {
    this.readyState = 3;
    this.onclose?.({ code } as CloseEvent);
  }
  of(type: string): Record<string, unknown>[] {
    return this.sent.filter((message) => message.type === type);
  }
}

function json(status: number, body?: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Identity and one API service: the server accepts only the newest token it issued. */
function server(options: { refreshStatus?: number } = {}) {
  let issued = 1;
  const expired = new Set<string>();
  const refreshes = vi.fn();
  const identity = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/session')) return json(200, { access_token: 'acc-1', expires_in: 600 });
    if (url.endsWith('/me')) return json(200, { subject: 'u1', permissions: [], roles: [] });
    if (url.endsWith('/session/refresh')) {
      refreshes();
      if (options.refreshStatus) return json(options.refreshStatus);
      issued += 1;
      return json(200, { access_token: `acc-${issued}`, expires_in: 600 });
    }
    return json(404);
  });
  /** Held responses: the request reached the server and answers later. */
  const held: { resolve: () => void }[] = [];
  const api = vi.fn(async (request: Request) => {
    const token = request.headers.get('Authorization')?.replace('Bearer ', '');
    const answer = () =>
      token === `acc-${issued}` && !expired.has(token)
        ? json(200, { token })
        : json(401, { title: 'Unauthorized' });
    if (request.headers.get('X-Hold') === '1' && held.length === 0) {
      // Decided when it answers: by then the token it carries has expired.
      return new Promise<Response>((resolve) => held.push({ resolve: () => resolve(answer()) }));
    }
    return answer();
  });
  return {
    identity,
    api,
    refreshes,
    held,
    /** The server considers the current token expired (its clock is ahead of the tab's). */
    expire: () => expired.add(`acc-${issued}`),
    issued: () => issued,
  };
}

async function signedIn(options: { refreshStatus?: number } = {}) {
  const srv = server(options);
  const session = new SessionManager({
    fetch: srv.identity as unknown as typeof fetch,
    now: () => Date.now(),
    locks: null,
    channel: null,
  });
  await session.login('owner', 'secret');
  const fetcher = authFetch(session, srv.api as unknown as typeof fetch);
  return { srv, session, fetcher };
}

describe('access token expiry in the middle of a session', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-10T12:00:00Z'));
  });
  afterEach(() => vi.useRealTimers());

  it('retries every pending request once with the new token after exactly one refresh', async () => {
    const { srv, fetcher } = await signedIn();
    // A request that reached the server with acc-1 and answers 401 only after the refresh.
    const late = fetcher('http://x/api/v1/fleet/mowers', { headers: { 'X-Hold': '1' } });
    await vi.waitFor(() => expect(srv.held).toHaveLength(1));
    srv.expire();
    const wave = await Promise.all([
      fetcher('http://x/api/v1/fleet/mowers'),
      fetcher('http://x/api/v1/telemetry/accounts/me/mowers/states'),
      fetcher('http://x/api/v1/fleet/mowers/m1', { method: 'POST', body: '{"a":1}' }),
    ]);
    expect(wave.map((response) => response.status)).toEqual([200, 200, 200]);
    expect(srv.refreshes).toHaveBeenCalledTimes(1);
    srv.held[0]!.resolve();
    const lateResponse = await late;
    expect(lateResponse.status).toBe(200);
    expect(await lateResponse.json()).toEqual({ token: 'acc-2' });
    // The late 401 carried the replaced token: no second refresh.
    expect(srv.refreshes).toHaveBeenCalledTimes(1);
  });

  it('re-authenticates the socket, resubscribes after a 4401 and reschedules from the new expiry', async () => {
    const { srv, session, fetcher } = await signedIn();
    const sockets: FakeSocket[] = [];
    const realtime = new RealtimeClient({
      url: () => 'ws://x/api/v1/realtime/ws',
      getToken: (force, rejected) => session.validToken(force, rejected),
      onState: () => undefined,
      onResult: () => undefined,
      onRevoked: () => undefined,
      onSessionLost: () => session.expire(),
      socketFactory: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        return socket;
      },
      random: () => 0,
    });
    let refreshedAt = 0;
    session.onToken((token) => {
      if (token === 'acc-2') refreshedAt = Date.now();
      if (token) realtime.reauth(token);
    });
    realtime.watch(['m1']);
    sockets[0]!.open();
    await vi.waitFor(() => expect(sockets[0]!.of('auth')).toHaveLength(1));
    sockets[0]!.receive(READY);
    expect(sockets[0]!.of('subscribe')[0]?.mower_ids).toEqual(['m1']);

    // The server's clock is ahead: it closes the socket and refuses requests with acc-1.
    srv.expire();
    sockets[0]!.drop(4401);
    const response = await fetcher('http://x/api/v1/fleet/mowers');
    expect(response.status).toBe(200);
    await vi.waitFor(() => expect(sockets).toHaveLength(2));
    expect(srv.refreshes).toHaveBeenCalledTimes(1);
    sockets[1]!.open();
    await vi.waitFor(() => expect(sockets[1]!.of('auth')).toHaveLength(1));
    expect(sockets[1]!.of('auth')[0]?.token).toBe('acc-2');
    sockets[1]!.receive(READY);
    expect(sockets[1]!.of('subscribe')[0]?.mower_ids).toEqual(['m1']);
    expect(realtime.phase).toBe('ready');

    // The next proactive refresh comes 60 s before the new token's expiry, not the old one's.
    expect(session.expiresAt).toBe(refreshedAt + 600_000);
    const due = session.expiresAt - 60_000 - Date.now();
    await vi.advanceTimersByTimeAsync(due - 1_000);
    expect(srv.refreshes).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(srv.refreshes).toHaveBeenCalledTimes(2);
    // ... and the open socket got the token without reconnecting.
    expect(sockets).toHaveLength(2);
    expect(sockets[1]!.of('auth').at(-1)?.token).toBe('acc-3');
    realtime.stop();
  });

  it('ends the session when the refresh is refused (the layout then shows the login)', async () => {
    const { srv, session, fetcher } = await signedIn({ refreshStatus: 401 });
    srv.expire();
    const response = await fetcher('http://x/api/v1/fleet/mowers');
    expect(response.status).toBe(401);
    expect(session.status).toBe('anonymous');
    expect(session.ended).toBe('expired');
  });

  it('fails a request that hangs instead of loading forever', async () => {
    const tokens = { validToken: vi.fn(async () => 't'), expire: vi.fn() };
    const hanging = vi.fn(
      (_request: Request, init?: RequestInit) =>
        new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener('abort', () =>
            reject(new DOMException('aborted', 'AbortError')),
          );
        }),
    );
    const pending = authFetch(tokens, hanging as never)('http://x/api/v1/fleet/mowers');
    const outcome = expect(pending).rejects.toMatchObject({ status: 0 });
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS);
    await outcome;
    // A token that never comes (a stuck refresh) is covered by the same deadline.
    const stuck = { validToken: () => new Promise<string>(() => undefined), expire: vi.fn() };
    const waiting = authFetch(stuck, hanging as never)('http://x/api/v1/fleet/mowers');
    const outcome2 = expect(waiting).rejects.toMatchObject({ status: 0 });
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS);
    await outcome2;
  });

  it("keeps the caller's abort an abort", async () => {
    const tokens = { validToken: vi.fn(async () => 't'), expire: vi.fn() };
    const hanging = vi.fn(
      (_request: Request, init?: RequestInit) =>
        new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener('abort', () =>
            reject(new DOMException('aborted', 'AbortError')),
          );
        }),
    );
    const controller = new AbortController();
    const pending = authFetch(tokens, hanging as never)('http://x/a', {
      signal: controller.signal,
    });
    await vi.waitFor(() => expect(hanging).toHaveBeenCalled());
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  });
});
