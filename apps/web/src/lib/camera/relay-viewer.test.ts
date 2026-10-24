import { describe, expect, it, vi } from 'vitest';

import { ApiProblem } from '#lib/api/problem.js';

import { DEFAULT_OPTIONS, type CameraTransport } from './options';
import type { CameraPlayer, PlayerEvents } from './player';
import { sameChannel, type CameraSession } from './session';
import { CameraViewer, type ViewerDeps } from './viewer.svelte';

const NOW = Date.parse('2026-10-17T12:00:00Z');

function relaySession(lease = 'lease-1', ticket = 'v1.t.sig'): CameraSession {
  return {
    provider: 'relay',
    app_id: '',
    channel: '',
    token: '',
    uid: 0,
    cameras: [{ id: 'front_left', name: 'Front left', uid: 1 }],
    expires_at: new Date(NOW + 60_000).toISOString(),
    relay: {
      tiles: [{ camera_id: 'front_left', whep_url: '/media/whep/r/a/front_left/whep' }],
      ticket,
      lease_id: lease,
      heartbeat_seconds: 15,
    },
  };
}

function directSession(): CameraSession {
  return {
    provider: 'fake',
    app_id: '',
    channel: 'sim',
    token: '',
    uid: 0,
    cameras: [{ id: 'front_left', name: 'Front left', uid: 1 }],
    expires_at: new Date(NOW + 300_000).toISOString(),
    relay: null,
  };
}

class StubPlayer implements CameraPlayer {
  stopped = 0;
  constructor(
    readonly session: CameraSession,
    readonly events: PlayerEvents,
    /** Report a lost connection while starting (a lease that lapsed under the new player). */
    readonly loseOnStart = false,
  ) {}
  async start(): Promise<void> {
    if (this.loseOnStart) {
      this.events.onStarting?.([1]);
      this.events.onConnectionLost?.('disconnected');
      return;
    }
    this.events.onPublishers?.([1]);
    this.events.onActive?.(1);
    this.events.onTracks?.([1]);
    this.events.onLive();
  }
  async renew(): Promise<void> {}
  async select(): Promise<void> {}
  setMuted(): void {}
  async stop(): Promise<void> {
    this.stopped += 1;
  }
}

function harness(first: CameraSession = relaySession(), transport: CameraTransport = 'auto') {
  const players: StubPlayer[] = [];
  const losing = { count: 0 };
  const discarded: string[] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  let lease = 1;
  const request = vi.fn(async (_id: string, wanted: CameraTransport) => {
    if (request.mock.calls.length === 1) return first;
    lease += 1;
    return wanted === 'direct' ? directSession() : relaySession(`lease-${lease}`);
  });
  const deps: ViewerDeps = {
    request,
    discard: async (session) => void discarded.push(session.relay?.lease_id ?? ''),
    createPlayer: async (session, events) => {
      const lose = losing.count > 0;
      if (lose) losing.count -= 1;
      const player = new StubPlayer(session, events, lose);
      players.push(player);
      return player;
    },
    secure: () => true,
    cameraEnabled: async () => true,
    now: () => NOW,
    setTimer: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimer: () => undefined,
    options: { ...DEFAULT_OPTIONS, transport },
    saveOptions: () => undefined,
    layout: 'single',
    saveLayout: () => undefined,
  };
  return { viewer: new CameraViewer(deps), players, timers, request, losing, discarded, deps };
}

