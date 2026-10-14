import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  PING_INTERVAL_MS,
  PONG_TIMEOUT_MS,
  RealtimeClient,
  RELEASE_DELAY_MS,
  SUBSCRIBE_CHUNK,
  backoffDelay,
  type DriveEvent,
  type RealtimeDeps,
  type SocketLike,
} from './client.svelte';

class FakeSocket implements SocketLike {
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  closed: number | null = null;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(readonly url: string) {}

  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }

  close(code = 1000): void {
    this.closed = code;
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

  types(): string[] {
    return this.sent.map((m) => String(m.type));
  }
}

const READY = {
  type: 'ready',
  connection_id: 'c1',
  principal: {},
  server_time: '2026-10-09T12:00:00Z',
  token_expires_at: '2026-10-09T12:10:00Z',
};

function setup(overrides: Partial<RealtimeDeps> = {}) {
  const sockets: FakeSocket[] = [];
  const deps: RealtimeDeps = {
    url: () => 'ws://test/api/v1/realtime/ws',
    getToken: vi.fn(async () => 'token-1'),
    onState: vi.fn(),
    onResult: vi.fn(),
    onRevoked: vi.fn(),
    onDenied: vi.fn(),
    onSessionLost: vi.fn(),
    socketFactory: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    random: () => 0.5,
    ...overrides,
  };
  const client = new RealtimeClient(deps);
  const last = () => sockets[sockets.length - 1] as FakeSocket;
  return { client, deps, sockets, last };
}

async function connect(env: ReturnType<typeof setup>): Promise<FakeSocket> {
  const socket = env.last();
  socket.open();
  await vi.waitFor(() => expect(socket.types()).toContain('auth'));
  socket.receive(READY);
  return socket;
}

