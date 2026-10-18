/** Stability of the live camera: options, diagnostics, rejoin with backoff, option changes. */
import { describe, expect, it, vi } from 'vitest';

import {
  buildSnapshot,
  EventLog,
  lossToPercent,
  pickDelay,
  shown,
  summarizeInbound,
} from './diagnostics';
import {
  clearStaleKeys,
  DEFAULT_OPTIONS,
  loadOptions,
  MAX_REJOIN_ATTEMPTS,
  OPTIONS_KEY,
  parseOptions,
  rejoinDelayMs,
  saveOptions,
  STALE_KEYS,
  type CameraOptions,
} from './options';
import type { CameraPlayer, PlayerEvents } from './player';
import type { CameraSession } from './session';
import { CameraViewer, DIAGNOSTICS_INTERVAL_MS, type ViewerDeps } from './viewer.svelte';

const NOW = Date.parse('2026-05-01T10:00:00Z');

function session(token = 't1'): CameraSession {
  return {
    provider: 'agora',
    app_id: 'app',
    channel: 'ch',
    token,
    uid: 7,
    cameras: [{ id: 'front', name: 'Front', uid: 1 }],
    encryption: null,
    expires_at: new Date(NOW + 300_000).toISOString(),
    quota: null,
  } as CameraSession;
}

class Player implements CameraPlayer {
  started: CameraOptions[] = [];
  applied: CameraOptions[] = [];
  stopped = 0;
  constructor(
    readonly events: PlayerEvents,
    private readonly live: boolean,
  ) {}
  async start(_s: CameraSession, _uid: number | null, options?: CameraOptions): Promise<void> {
    this.started.push(options ?? DEFAULT_OPTIONS);
    if (this.live) this.events.onLive();
    else this.events.onWaiting();
  }
  async renew(): Promise<void> {}
  async select(): Promise<void> {}
  setMuted(): void {}
  async applyOptions(options: CameraOptions): Promise<void> {
    this.applied.push(options);
  }
  diagnostics() {
    return buildSnapshot({
      state: 'CONNECTED',
      uid: 1,
      remote: { receiveBitrate: 1_234_000, receiveFrameRate: 24.4, freezeRate: 0.05 },
      rtc: { RTT: 40 },
      log: new EventLog(() => NOW),
    });
  }
  async stop(): Promise<void> {
    this.stopped += 1;
  }
}

