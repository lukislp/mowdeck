import { describe, expect, it, vi } from 'vitest';

import { ApiProblem } from '$lib/api/problem';

import { CameraPlayerFactory } from './factory';
import type { CameraPlayer, PlayerEvents } from './player';
import type { CameraSession } from './session';
import {
  DISCONNECTED_LIMIT_MS,
  GATHER_TIMEOUT_MS,
  resolveLocation,
  WHEP_RETRY_MS,
  WHEP_START_ATTEMPTS,
  WhepPlayer,
  type PeerConnection,
  type RelayLeaseApi,
} from './whep-player';

const NOW = Date.parse('2026-10-17T12:00:00Z');
const ORIGIN = 'https://mowdeck.example';
const LEASE = '6f1c2a3b-0000-4000-8000-000000000001';
const TICKET = 'v1.ticket-one.sig'; // a test value, never a real ticket

function relaySession(ticket = TICKET, lease = LEASE): CameraSession {
  return {
    provider: 'relay',
    app_id: '',
    channel: '',
    token: '',
    uid: 0,
    cameras: [
      { id: 'front_left', name: 'Front left', uid: 1, placement: 'front_left' },
      { id: 'front_right', name: 'Front right', uid: 2, placement: 'front_right' },
    ],
    encryption: null,
    expires_at: new Date(NOW + 60_000).toISOString(),
    quota: null,
    relay: {
      tiles: [
        { camera_id: 'front_left', whep_url: '/media/whep/r/abc/front_left/whep' },
        { camera_id: 'front_right', whep_url: '/media/whep/r/abc/front_right/whep' },
      ],
      ticket,
      lease_id: lease,
      heartbeat_seconds: 15,
    },
  };
}

/** A receive-only peer connection that records what the player does. */
class FakePeer extends EventTarget {
  iceGatheringState: RTCIceGatheringState = 'new';
  iceConnectionState: RTCIceConnectionState = 'new';
  connectionState: RTCPeerConnectionState = 'new';
  localDescription: { type: string; sdp: string } | null = null;
  remote: { type: string; sdp?: string } | null = null;
  transceivers: { kind: string; init: RTCRtpTransceiverInit | undefined }[] = [];
  closed = false;
  gathers = true;
  stats: Record<string, unknown>[] = [];

  addTransceiver(kind: string, init?: RTCRtpTransceiverInit): RTCRtpTransceiver {
    this.transceivers.push({ kind, init });
    return {} as RTCRtpTransceiver;
  }

  async createOffer(): Promise<RTCSessionDescriptionInit> {
    return { type: 'offer', sdp: 'v=0 offer' };
  }

  async setLocalDescription(description: RTCSessionDescriptionInit): Promise<void> {
    this.localDescription = { type: 'offer', sdp: `${description.sdp} +candidates` };
    if (this.gathers) {
      this.iceGatheringState = 'complete';
      this.dispatchEvent(new Event('icegatheringstatechange'));
    }
  }

  async setRemoteDescription(description: RTCSessionDescriptionInit): Promise<void> {
    this.remote = { type: description.type, sdp: description.sdp };
  }

  async getStats(): Promise<RTCStatsReport> {
    return new Map(this.stats.map((s, i) => [String(i), s])) as unknown as RTCStatsReport;
  }

  getReceivers(): RTCRtpReceiver[] {
    return [];
  }

  close(): void {
    this.closed = true;
  }

  emitTrack(track: MediaStreamTrack): void {
    const event = new Event('track') as Event & { track: MediaStreamTrack };
    event.track = track;
    this.dispatchEvent(event);
  }

  setIce(state: RTCIceConnectionState): void {
    this.iceConnectionState = state;
    this.dispatchEvent(new Event('iceconnectionstatechange'));
  }
}

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string;
  keepalive: boolean;
}

