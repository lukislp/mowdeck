/**
 * The live incident of 2026-10-18 end to end at unit level: the real viewer with the real WHEP
 * player against a fake relay (Fleet's session and lease endpoints, MediaMTX's WHEP endpoint)
 * on one fake clock. The camera becomes playable only some seconds after its lease opened (a
 * LUBA takes 6 to 8 s, after a 4G stall up to about 35 s), its muxer restarts mid-stream, the
 * relay restarts and forgets the lease. Before the fix the player stopped offering after 20 s,
 * the viewer swallowed the loss its rejoin reported and the tile said "connecting" for good
 * while the relay released the mower's stream.
 */
import { describe, expect, it } from 'vitest';

import { ApiProblem } from '#lib/api/problem.js';

import { DEFAULT_OPTIONS } from './options';
import type { CameraSession } from './session';
import { CameraViewer } from './viewer.svelte';
import { TILE_GIVE_UP_MS, WhepPlayer, type PeerConnection } from './whep-player';

const T0 = Date.parse('2026-10-18T16:03:14Z');

class Peer extends EventTarget {
  iceGatheringState: RTCIceGatheringState = 'complete';
  iceConnectionState: RTCIceConnectionState = 'new';
  connectionState: RTCPeerConnectionState = 'new';
  localDescription = { type: 'offer', sdp: 'v=0 offer' };
  answered = false;
  closed = false;

  addTransceiver(): RTCRtpTransceiver {
    return {} as RTCRtpTransceiver;
  }
  async createOffer(): Promise<RTCSessionDescriptionInit> {
    return { type: 'offer', sdp: 'v=0 offer' };
  }
  async setLocalDescription(): Promise<void> {}
  async setRemoteDescription(): Promise<void> {
    this.answered = true;
    // The media arrives: a track on the tile.
    queueMicrotask(() => {
      const event = new Event('track') as Event & { track: MediaStreamTrack };
      event.track = { kind: 'video', id: 't', stop: () => undefined } as MediaStreamTrack;
      this.dispatchEvent(event);
    });
  }
  async getStats(): Promise<RTCStatsReport> {
    return new Map() as unknown as RTCStatsReport;
  }
  getReceivers(): RTCRtpReceiver[] {
    return [];
  }
  close(): void {
    this.closed = true;
  }
  closeRemotely(): void {
    this.connectionState = 'closed';
    this.dispatchEvent(new Event('connectionstatechange'));
  }
}

/** A fake relay: leases, a WHEP path that is ready from `readyAt`, a fake clock. */
function stack() {
  let clock = T0;
  let nextTimer = 0;
  const timers = new Map<number, { fn: () => void; due: number }>();
  const leases = new Set<string>();
  const log: string[] = [];
  let leaseCount = 0;
  let readyAt = T0;
  let requestFailures: (ApiProblem | null)[] = [];
  const peers: Peer[] = [];
  const video = document.createElement('video');
  let frames = 0;

  const session = (lease: string): CameraSession => ({
    provider: 'relay',
    app_id: '',
    channel: '',
    token: '',
    uid: 0,
    cameras: [{ id: '0', name: 'Front left', uid: 1, placement: 'front_left' }],
    expires_at: new Date(clock + 60_000).toISOString(),
    relay: {
      tiles: [{ camera_id: '0', whep_url: '/media/whep/r/7f53/0/whep', available: null }],
      ticket: `v1.${lease}.sig`,
      lease_id: lease,
      heartbeat_seconds: 15,
    },
  });

  const setTimer = (fn: () => void, ms: number): number => {
    nextTimer += 1;
    timers.set(nextTimer, { fn, due: clock + ms });
    return nextTimer;
  };
  const clearTimer = (handle: unknown): void => void timers.delete(handle as number);
  const flush = async (): Promise<void> => {
    for (let i = 0; i < 20; i += 1) await Promise.resolve();
  };

  const fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    const auth = (init?.headers as Record<string, string> | undefined)?.Authorization ?? '';
    const lease = auth.replace('Bearer v1.', '').replace('.sig', '');
    log.push(`${method} ${String(input)}`);
    if (method === 'DELETE') return { status: 200, headers: new Headers() } as Response;
    if (!leases.has(lease)) return { status: 401, headers: new Headers() } as Response;
    if (clock < readyAt) return { status: 404, headers: new Headers() } as Response;
    const headers = new Headers({ Location: `/r/7f53/0/whep/${lease}` });
    return { status: 201, headers, text: async () => 'v=0 answer' } as unknown as Response;
  }) as typeof globalThis.fetch;

  const lease = {
    renew: async (id: string) => {
      log.push(`renew ${id}`);
      if (!leases.has(id)) throw new ApiProblem({ status: 404 });
      return session(id);
    },
    detach: async (id: string) => {
      log.push(`detach ${id}`);
      leases.delete(id);
    },
  };

  const viewer = new CameraViewer({
    request: async () => {
      const failure = requestFailures.shift();
      if (failure) throw failure;
      leaseCount += 1;
      const id = `lease-${leaseCount}`;
      leases.add(id);
      log.push(`open ${id}`);
      return session(id);
    },
    discard: async (s) => void (await lease.detach(s.relay!.lease_id)),
    createPlayer: async (_s, events) =>
      new WhepPlayer(() => video, events, {
        lease,
        fetch,
        createPeer: () => {
          const peer = new Peer();
          peers.push(peer);
          return peer as unknown as PeerConnection;
        },
        origin: () => 'https://mowdeck.example',
        frames: () => frames,
        stream: (tracks) => ({ getTracks: () => tracks }) as unknown as MediaStream,
        now: () => clock,
        setTimer,
        clearTimer,
      }),
    secure: () => true,
    cameraEnabled: async () => true,
    now: () => clock,
    setTimer,
    clearTimer,
    options: { ...DEFAULT_OPTIONS },
    saveOptions: () => undefined,
    layout: 'single',
    saveLayout: () => undefined,
  });

  /** Let `ms` pass; frames flow while a tile has its answer (the picture plays). */
  const advance = async (ms: number): Promise<void> => {
    const until = clock + ms;
    await flush();
    for (;;) {
      const next = [...timers].sort((a, b) => a[1].due - b[1].due || a[0] - b[0])[0];
      if (!next || next[1].due > until) break;
      timers.delete(next[0]);
      clock = next[1].due;
      if (peers.some((p) => p.answered && !p.closed)) frames += 1;
      next[1].fn();
      await flush();
    }
    clock = until;
    await flush();
  };

  return {
    viewer,
    peers,
    log,
    leases,
    advance,
    readyIn: (ms: number) => (readyAt = clock + ms),
    /** The relay restarted: every lease it held is forgotten. */
    forgetLeases: () => leases.clear(),
    failRequests: (...failures: (ApiProblem | null)[]) => (requestFailures = failures),
    opened: () => log.filter((l) => l.startsWith('open ')).length,
  };
}