describe('RealtimeClient', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('connects lazily, authenticates first and subscribes after ready', async () => {
    const env = setup();
    expect(env.sockets).toHaveLength(0);
    env.client.watch(['m1', 'm2']);
    expect(env.client.phase).toBe('connecting');
    const socket = await connect(env);
    expect(socket.sent[0]).toEqual({ type: 'auth', token: 'token-1' });
    expect(socket.sent[1]).toMatchObject({ type: 'subscribe', mower_ids: ['m1', 'm2'] });
    expect(env.client.phase).toBe('ready');
  });

  it('subscribes in chunks of 200', async () => {
    const env = setup();
    const ids = Array.from({ length: SUBSCRIBE_CHUNK + 5 }, (_, i) => `m${i}`);
    env.client.watch(ids);
    const socket = await connect(env);
    const subscribes = socket.sent.filter((m) => m.type === 'subscribe');
    expect(subscribes.map((m) => (m.mower_ids as string[]).length)).toEqual([200, 5]);
  });

  it('counts references and releases with a delay', async () => {
    const env = setup();
    const releaseA = env.client.watch(['m1']);
    const socket = await connect(env);
    const releaseB = env.client.watch(['m1', 'm2']);
    expect(socket.sent.at(-1)).toMatchObject({ type: 'subscribe', mower_ids: ['m2'] });
    releaseA();
    vi.advanceTimersByTime(RELEASE_DELAY_MS);
    expect(socket.types()).not.toContain('unsubscribe');
    releaseB();
    releaseB(); // idempotent
    vi.advanceTimersByTime(RELEASE_DELAY_MS);
    expect(socket.sent.at(-1)).toMatchObject({ type: 'unsubscribe', mower_ids: ['m1', 'm2'] });
    expect(env.client.watched).toEqual([]);
  });

  it('forwards states, results, revocations and denials', async () => {
    const env = setup();
    env.client.watch(['m1', 'm2']);
    const socket = await connect(env);
    socket.receive({ type: 'state', mower_id: 'm1', state: { activity: 'mowing' } });
    socket.receive({ type: 'command_result', mower_id: 'm1', result: { command_id: 'c' } });
    socket.receive({ type: 'subscribed', mower_ids: ['m1'], denied: ['m2'] });
    socket.receive({ type: 'revoked', mower_ids: ['m1'], reason: 'grants_changed' });
    socket.receive({ type: 'pong' });
    expect(env.deps.onState).toHaveBeenCalledOnce();
    expect(env.deps.onResult).toHaveBeenCalledOnce();
    expect(env.deps.onDenied).toHaveBeenCalledWith(['m2']);
    expect(env.deps.onRevoked).toHaveBeenCalledWith(['m1']);
    expect(env.client.watched).toEqual(['m2']);
  });

  it('ignores garbage frames', async () => {
    const env = setup();
    env.client.watch(['m1']);
    const socket = await connect(env);
    socket.onmessage?.({ data: 'not json' } as MessageEvent<string>);
    expect(env.client.phase).toBe('ready');
  });

  it('re-authenticates on the open socket', async () => {
    const env = setup();
    env.client.watch(['m1']);
    const socket = await connect(env);
    env.client.reauth('token-2');
    expect(socket.sent.at(-1)).toEqual({ type: 'auth', token: 'token-2' });
    socket.receive(READY); // the server answers ready again: no second subscribe
    expect(socket.sent.filter((m) => m.type === 'subscribe')).toHaveLength(1);
  });

  it('reconnects with jittered backoff and resubscribes', async () => {
    const env = setup();
    env.client.watch(['m1']);
    await connect(env);
    env.last().drop(1006);
    expect(env.client.phase).toBe('backoff');
    vi.advanceTimersByTime(backoffDelay(0, () => 0.5));
    expect(env.sockets).toHaveLength(2);
    const second = await connect(env);
    expect(second.sent[1]).toMatchObject({ type: 'subscribe', mower_ids: ['m1'] });
  });

  it('grows the backoff and caps it at 30 s', () => {
    const delays = [0, 1, 2, 3, 10].map((n) => backoffDelay(n, () => 0.999));
    expect(delays[0]).toBeLessThan(delays[1] as number);
    expect(delays[4]).toBeLessThanOrEqual(30_000);
    expect(backoffDelay(5, () => 0)).toBe(0);
  });

  it('gets a fresh token after 4401 and reconnects at once', async () => {
    const getToken = vi.fn(async (force?: boolean) => (force ? 'fresh' : 'stale'));
    const env = setup({ getToken });
    env.client.watch(['m1']);
    await connect(env);
    env.last().drop(4401);
    await vi.waitFor(() => expect(env.sockets).toHaveLength(2));
    // The refused token goes along, so one that replaced it meanwhile needs no refresh.
    expect(getToken).toHaveBeenCalledWith(true, 'stale');
  });

  it('reports a lost session when no token can be had after 4401', async () => {
    const getToken = vi.fn(async (force?: boolean) => (force ? null : 'stale'));
    const env = setup({ getToken });
    env.client.watch(['m1']);
    await connect(env);
    env.last().drop(4401);
    await vi.waitFor(() => expect(env.deps.onSessionLost).toHaveBeenCalled());
    expect(env.client.phase).toBe('stopped');
  });

  it('stops when there is no token for the first auth', async () => {
    const env = setup({ getToken: vi.fn(async () => null) });
    env.client.watch(['m1']);
    env.last().open();
    await vi.waitFor(() => expect(env.deps.onSessionLost).toHaveBeenCalled());
    expect(env.client.phase).toBe('stopped');
  });

  it('backs off at least 30 s after 4429 and flags too many sessions', async () => {
    const env = setup();
    env.client.watch(['m1']);
    await connect(env);
    env.last().drop(4429);
    expect(env.client.tooMany).toBe(true);
    vi.advanceTimersByTime(29_999);
    expect(env.sockets).toHaveLength(1);
    vi.advanceTimersByTime(30_000);
    expect(env.sockets).toHaveLength(2);
    await connect(env);
    expect(env.client.tooMany).toBe(false);
  });

  it('retries a slow consumer close (4430) immediately once', async () => {
    const env = setup();
    env.client.watch(['m1']);
    await connect(env);
    env.last().drop(4430);
    vi.advanceTimersByTime(0);
    expect(env.sockets).toHaveLength(2);
  });

  it('closes and reconnects a half-open socket when no pong arrives', async () => {
    const env = setup();
    env.client.watch(['m1']);
    const socket = await connect(env);
    vi.advanceTimersByTime(PING_INTERVAL_MS);
    expect(socket.types()).toContain('ping');
    vi.advanceTimersByTime(PONG_TIMEOUT_MS);
    expect(socket.closed).toBe(4000);
    expect(env.client.phase).toBe('backoff');
  });

  it('wakes up from backoff and stops for good on logout', async () => {
    const env = setup();
    env.client.watch(['m1']);
    await connect(env);
    env.last().drop(1011);
    env.client.wake();
    expect(env.sockets).toHaveLength(2);
    env.client.stop();
    expect(env.last().closed).toBe(1000);
    expect(env.client.phase).toBe('stopped');
    expect(env.client.watched).toEqual([]);
  });

  it('survives a socket factory that throws', () => {
    const env = setup({
      socketFactory: () => {
        throw new Error('blocked');
      },
    });
    env.client.watch(['m1']);
    expect(env.client.phase).toBe('backoff');
  });
});