function harness(options: { status?: number; location?: string | null; gathers?: boolean } = {}) {
  let status = options.status ?? 201;
  const peers: FakePeer[] = [];
  const calls: Call[] = [];
  const timers = new Map<number, { fn: () => void; ms: number }>();
  let nextTimer = 0;
  const videos = [document.createElement('video'), document.createElement('video')];
  const frames = [0, 0];
  const events = {
    onLive: vi.fn(),
    onWaiting: vi.fn(),
    onTokenExpiring: vi.fn(),
    onFailed: vi.fn(),
    onConnectionLost: vi.fn(),
    onPublishers: vi.fn(),
    onActive: vi.fn(),
    onTracks: vi.fn(),
    onRenewed: vi.fn(),
  } satisfies PlayerEvents;
  let renewTicket = 'v1.ticket-two.sig';
  const lease = {
    renew: vi.fn(async (_id: string) => relaySession(renewTicket)),
    detach: vi.fn(async (_id: string) => undefined),
  } satisfies RelayLeaseApi;
  const location = options.location === undefined ? '/r/abc/CAM/whep/secret-1' : options.location;
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({
      url,
      method: init?.method ?? 'GET',
      headers: Object.fromEntries(Object.entries((init?.headers ?? {}) as Record<string, string>)),
      body: String(init?.body ?? ''),
      keepalive: init?.keepalive === true,
    });
    const camera = url.split('/').at(-2) ?? '';
    const headers = new Headers();
    if (location) headers.set('Location', location.replace('CAM', camera));
    return {
      status: init?.method === 'DELETE' ? 200 : status,
      headers,
      text: async () => `v=0 answer ${camera}`,
    } as unknown as Response;
  });
  const player = new WhepPlayer((slot) => videos[slot] ?? null, events, {
    lease,
    fetch: fetch as unknown as typeof globalThis.fetch,
    createPeer: () => {
      const peer = new FakePeer();
      peer.gathers = options.gathers ?? true;
      peers.push(peer);
      return peer as unknown as PeerConnection;
    },
    origin: () => ORIGIN,
    frames: (video) => frames[videos.indexOf(video)] ?? null,
    stream: (tracks) => ({ getTracks: () => tracks }) as unknown as MediaStream,
    now: () => NOW,
    setTimer: (fn, ms) => {
      nextTimer += 1;
      timers.set(nextTimer, { fn, ms });
      return nextTimer;
    },
    clearTimer: (handle) => void timers.delete(handle as number),
  });
  /** Run (once) every pending timer of exactly `ms`. */
  const fire = async (ms: number): Promise<void> => {
    for (const [id, timer] of [...timers]) {
      if (timer.ms !== ms) continue;
      timers.delete(id);
      timer.fn();
    }
    await flush();
  };
  return {
    player,
    peers,
    calls,
    timers,
    videos,
    frames,
    events,
    lease,
    fetch,
    fire,
    setRenewTicket: (ticket: string) => (renewTicket = ticket),
    setStatus: (next: number) => (status = next),
  };
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

const track = (): MediaStreamTrack =>
  ({ kind: 'video', id: 't', stop: vi.fn() }) as unknown as MediaStreamTrack;

describe('resolveLocation', () => {
  const endpoint = '/media/whep/r/abc/front_left/whep';

  it('puts the gateway prefix back on a Location relative to the media server root', () => {
    expect(resolveLocation('/r/abc/front_left/whep/s1', endpoint, ORIGIN)).toBe(
      `${ORIGIN}/media/whep/r/abc/front_left/whep/s1`,
    );
  });

  it('keeps a Location that already carries the prefix, or is absolute', () => {
    expect(resolveLocation('/media/whep/r/abc/front_left/whep/s1', endpoint, ORIGIN)).toBe(
      `${ORIGIN}/media/whep/r/abc/front_left/whep/s1`,
    );
    expect(resolveLocation('https://relay.example/x/s1', endpoint, ORIGIN)).toBe(
      'https://relay.example/x/s1',
    );
  });

  it('resolves anything else like a link against the endpoint', () => {
    expect(resolveLocation('s1', endpoint, ORIGIN)).toBe(
      `${ORIGIN}/media/whep/r/abc/front_left/s1`,
    );
    expect(resolveLocation('/elsewhere/s1', endpoint, ORIGIN)).toBe(`${ORIGIN}/elsewhere/s1`);
    expect(resolveLocation('/r/abc/front_left/whep/s1', `${ORIGIN}${endpoint}`, 'http://x')).toBe(
      `${ORIGIN}/media/whep/r/abc/front_left/whep/s1`,
    );
  });

  it('does not take a mere prefix of a segment for the overlap', () => {
    // `/whep` of `/media/whep` is not the start of `/whepx/...`
    expect(resolveLocation('/whepx/s1', '/media/whep', ORIGIN)).toBe(`${ORIGIN}/whepx/s1`);
  });
});