describe('camera relay recovery (live incident 2026-10-18)', () => {
  it.each([8_000, 30_000, 35_000])(
    'a camera that becomes playable %i ms after its lease opened plays on that lease',
    async (delay) => {
      const s = stack();
      s.readyIn(delay);
      await s.viewer.open('mower');
      await s.advance(1_000);
      expect(s.viewer.phase).toBe('connecting');
      expect(s.viewer.starting).toEqual([1]);
      expect(s.viewer.tiles[0]!.status).toBe('starting');
      await s.advance(delay + 4_000);
      expect(s.viewer.phase).toBe('live');
      expect(s.viewer.starting).toEqual([]);
      expect(s.opened()).toBe(1); // no rejoin, no second lease
      expect(s.log.filter((l) => l.startsWith('detach'))).toEqual([]);
      await s.advance(60_000);
      expect(s.viewer.phase).toBe('live'); // the heartbeat keeps the lease
    },
  );

  it('a lease that lapses while the rejoin still starts its camera leads to the next rejoin', async () => {
    // The deadlock itself: a loss the new player reports while it starts used to be dropped.
    const s = stack();
    s.readyIn(5_000);
    await s.viewer.open('mower');
    await s.advance(8_000);
    expect(s.viewer.phase).toBe('live');
    s.forgetLeases(); // the relay restarted (in-memory leases gone)
    s.readyIn(40_000); // and its source takes long to come back
    await s.advance(20_000); // heartbeat 404: rejoin with a new lease
    expect(s.opened()).toBe(2);
    expect(s.viewer.phase).toBe('connecting');
    s.forgetLeases(); // and again, while the new player still offers (401 on its next offer)
    await s.advance(15_000);
    expect(s.opened()).toBeGreaterThanOrEqual(3);
    await s.advance(40_000);
    expect(s.viewer.phase).toBe('live');
    expect(s.viewer.error).toBeNull();
  });

  it('a muxer restart mid-stream (reader dropped, 28 s of 404) recovers on the same lease', async () => {
    const s = stack();
    await s.viewer.open('mower');
    await s.advance(2_000);
    expect(s.viewer.phase).toBe('live');
    s.readyIn(28_000);
    s.peers.at(-1)!.closeRemotely();
    await s.advance(1_000);
    expect(s.viewer.live).toEqual([]);
    expect(s.viewer.starting).toEqual([1]);
    await s.advance(32_000);
    expect(s.viewer.live).toEqual([1]);
    expect(s.viewer.phase).toBe('live');
    expect(s.opened()).toBe(1);
  });

  it('a 4G or WLAN gap keeps retrying the session request and says so', async () => {
    const s = stack();
    await s.viewer.open('mower');
    await s.advance(2_000);
    s.forgetLeases();
    s.failRequests(...Array.from({ length: 10 }, () => ApiProblem.network()));
    await s.advance(20_000);
    expect(s.viewer.unreachable).toBe(true);
    expect(s.viewer.phase).toBe('connecting');
    await s.advance(180_000); // ten failed requests, more than the rejoin limit
    expect(s.viewer.error).toBeNull();
    expect(s.viewer.phase).toBe('live');
    expect(s.viewer.unreachable).toBe(false);
  });

  it('a camera that never comes is reported unreachable after 90 s, with its lease ended', async () => {
    const s = stack();
    s.readyIn(10 * 60_000);
    await s.viewer.open('mower');
    await s.advance(TILE_GIVE_UP_MS + 5_000);
    expect(s.viewer.phase).toBe('error');
    expect(s.viewer.error).toBe('unavailable');
    expect(s.leases.size).toBe(0); // detached: the relay releases the stream after its grace
  });
});