describe('RealtimeClient manual driving', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  const STARTED = {
    type: 'drive_started',
    session_id: 's1',
    mower_id: 'm1',
    max_rate_hz: 10,
    ttl_ms: 1000,
    max_seconds: 600,
    blade_on_allowed: false,
    capabilities: { manual_drive: true, manual_drive_blades: true },
  };

  async function driving(blades = false) {
    let now = 1_000;
    const env = setup({ now: () => now });
    const events: DriveEvent[] = [];
    env.client.onDrive((event) => events.push(event));
    env.client.watch(['m1']);
    const socket = await connect(env);
    expect(env.client.startDrive('m1', blades)).toBe(true);
    const start = socket.sent.at(-1) as Record<string, unknown>;
    expect(start).toMatchObject({ type: 'drive_start', mower_id: 'm1', blade_on_allowed: blades });
    expect(env.client.driveStarting).toBe(true);
    socket.receive({ ...STARTED, request_id: start.request_id, blade_on_allowed: blades });
    return {
      env,
      socket,
      events,
      advance: (ms: number) => {
        now += ms;
      },
    };
  }

  it('refuses to start before the socket is ready', () => {
    const env = setup();
    expect(env.client.startDrive('m1')).toBe(false);
    expect(env.client.sendFrame({ linear: 1, angular: 0 })).toBe(false);
  });

  it('opens a session, sends numbered frames and reports acks', async () => {
    const { env, socket, events, advance } = await driving();
    expect(env.client.driveStarting).toBe(false);
    expect(env.client.drive).toMatchObject({ sessionId: 's1', maxRateHz: 10, ttlMs: 1000 });
    expect(events[0]).toMatchObject({ type: 'started', session: { sessionId: 's1' } });
    expect(env.client.sendFrame({ linear: 0.5, angular: -2, blade_on: true })).toBe(true);
    expect(socket.sent.at(-1)).toEqual({
      type: 'drive_frame',
      session_id: 's1',
      seq: 1,
      linear: 0.5,
      angular: -1,
      blade_on: false, // the session does not allow blades
    });
    advance(100);
    expect(env.client.sendFrame({ linear: Number.NaN, angular: 0 })).toBe(true);
    expect(socket.sent.at(-1)).toMatchObject({ seq: 2, linear: 0 });
    socket.receive({ type: 'drive_frame_ack', session_id: 's1', seq: 2 });
    socket.receive({ type: 'drive_frame_ack', session_id: 'other', seq: 9 });
    expect(events.filter((e) => e.type === 'ack')).toEqual([
      { type: 'ack', sessionId: 's1', seq: 2 },
    ]);
  });

  it('drops frames above the server rate unless forced', async () => {
    const { env, socket, advance } = await driving();
    expect(env.client.sendFrame({ linear: 1, angular: 0 })).toBe(true);
    advance(50);
    expect(env.client.sendFrame({ linear: 1, angular: 0 })).toBe(false);
    advance(40); // 90 ms: within the 10 % slack of 100 ms
    expect(env.client.sendFrame({ linear: 1, angular: 0 })).toBe(true);
    advance(10);
    expect(env.client.sendFrame({ linear: 0, angular: 0 }, true)).toBe(true);
    expect(socket.sent.filter((m) => m.type === 'drive_frame')).toHaveLength(3);
  });

  it('carries blade_on only when the session allows blades', async () => {
    const { env, socket } = await driving(true);
    env.client.sendFrame({ linear: 0.2, angular: 0, blade_on: true });
    expect(socket.sent.at(-1)).toMatchObject({ blade_on: true });
  });

  it('stops: drive_stop, a local end event, and no more frames', async () => {
    const { env, socket, events } = await driving();
    env.client.stopDrive();
    expect(socket.sent.at(-1)).toMatchObject({ type: 'drive_stop', session_id: 's1' });
    expect(events.at(-1)).toEqual({
      type: 'ended',
      sessionId: 's1',
      reason: 'client_stop',
      detail: null,
    });
    expect(env.client.drive).toBeNull();
    expect(env.client.sendFrame({ linear: 1, angular: 0 })).toBe(false);
    // The server's own drive_ended for that session is not reported twice.
    socket.receive({
      type: 'drive_ended',
      session_id: 's1',
      mower_id: 'm1',
      reason: 'client_stop',
    });
    expect(events.filter((e) => e.type === 'ended')).toHaveLength(1);
    env.client.stopDrive(); // idempotent
  });

  it('reports the server ending the session with its reason', async () => {
    const { env, socket, events } = await driving();
    socket.receive({
      type: 'drive_ended',
      session_id: 's1',
      mower_id: 'm1',
      reason: 'mower_busy',
      detail: 'job running',
    });
    expect(events.at(-1)).toEqual({
      type: 'ended',
      sessionId: 's1',
      reason: 'mower_busy',
      detail: 'job running',
    });
    expect(env.client.drive).toBeNull();
  });

  it('ends the session when the socket closes, so frames stop', async () => {
    const { env, socket, events } = await driving();
    socket.drop(1006);
    expect(events.at(-1)).toMatchObject({ type: 'ended', reason: 'connection_lost' });
    expect(env.client.drive).toBeNull();
    expect(env.client.sendFrame({ linear: 1, angular: 0 })).toBe(false);
  });

  it('sends drive_stop and ends the session on logout', async () => {
    const { env, socket, events } = await driving();
    env.client.stop();
    expect(socket.sent.at(-1)).toMatchObject({ type: 'drive_stop', session_id: 's1' });
    expect(events.at(-1)).toMatchObject({ type: 'ended', reason: 'connection_lost' });
  });

  it('reports drive errors of the pending start', async () => {
    const env = setup();
    const events: DriveEvent[] = [];
    const off = env.client.onDrive((event) => events.push(event));
    env.client.watch(['m1']);
    const socket = await connect(env);
    env.client.startDrive('m1');
    const start = socket.sent.at(-1) as Record<string, unknown>;
    socket.receive({ type: 'error', code: 'limit_exceeded', message: 'no', request_id: 'x' });
    socket.receive({
      type: 'error',
      code: 'drive_not_allowed',
      message: 'not allowed',
      request_id: start.request_id,
    });
    expect(events).toEqual([{ type: 'error', code: 'drive_not_allowed', message: 'not allowed' }]);
    expect(env.client.driveStarting).toBe(false);
    // A pending start lost with the socket is reported as unavailable.
    env.client.startDrive('m1');
    socket.drop(1006);
    expect(events.at(-1)).toMatchObject({ type: 'error', code: 'drive_unavailable' });
    off();
  });

  it('closes a session nobody waits for any more', async () => {
    const env = setup();
    env.client.watch(['m1']);
    const socket = await connect(env);
    env.client.startDrive('m1');
    env.client.stopDrive(); // the page was left before the answer
    socket.receive({ ...STARTED, request_id: 'r-late' });
    expect(socket.sent.at(-1)).toEqual({ type: 'drive_stop', session_id: 's1' });
    expect(env.client.drive).toBeNull();
  });

  it('ends the session on drive_not_active', async () => {
    const { env, socket, events } = await driving();
    socket.receive({ type: 'error', code: 'drive_not_active', message: 'no session' });
    expect(events.slice(-2)).toEqual([
      { type: 'error', code: 'drive_not_active', message: 'no session' },
      { type: 'ended', sessionId: 's1', reason: 'connection_lost', detail: 'no session' },
    ]);
    expect(env.client.drive).toBeNull();
  });
});