describe('WhepPlayer', () => {
  it('offers recvonly video, POSTs the gathered offer with the ticket and applies the answer', async () => {
    const h = harness();
    await h.player.start(relaySession(), 1);
    expect(h.peers).toHaveLength(1);
    const peer = h.peers[0]!;
    expect(peer.transceivers).toEqual([{ kind: 'video', init: { direction: 'recvonly' } }]);
    const [post] = h.calls;
    expect(post).toMatchObject({
      url: '/media/whep/r/abc/front_left/whep',
      method: 'POST',
      body: 'v=0 offer +candidates',
    });
    expect(post!.headers).toEqual({
      'Content-Type': 'application/sdp',
      Authorization: `Bearer ${TICKET}`,
    });
    expect(peer.remote).toEqual({ type: 'answer', sdp: 'v=0 answer front_left' });
    expect(h.events.onPublishers).toHaveBeenCalledWith([1, 2]);
    expect(h.events.onActive).toHaveBeenCalledWith(1);
  });

  it('sends the offer after the gathering timeout when ICE gathering never completes', async () => {
    const h = harness({ gathers: false });
    const started = h.player.start(relaySession(), 1);
    await flush();
    expect(h.calls).toEqual([]);
    await h.fire(GATHER_TIMEOUT_MS);
    await started;
    expect(h.calls.map((c) => c.method)).toEqual(['POST']);
  });

  it('is live at the first presented frame of the received track', async () => {
    const h = harness();
    await h.player.start(relaySession(), null);
    h.peers[0]!.emitTrack(track());
    await flush();
    expect(h.events.onLive).not.toHaveBeenCalled();
    expect(h.videos[0]!.muted).toBe(true);
    expect(h.videos[0]!.playsInline).toBe(true);
    h.frames[0] = 1;
    await h.fire(100);
    expect(h.events.onLive).toHaveBeenCalledTimes(1);
    expect(h.events.onTracks).toHaveBeenLastCalledWith([1]);
  });

  it('plays both front cameras on two peer connections, one per tile', async () => {
    const h = harness();
    await h.player.start(relaySession(), [1, 2]);
    expect(h.peers).toHaveLength(2);
    expect(h.calls.map((c) => c.url)).toEqual([
      '/media/whep/r/abc/front_left/whep',
      '/media/whep/r/abc/front_right/whep',
    ]);
    h.peers[0]!.emitTrack(track());
    h.peers[1]!.emitTrack(track());
    h.frames[0] = 3;
    h.frames[1] = 2;
    await flush();
    await h.fire(100);
    expect(h.events.onTracks).toHaveBeenLastCalledWith([1, 2]);
  });

  it('keeps the connection of a tile whose camera stays the same', async () => {
    const h = harness();
    await h.player.start(relaySession(), null);
    await h.player.select([1, 2]);
    expect(h.peers).toHaveLength(2);
    expect(h.peers[0]!.closed).toBe(false);
    expect(h.calls.filter((c) => c.method === 'DELETE')).toEqual([]);
    await h.player.select(1);
    expect(h.peers[1]!.closed).toBe(true);
    expect(h.peers[0]!.closed).toBe(false);
  });

  it('a camera the relay does not serve gets no connection', async () => {
    const h = harness();
    await h.player.start(relaySession(), 7);
    expect(h.peers).toEqual([]);
    expect(h.events.onActive).toHaveBeenCalledWith(null);
  });

  it('deletes the WHEP sessions under the gateway prefix and detaches the lease on stop', async () => {
    const h = harness();
    await h.player.start(relaySession(), [1, 2]);
    await h.player.stop();
    const deletes = h.calls.filter((c) => c.method === 'DELETE');
    expect(deletes.map((c) => c.url).sort()).toEqual([
      `${ORIGIN}/media/whep/r/abc/front_left/whep/secret-1`,
      `${ORIGIN}/media/whep/r/abc/front_right/whep/secret-1`,
    ]);
    expect(deletes.every((c) => c.keepalive)).toBe(true);
    expect(h.peers.every((p) => p.closed)).toBe(true);
    expect(h.lease.detach).toHaveBeenCalledExactlyOnceWith(LEASE);
    // Idempotent, and the heartbeat is gone.
    await h.player.stop();
    expect(h.lease.detach).toHaveBeenCalledTimes(1);
    expect([...h.timers.values()].some((t) => t.ms === 15_000)).toBe(false);
  });

  it('renews the lease every heartbeat and uses the fresh ticket for the next offer', async () => {
    const h = harness();
    await h.player.start(relaySession(), 1);
    expect([...h.timers.values()].map((t) => t.ms)).toContain(15_000);
    await h.fire(15_000);
    expect(h.lease.renew).toHaveBeenCalledExactlyOnceWith(LEASE);
    expect(h.events.onRenewed).toHaveBeenCalledTimes(1);
    expect([...h.timers.values()].map((t) => t.ms)).toContain(15_000); // the next beat
    await h.player.select(2);
    const post = h.calls.filter((c) => c.method === 'POST').at(-1)!;
    expect(post.url).toBe('/media/whep/r/abc/front_right/whep');
    expect(post.headers.Authorization).toBe('Bearer v1.ticket-two.sig');
    // The tile that showed camera 1 was closed and its WHEP session deleted.
    expect(h.peers[0]!.closed).toBe(true);
    expect(h.calls.some((c) => c.method === 'DELETE' && c.url.includes('front_left'))).toBe(true);
    await h.fire(15_000);
    expect(h.lease.renew).toHaveBeenCalledTimes(2);
  });

  it('leaves cameras the relay reports as not available out of the publishers', async () => {
    const h = harness();
    const listed = relaySession();
    listed.cameras = [
      ...(listed.cameras ?? []),
      { id: 'rear', name: 'Rear', uid: 3, placement: 'rear' },
    ];
    listed.relay!.tiles.push({ camera_id: 'rear', whep_url: '/media/whep/r/abc/rear/whep' });
    const known = structuredClone(listed);
    for (const tile of known.relay!.tiles) tile.available = tile.camera_id !== 'rear';
    h.lease.renew.mockResolvedValue(known);
    await h.player.start(listed, null);
    expect(h.events.onPublishers).toHaveBeenLastCalledWith([1, 2, 3]); // not known yet
    await h.fire(15_000);
    // The vendor lists a rear camera the mower never publishes: no longer offered.
    expect(h.events.onPublishers).toHaveBeenLastCalledWith([1, 2]);
  });

  it('a lease that is gone (404) is a lost connection; other renew errors retry', async () => {
    const h = harness();
    await h.player.start(relaySession(), 1);
    h.lease.renew.mockRejectedValueOnce(ApiProblem.network());
    await h.fire(15_000);
    expect(h.events.onConnectionLost).not.toHaveBeenCalled();
    h.lease.renew.mockRejectedValueOnce(new ApiProblem({ status: 404 }));
    await h.fire(15_000);
    expect(h.events.onConnectionLost).toHaveBeenCalledExactlyOnceWith('disconnected');
  });

  it('ICE failed is a lost connection at once, disconnected only after 5 s', async () => {
    const h = harness();
    await h.player.start(relaySession(), 1);
    const peer = h.peers[0]!;
    peer.setIce('connected');
    peer.setIce('disconnected');
    expect(h.events.onConnectionLost).not.toHaveBeenCalled();
    peer.setIce('connected'); // back within the limit
    await h.fire(DISCONNECTED_LIMIT_MS);
    expect(h.events.onConnectionLost).not.toHaveBeenCalled();
    peer.setIce('disconnected');
    await h.fire(DISCONNECTED_LIMIT_MS);
    expect(h.events.onConnectionLost).toHaveBeenCalledExactlyOnceWith('disconnected');

    const other = harness();
    await other.player.start(relaySession(), 1);
    other.peers[0]!.setIce('failed');
    expect(other.events.onConnectionLost).toHaveBeenCalledExactlyOnceWith('disconnected');
  });

  it('without automatic rejoin a lost connection fails the stream', async () => {
    const h = harness();
    await h.player.start(relaySession(), 1, {
      profile: 'rtc',
      latency: 1,
      fallback: true,
      dataSaver: false,
      autoRejoin: false,
      transport: 'relay',
    });
    h.peers[0]!.setIce('failed');
    expect(h.events.onFailed).toHaveBeenCalledWith('failed');
    expect(h.events.onConnectionLost).not.toHaveBeenCalled();
  });

  it('a refused WHEP offer is a lost connection (the viewer requests a new session)', async () => {
    const h = harness({ status: 401 });
    await h.player.start(relaySession(), 1);
    expect(h.peers[0]!.remote).toBeNull();
    expect(h.events.onConnectionLost).toHaveBeenCalledExactlyOnceWith('disconnected');
  });

  it('offers again while the starting source has nothing published yet (404)', async () => {
    const h = harness({ status: 404 });
    const started = h.player.start(relaySession(), 1);
    await flush();
    expect(h.calls).toHaveLength(1);
    await h.fire(WHEP_RETRY_MS);
    expect(h.calls).toHaveLength(2);
    expect(h.calls[1]!.body).toBe(h.calls[0]!.body); // the same offer
    h.setStatus(201);
    await h.fire(WHEP_RETRY_MS);
    await started;
    expect(h.calls.filter((c) => c.method === 'POST')).toHaveLength(3);
    expect(h.peers[0]!.remote?.type).toBe('answer');
    expect(h.events.onConnectionLost).not.toHaveBeenCalled();
  });

  it('gives up after 20 s of 404 answers', async () => {
    const h = harness({ status: 404 });
    const started = h.player.start(relaySession(), 1);
    for (let i = 0; i < WHEP_START_ATTEMPTS; i += 1) await h.fire(WHEP_RETRY_MS);
    await started;
    expect(h.calls).toHaveLength(WHEP_START_ATTEMPTS);
    expect(h.events.onConnectionLost).toHaveBeenCalledExactlyOnceWith('disconnected');
  });

  it('refuses a session that is not a relay session', async () => {
    const h = harness();
    await expect(
      h.player.start({ ...relaySession(), provider: 'agora', relay: null }, 1),
    ).rejects.toThrow('not a relay session');
  });

  it('takes a renewed session of the same lease, refuses another lease', async () => {
    const h = harness();
    await h.player.start(relaySession(), 1);
    await h.player.renew(relaySession('v1.ticket-three.sig'));
    expect(h.events.onRenewed).toHaveBeenCalledTimes(1);
    await expect(
      h.player.renew(relaySession(TICKET, '6f1c2a3b-0000-4000-8000-000000000002')),
    ).rejects.toThrow();
  });

  it('reports the receiver statistics in the diagnostics snapshot', async () => {
    const h = harness();
    await h.player.start(relaySession(), 1);
    h.peers[0]!.setIce('connected');
    h.peers[0]!.stats = [
      {
        type: 'inbound-rtp',
        kind: 'video',
        codecId: 'c1',
        bytesReceived: 30_000,
        packetsReceived: 90,
        packetsLost: 10,
        framesReceived: 100,
        framesDecoded: 98,
        keyFramesDecoded: 10,
        framesPerSecond: 10,
        frameWidth: 640,
        frameHeight: 480,
        pliCount: 1,
        nackCount: 3,
        freezeCount: 0,
        totalFreezesDuration: 0,
        jitterBufferDelay: 4,
        jitterBufferEmittedCount: 100,
      },
      { type: 'codec', id: 'c1', mimeType: 'video/H264', sdpFmtpLine: 'profile-level-id=42e01f' },
      { type: 'candidate-pair', nominated: true, state: 'succeeded', currentRoundTripTime: 0.004 },
    ];
    h.peers[0]!.emitTrack(track());
    h.frames[0] = 1;
    await flush();
    await h.fire(100);
    h.player.diagnostics!();
    await flush();
    const snapshot = h.player.diagnostics!();
    await flush();
    expect(snapshot).toMatchObject({
      state: 'connected',
      uid: 1,
      fps: 10,
      width: 640,
      height: 480,
      rttMs: 4,
      lossTotalPercent: 10,
      delayMs: 40,
      delaySource: 'receive',
    });
    expect(snapshot?.tracks.map((t) => t.uid)).toEqual([1]);
    expect(JSON.stringify(snapshot)).not.toContain('ticket-one');
    expect(JSON.stringify(snapshot)).not.toContain('secret-1');
  });
});