function harness(live = true, overrides: Partial<ViewerDeps> = {}) {
  const players: Player[] = [];
  const timers: { fn: () => void; ms: number; cleared: boolean }[] = [];
  const saved: CameraOptions[] = [];
  const request = vi.fn(async (_id: string) => session(`t${request.mock.calls.length}`));
  const viewer = new CameraViewer({
    request,
    createPlayer: async (_s, events) => {
      const player = new Player(events, live);
      players.push(player);
      return player;
    },
    secure: () => true,
    cameraEnabled: async () => true,
    now: () => NOW,
    setTimer: (fn, ms) => {
      const timer = { fn, ms, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimer: (handle) => {
      (handle as { cleared: boolean }).cleared = true;
    },
    options: { ...DEFAULT_OPTIONS },
    saveOptions: (o) => saved.push(o),
    ...overrides,
  });
  const pending = () => timers.filter((t) => !t.cleared);
  return { viewer, players, timers, pending, saved, request };
}

describe('camera options', () => {
  it('defaults to rtc, fallback on, data saver off, rejoin on, transport auto', () => {
    expect(DEFAULT_OPTIONS).toEqual({
      profile: 'rtc',
      latency: 1,
      fallback: true,
      dataSaver: false,
      autoRejoin: true,
      transport: 'auto',
    });
  });

  it('keeps valid fields and defaults the rest', () => {
    expect(parseOptions({ profile: 'live', latency: 2, fallback: false, dataSaver: 'x' })).toEqual({
      profile: 'live',
      latency: 2,
      fallback: false,
      dataSaver: false,
      autoRejoin: true,
      transport: 'auto',
    });
    expect(parseOptions(null)).toEqual(DEFAULT_OPTIONS);
    expect(parseOptions({ profile: 'p2p', latency: 9, transport: 'carrier-pigeon' })).toEqual(
      DEFAULT_OPTIONS,
    );
    expect(parseOptions({ transport: 'relay' }).transport).toBe('relay');
    expect(parseOptions({ transport: 'direct' }).transport).toBe('direct');
  });

  it('clears only the stale debug keys and survives broken storage', () => {
    expect(STALE_KEYS).toEqual(['mowdeck.cameraDebug', 'mowdeck.cameraExperiments']);
    const data = new Map<string, string>([
      ['mowdeck.cameraDebug', '1'],
      ['mowdeck.cameraExperiments', '{}'],
      [OPTIONS_KEY, '{}'],
    ]);
    clearStaleKeys({ removeItem: (k: string) => void data.delete(k) });
    expect([...data.keys()]).toEqual([OPTIONS_KEY]);
    const tried: string[] = [];
    expect(() =>
      clearStaleKeys({
        removeItem: (k: string) => {
          tried.push(k);
          throw new Error('blocked');
        },
      }),
    ).not.toThrow();
    expect(tried).toEqual([...STALE_KEYS]);
    expect(() => clearStaleKeys(null)).not.toThrow();
  });

  it('round-trips through storage and survives broken storage', () => {
    const data = new Map<string, string>();
    const storage = {
      getItem: (k: string) => data.get(k) ?? null,
      setItem: (k: string, v: string) => void data.set(k, v),
    };
    saveOptions({ ...DEFAULT_OPTIONS, profile: 'live' }, storage);
    expect(data.has(OPTIONS_KEY)).toBe(true);
    expect(loadOptions(storage).profile).toBe('live');
    data.set(OPTIONS_KEY, '{broken');
    expect(loadOptions(storage)).toEqual(DEFAULT_OPTIONS);
    expect(() =>
      saveOptions(DEFAULT_OPTIONS, {
        setItem: () => {
          throw new Error('quota');
        },
      }),
    ).not.toThrow();
  });

  it('backs off 1, 2, 4, 8, 16 s and then repeats', () => {
    expect([0, 1, 2, 3, 4, 5, 9].map(rejoinDelayMs)).toEqual([
      1000, 2000, 4000, 8000, 16000, 16000, 16000,
    ]);
  });
});

describe('diagnostics snapshot', () => {
  it('converts the SDK numbers and keeps the last three events', () => {
    let t = NOW;
    const log = new EventLog(() => (t += 1000));
    log.add('state', 'CONNECTED > RECONNECTING');
    log.add('published', '1 video');
    log.add('unpublished', '1 video');
    log.add('state', 'RECONNECTING > CONNECTED');
    const snap = buildSnapshot({
      state: 'CONNECTED',
      uid: 1,
      remote: {
        receiveBitrate: 1_234_000,
        receiveFrameRate: 24.4,
        receiveResolutionWidth: 1280,
        receiveResolutionHeight: 720,
        freezeRate: 0.05,
        totalFreezeTime: 3.14,
        packetLossRate: 0.328,
        currentPacketLossRate: 0.02,
        end2EndDelay: 410.6,
      },
      rtc: { RTT: 38 },
      log,
    });
    expect(snap).toMatchObject({
      bitrateKbps: 1234,
      fps: 24,
      width: 1280,
      height: 720,
      freezePercent: 5,
      freezeSeconds: 3.1,
      lossPercent: 2,
      lossTotalPercent: 32.8,
      delayMs: 411,
      delaySource: 'end2end',
      rttMs: 38,
      stateChanges: 2,
      published: 1,
      unpublished: 1,
    });
    expect(snap.recent.map((e) => e.text)).toEqual([
      '1 video',
      '1 video',
      'RECONNECTING > CONNECTED',
    ]);
    expect(snap.recent[0]?.at).toBeLessThan(snap.recent[2]?.at ?? 0);
  });

  it('shows unknown numbers as a dash', () => {
    const snap = buildSnapshot({
      state: 'DISCONNECTED',
      uid: null,
      remote: undefined,
      rtc: undefined,
      log: new EventLog(),
    });
    expect(snap.bitrateKbps).toBeNull();
    expect(shown(snap.bitrateKbps, ' kbit/s')).toBe('-');
    expect(shown(12, ' ms')).toBe('12 ms');
    expect(snap.delayMs).toBeNull();
    expect(snap.delaySource).toBeNull();
    expect(snap.lossPercent).toBeNull();
  });

  it('takes current loss as a fraction, keeps a percent, ignores bad values', () => {
    expect(lossToPercent(0.0123)).toBe(1.2);
    expect(lossToPercent(0)).toBe(0);
    expect(lossToPercent(1)).toBe(100);
    expect(lossToPercent(7.5)).toBe(7.5);
    expect(lossToPercent(-1)).toBeNull();
    expect(lossToPercent(null)).toBeNull();
  });

  it('prefers end-to-end delay and falls back when the SDK reports 0', () => {
    const rtc = { RTT: 44 };
    expect(pickDelay({ end2EndDelay: 300, receiveDelay: 100 }, rtc)).toEqual({
      ms: 300,
      source: 'end2end',
    });
    expect(pickDelay({ end2EndDelay: 0, receiveDelay: 120.4, transportDelay: 80 }, rtc)).toEqual({
      ms: 120,
      source: 'receive',
    });
    expect(pickDelay({ end2EndDelay: 0, receiveDelay: 0, transportDelay: 80 }, rtc)).toEqual({
      ms: 80,
      source: 'transport',
    });
    expect(pickDelay({ end2EndDelay: 0, transportDelay: 0 }, rtc)).toEqual({
      ms: 44,
      source: 'rtt',
    });
    expect(pickDelay({ end2EndDelay: 0 }, { RTT: 0 })).toEqual({ ms: null, source: null });
    expect(pickDelay(undefined, undefined)).toEqual({ ms: null, source: null });
  });

  it('reports the cumulative loss separately from the current one', () => {
    const snap = buildSnapshot({
      state: 'CONNECTED',
      uid: 1,
      remote: { packetLossRate: 0.328, currentPacketLossRate: 0 },
      rtc: undefined,
      log: new EventLog(),
    });
    expect(snap.lossPercent).toBe(0);
    expect(snap.lossTotalPercent).toBe(32.8);
  });
});

describe('inbound statistics', () => {
  it('reads the video entry of a WebRTC stats report', () => {
    const report = [
      { id: 'a', type: 'inbound-rtp', kind: 'audio', packetsReceived: 5 },
      {
        id: 'v',
        type: 'inbound-rtp',
        kind: 'video',
        packetsReceived: 300,
        packetsLost: 3,
        framesPerSecond: 12,
        frameWidth: 1280,
        frameHeight: 720,
        totalFreezesDuration: 0.5,
        jitterBufferDelay: 3,
        jitterBufferEmittedCount: 30,
      },
    ];
    expect(summarizeInbound(report)).toEqual({
      packetsReceived: 300,
      packetsLost: 3,
      framesPerSecond: 12,
      width: 1280,
      height: 720,
      freezeSeconds: 0.5,
      jitterMs: 100,
    });
    expect(summarizeInbound([report[0]!])).toBeNull();
    expect(
      summarizeInbound([{ type: 'inbound-rtp', mediaType: 'video', jitterBufferDelay: 1 }]),
    ).toMatchObject({ packetsReceived: null, jitterMs: null });
  });
});

describe('viewer: polling, options and rejoin', () => {
  it('polls the statistics every 2 s: per tile always, the full readout while watched', async () => {
    const { viewer, pending } = harness();
    await viewer.open('m1');
    expect(viewer.diagnostics).toBeNull();
    // Every tile shows its resolution and frame rate, so the numbers are read anyway.
    expect(viewer.tracks).toEqual([expect.objectContaining({ uid: 1, fps: 24 })]);
    viewer.watchDiagnostics(true);
    expect(viewer.diagnostics?.bitrateKbps).toBe(1234);
    const poll = pending().find((t) => t.ms === DIAGNOSTICS_INTERVAL_MS);
    expect(poll).toBeDefined();
    viewer.watchDiagnostics(false);
    expect(viewer.diagnostics).toBeNull();
    // One timer chain, still running for the tiles.
    expect(pending().filter((t) => t.ms === DIAGNOSTICS_INTERVAL_MS)).toHaveLength(1);
    await viewer.close();
    expect(pending().filter((t) => t.ms === DIAGNOSTICS_INTERVAL_MS)).toHaveLength(0);
    expect(viewer.tracks).toEqual([]);
  });

  it('never logs the statistics to the console, even with the old debug key set', async () => {
    localStorage.setItem('mowdeck.cameraDebug', '1');
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => undefined);
    const { viewer } = harness(true);
    await viewer.open('m1');
    expect(debug).not.toHaveBeenCalled();
    debug.mockRestore();
  });

  it('removes the stale debug keys from localStorage when a viewer starts', () => {
    localStorage.setItem('mowdeck.cameraDebug', '1');
    localStorage.setItem('mowdeck.cameraExperiments', '{"codec":"h264"}');
    localStorage.setItem(OPTIONS_KEY, JSON.stringify(DEFAULT_OPTIONS));
    harness(true);
    expect(localStorage.getItem('mowdeck.cameraDebug')).toBeNull();
    expect(localStorage.getItem('mowdeck.cameraExperiments')).toBeNull();
    expect(localStorage.getItem(OPTIONS_KEY)).not.toBeNull();
    localStorage.removeItem(OPTIONS_KEY);
  });

  it('applies fallback and data saver live, but rejoins for a new profile', async () => {
    const { viewer, players, saved, request } = harness();
    await viewer.open('m1');
    await viewer.setOptions({ dataSaver: true });
    expect(saved.at(-1)?.dataSaver).toBe(true);
    expect(players[0]?.applied.at(-1)?.dataSaver).toBe(true);
    expect(players).toHaveLength(1);
    await viewer.setOptions({ profile: 'live' });
    expect(saved.at(-1)?.profile).toBe('live');
    expect(players).toHaveLength(2);
    expect(players[1]?.started[0]?.profile).toBe('live');
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('joins again with a fresh session after DISCONNECTED, with backoff', async () => {
    const { viewer, players, pending, request } = harness(false);
    await viewer.open('m1');
    players[0]?.events.onConnectionLost?.('disconnected');
    expect(viewer.phase).toBe('connecting');
    let timer = pending().at(-1);
    expect(timer?.ms).toBe(1000);
    timer?.fn();
    await vi.waitFor(() => expect(players).toHaveLength(2));
    expect(request).toHaveBeenCalledTimes(2);
    expect(viewer.rejoins).toBe(1);
    expect(players[0]?.stopped).toBe(1);
    // never live: the next loss waits twice as long
    players[1]?.events.onConnectionLost?.('disconnected');
    timer = pending().at(-1);
    expect(timer?.ms).toBe(2000);
  });

  it('rejoins at once when RECONNECTING stayed stuck, and resets the backoff when live', async () => {
    const { viewer, players, pending } = harness(true);
    await viewer.open('m1');
    players[0]?.events.onConnectionLost?.('stuck');
    expect(pending().at(-1)?.ms).toBe(0);
    pending().at(-1)?.fn();
    await vi.waitFor(() => expect(players).toHaveLength(2));
    players[1]?.events.onConnectionLost?.('disconnected');
    expect(pending().at(-1)?.ms).toBe(1000);
  });

  it('gives up after the last attempt', async () => {
    const { viewer, players, pending } = harness(false);
    await viewer.open('m1');
    for (let i = 0; i < MAX_REJOIN_ATTEMPTS; i += 1) {
      players.at(-1)?.events.onConnectionLost?.('disconnected');
      const before = players.length;
      pending().at(-1)?.fn();
      await vi.waitFor(() => expect(players).toHaveLength(before + 1));
    }
    players.at(-1)?.events.onConnectionLost?.('disconnected');
    await vi.waitFor(() => expect(viewer.phase).toBe('error'));
    expect(viewer.error).toBe('failed');
  });

  it('does not rejoin after the viewer closed', async () => {
    const { viewer, players, pending, request } = harness(false);
    await viewer.open('m1');
    players[0]?.events.onConnectionLost?.('disconnected');
    const timer = pending().at(-1);
    await viewer.close();
    expect(timer?.cleared).toBe(true);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('renews the token through the player when the SDK announces the expiry', async () => {
    const renew = vi.fn(async () => undefined);
    const { viewer, players } = harness(true, {
      createPlayer: async (_s, events) => {
        const player = new Player(events, true);
        player.renew = renew;
        players.push(player);
        return player;
      },
    });
    await viewer.open('m1');
    players[0]?.events.onTokenExpiring();
    await vi.waitFor(() => expect(renew).toHaveBeenCalled());
    expect(viewer.renewals).toBe(1);
  });
});