describe('CameraViewer with the camera relay', () => {
  it('asks for the browser transport and never requests a new session for a relay ticket', async () => {
    const { viewer, timers, request } = harness();
    await viewer.open('m1');
    expect(request).toHaveBeenCalledExactlyOnceWith('m1', 'auto');
    expect(viewer.phase).toBe('live');
    expect(viewer.transport).toBe('relay');
    // The player renews the lease itself; no refresh timer by the ticket's expiry.
    expect(timers.filter((t) => t.ms >= 5_000 && t.ms !== 8_000 && t.ms !== 2_000)).toEqual([]);
  });

  it('takes renewed sessions from the player', async () => {
    const { viewer, players } = harness();
    await viewer.open('m1');
    players[0]!.events.onRenewed?.(relaySession('lease-1', 'v1.fresh.sig'));
    expect(viewer.session?.relay?.ticket).toBe('v1.fresh.sig');
    expect(viewer.renewals).toBe(1);
  });

  it('pins the path of the first session: auto never switches on a rejoin', async () => {
    const { viewer, players, timers, request } = harness();
    await viewer.open('m1');
    players[0]!.events.onConnectionLost?.('disconnected');
    const rejoin = timers.at(-1)!;
    rejoin.fn();
    await vi.waitFor(() => expect(players).toHaveLength(2));
    expect(request).toHaveBeenLastCalledWith('m1', 'relay');
    expect(players[0]!.stopped).toBe(1);

    const direct = harness(directSession());
    await direct.viewer.open('m1');
    expect(direct.viewer.transport).toBe('direct');
    direct.players[0]!.events.onConnectionLost?.('stuck');
    direct.timers.at(-1)!.fn();
    await vi.waitFor(() => expect(direct.players).toHaveLength(2));
    expect(direct.request).toHaveBeenLastCalledWith('m1', 'direct');
  });

  it('a hidden page detaches a relay session at once, a visible one requests a new session', async () => {
    const { viewer, players, request } = harness();
    await viewer.open('m1');
    await viewer.suspend();
    expect(players[0]!.stopped).toBe(1);
    expect(viewer.suspended).toBe(true);
    expect(viewer.phase).toBe('connecting');
    expect(viewer.live).toEqual([]);
    await viewer.suspend(); // twice hidden: nothing more
    expect(players[0]!.stopped).toBe(1);
    await viewer.resume();
    expect(request).toHaveBeenLastCalledWith('m1', 'relay');
    expect(players).toHaveLength(2);
    expect(viewer.phase).toBe('live');
    expect(viewer.suspended).toBe(false);
    await viewer.resume(); // not suspended: nothing
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('a hidden page leaves the direct path playing', async () => {
    const { viewer, players } = harness(directSession(), 'direct');
    await viewer.open('m1');
    await viewer.suspend();
    expect(players[0]!.stopped).toBe(0);
    expect(viewer.suspended).toBe(false);
    expect(viewer.phase).toBe('live');
  });

  it('a loss reported by the new player of a rejoin while it starts leads to the next rejoin', async () => {
    // Live incident 2026-10-18: the second loss in a row arrived while the rejoin was still
    // playing its session and was dropped; the tile said "connecting" for good.
    const { viewer, players, timers, request, losing } = harness();
    await viewer.open('m1');
    losing.count = 2; // the next two players lose their lease while they start
    players[0]!.events.onConnectionLost?.('disconnected');
    for (let round = 1; round <= 3; round += 1) {
      timers.at(-1)!.fn();
      await vi.waitFor(() => expect(players).toHaveLength(round + 1));
    }
    expect(request).toHaveBeenCalledTimes(4);
    expect(viewer.phase).toBe('live');
    expect(viewer.rejoins).toBe(3);
    expect(viewer.starting).toEqual([]);
  });

  it('starting cameras are reported per tile until they play', async () => {
    const { viewer, players, losing } = harness();
    losing.count = 1;
    await viewer.open('m1');
    expect(viewer.starting).toEqual([1]);
    players[0]!.events.onActive?.(1);
    expect(viewer.tiles[0]!.status).toBe('starting');
    players[0]!.events.onStarting?.([]);
    players[0]!.events.onTracks?.([1]);
    expect(viewer.tiles[0]!.status).toBe('live');
  });

  it('a refused rejoin (503) gives up after the last attempt, a network gap does not', async () => {
    const { viewer, players, timers, request } = harness();
    await viewer.open('m1');
    request.mockRejectedValue(new ApiProblem({ status: 503 }));
    players[0]!.events.onConnectionLost?.('disconnected');
    for (let i = 0; i < 8 && viewer.phase !== 'error'; i += 1) {
      timers.at(-1)!.fn();
      await vi.waitFor(() => expect(request.mock.calls.length).toBeGreaterThan(i + 1));
    }
    await vi.waitFor(() => expect(viewer.phase).toBe('error'));
    expect(viewer.error).toBe('unavailable');

    const offline = harness();
    await offline.viewer.open('m1');
    offline.request.mockRejectedValue(ApiProblem.network());
    offline.players[0]!.events.onConnectionLost?.('disconnected');
    for (let i = 0; i < 12; i += 1) {
      offline.timers.at(-1)!.fn();
      await vi.waitFor(() => expect(offline.request.mock.calls.length).toBe(i + 2));
    }
    expect(offline.viewer.phase).toBe('connecting');
    expect(offline.viewer.unreachable).toBe(true);
    expect(offline.timers.at(-1)!.ms).toBe(16_000); // the longest delay repeats
    offline.request.mockResolvedValue(relaySession('lease-back'));
    offline.timers.at(-1)!.fn();
    await vi.waitFor(() => expect(offline.viewer.phase).toBe('live'));
    expect(offline.viewer.unreachable).toBe(false);
  });

  it('a session whose player cannot be created is discarded', async () => {
    const { viewer, deps, discarded } = harness();
    deps.createPlayer = async () => {
      throw new Error('no video element');
    };
    await viewer.open('m1');
    expect(viewer.phase).toBe('error');
    expect(discarded).toEqual(['lease-1']);
  });

  it('a session that arrives after the page went hidden is not played', async () => {
    const { viewer, request, players, discarded } = harness();
    let release!: (session: CameraSession) => void;
    request.mockImplementationOnce(
      () => new Promise<CameraSession>((resolve) => (release = resolve)),
    );
    const opening = viewer.open('m1');
    await vi.waitFor(() => expect(request).toHaveBeenCalled());
    await viewer.suspend();
    release(relaySession('lease-late'));
    await opening;
    expect(players).toEqual([]);
    expect(discarded).toEqual(['lease-late']); // its lease ends at once
    await viewer.resume();
    expect(players).toHaveLength(1);
  });

  it('a changed transport option opens the camera again with it', async () => {
    const { viewer, request, players } = harness();
    await viewer.open('m1');
    await viewer.setOptions({ transport: 'direct' });
    expect(request).toHaveBeenLastCalledWith('m1', 'direct');
    expect(players[0]!.stopped).toBe(1);
    expect(viewer.transport).toBe('direct');
  });

  it('a refused resume shows the error', async () => {
    const { viewer, request } = harness();
    await viewer.open('m1');
    await viewer.suspend();
    request.mockRejectedValueOnce(new ApiProblem({ status: 503 }));
    await viewer.resume();
    expect(viewer.phase).toBe('error');
    expect(viewer.error).toBe('unavailable');
  });

  it('two relay sessions are the same channel only with the same lease', () => {
    expect(sameChannel(relaySession('a'), relaySession('a', 'other-ticket'))).toBe(true);
    expect(sameChannel(relaySession('a'), relaySession('b'))).toBe(false);
    expect(sameChannel(relaySession('a'), directSession())).toBe(false);
  });
});