describe('CameraPlayerFactory (relay)', () => {
  const targets = (lease?: RelayLeaseApi) => ({
    canvas: () => document.createElement('canvas'),
    video: () => document.createElement('video'),
    source: {
      label: () => '',
      position: () => null,
      colors: () => ({ bars: [], background: '', ink: '', inkInverse: '', sweep: '' }),
    },
    lease,
    whep: { createPeer: () => new FakePeer() as unknown as PeerConnection },
    loadAgora: vi.fn(async () => ({
      createAgoraPlayer: () => ({ agora: true }) as unknown as CameraPlayer,
    })),
  });
  const events = {
    onLive: () => undefined,
    onWaiting: () => undefined,
    onTokenExpiring: () => undefined,
    onFailed: () => undefined,
  };

  it('plays relay sessions with the WHEP player and never loads the Agora SDK for them', async () => {
    const t = targets({ renew: async () => relaySession(), detach: async () => undefined });
    const player = await new CameraPlayerFactory(t).create(relaySession(), events);
    expect(player).toBeInstanceOf(WhepPlayer);
    expect(t.loadAgora).not.toHaveBeenCalled();
    const agora = await new CameraPlayerFactory(t).create(
      { ...relaySession(), provider: 'agora', relay: null },
      events,
    );
    expect(agora).toEqual({ agora: true });
  });

  it('needs the lease endpoints for a relay session', async () => {
    await expect(new CameraPlayerFactory(targets()).create(relaySession(), events)).rejects.toThrow(
      'no lease endpoints',
    );
  });
});
