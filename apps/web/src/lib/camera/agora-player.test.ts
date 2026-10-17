/**
 * The Agora player with both front cameras, on a fake SDK: every tile subscribes its own uid
 * into its own element and never falls back to another camera; the first publisher plays only
 * in the single view without a choice.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DEFAULT_OPTIONS } from './options';
import type { PlayerEvents } from './player';
import type { CameraSession } from './session';

type Handler = (...args: unknown[]) => void;

interface FakeUser {
  uid: number;
  hasVideo: boolean;
}

class FakeTrack {
  constructor(readonly uid: number) {}
  getUserId(): number {
    return this.uid;
  }
  clones = 0;
  getMediaStreamTrack(): MediaStreamTrack {
    const id = `track-${this.uid}`;
    return {
      id,
      clone: () => {
        this.clones += 1;
        return { id: `${id}-clone`, stop: () => undefined };
      },
    } as unknown as MediaStreamTrack;
  }
  getRTCRtpTransceiver(): { receiver: { transform: null; getStats(): Promise<unknown> } } {
    const uid = this.uid;
    return {
      receiver: {
        transform: null,
        getStats: async () =>
          new Map<string, unknown>([
            ['c', { id: 'c', type: 'codec', mimeType: 'video/H264' }],
            ['i', { type: 'inbound-rtp', kind: 'video', codecId: 'c', framesReceived: 90 + uid }],
          ]),
      },
    };
  }
}

class FakeClient {
  handlers = new Map<string, Handler>();
  remoteUsers: FakeUser[] = [];
  subscribed: number[] = [];
  unsubscribed: number[] = [];
  stats: Record<string, unknown> = {};
  on(event: string, handler: Handler): void {
    this.handlers.set(event, handler);
  }
  emit(event: string, ...args: unknown[]): void {
    this.handlers.get(event)?.(...args);
  }
  async join(): Promise<void> {}
  async leave(): Promise<void> {}
  removeAllListeners(): void {
    this.handlers.clear();
  }
  setEncryptionConfig(): void {}
  async setClientRole(): Promise<void> {}
  async renewToken(): Promise<void> {}
  async subscribe(user: FakeUser, kind: string): Promise<FakeTrack> {
    if (kind === 'video') this.subscribed.push(user.uid);
    return new FakeTrack(user.uid);
  }
  async unsubscribe(uid: number): Promise<void> {
    this.unsubscribed.push(uid);
  }
  async setStreamFallbackOption(): Promise<void> {}
  async setRemoteVideoStreamType(): Promise<void> {}
  getRemoteVideoStats(): Record<string, unknown> {
    return this.stats;
  }
  getRTCStats(): unknown {
    return { RTT: 30 };
  }
}

let client: FakeClient;
let clientConfig: unknown = null;
const sdkParams: [string, unknown][] = [];

vi.mock('agora-rtc-sdk-ng', () => ({
  default: {
    setLogLevel: () => undefined,
    disableLogUpload: () => undefined,
    checkSystemRequirements: () => true,
    createClient: (config: unknown) => {
      clientConfig = config;
      return client;
    },
    setParameter: (name: string, value: unknown) => void sdkParams.push([name, value]),
    onAutoplayFailed: undefined,
  },
}));

const { createAgoraPlayer } = await import('./agora-player');

const SESSION = {
  provider: 'agora',
  app_id: 'app',
  channel: 'ch',
  token: 't',
  uid: 7,
  cameras: [],
  encryption: null,
  expires_at: '2026-10-11T12:05:00Z',
  quota: null,
} as unknown as CameraSession;

class FakeStream {
  constructor(readonly tracks: { id: string }[]) {}
}

function events(): PlayerEvents & {
  tracks: number[][];
  active: (number | null)[];
  live: number;
  waiting: number;
} {
  const log = {
    tracks: [] as number[][],
    active: [] as (number | null)[],
    live: 0,
    waiting: 0,
    onLive: () => void (log.live += 1),
    onWaiting: () => void (log.waiting += 1),
    onTokenExpiring: () => undefined,
    onFailed: () => undefined,
    onTracks: (uids: number[]) => void log.tracks.push(uids),
    onActive: (uid: number | null) => void log.active.push(uid),
  };
  return log;
}

/** Let the player's event queue run. */
async function settle(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
}

function streamIds(video: HTMLVideoElement): string[] {
  const stream = video.srcObject as unknown as FakeStream | null;
  return stream ? stream.tracks.map((t) => t.id) : [];
}

describe('agora player with two tiles', () => {
  let videos: HTMLVideoElement[];

  beforeEach(() => {
    client = new FakeClient();
    vi.stubGlobal('MediaStream', FakeStream);
    videos = [document.createElement('video'), document.createElement('video')];
    for (const video of videos) {
      video.play = async () => undefined;
      video.pause = () => undefined;
    }
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows the left and right camera each in its own element', async () => {
    const log = events();
    const player = createAgoraPlayer((slot) => videos[slot] ?? null, log);
    await player.start(SESSION, [1, 2], DEFAULT_OPTIONS);
    const left = { uid: 1, hasVideo: true };
    const right = { uid: 2, hasVideo: true };
    client.remoteUsers = [left, right];
    client.emit('user-published', left, 'video');
    client.emit('user-published', right, 'video');
    await settle();
    expect(client.subscribed).toEqual([1, 2]);
    expect(streamIds(videos[0]!)).toEqual(['track-1']);
    expect(streamIds(videos[1]!)).toEqual(['track-2']);
    expect(log.tracks.at(-1)).toEqual([1, 2]);
    expect(log.active.at(-1)).toBe(1);
    expect(log.live).toBe(1);
    client.stats = { '1': { receiveFrameRate: 15 }, '2': { receiveFrameRate: 12 } };
    const snap = player.diagnostics?.();
    expect(snap?.tracks.map((t) => [t.uid, t.fps])).toEqual([
      [1, 15],
      [2, 12],
    ]);
    await player.stop();
    expect(videos[1]!.srcObject).toBeNull();
  });

  it('leaves the right tile empty when only the left camera publishes', async () => {
    const log = events();
    const player = createAgoraPlayer((slot) => videos[slot] ?? null, log);
    await player.start(SESSION, [1, 2], DEFAULT_OPTIONS);
    const left = { uid: 1, hasVideo: true };
    client.remoteUsers = [left];
    client.emit('user-published', left, 'video');
    await settle();
    expect(client.subscribed).toEqual([1]);
    expect(streamIds(videos[1]!)).toEqual([]);
    expect(log.tracks.at(-1)).toEqual([1]);
    // The right camera goes away again after it came: its tile empties, the left one stays.
    const right = { uid: 2, hasVideo: true };
    client.remoteUsers = [left, right];
    client.emit('user-published', right, 'video');
    await settle();
    expect(streamIds(videos[1]!)).toEqual(['track-2']);
    right.hasVideo = false;
    client.emit('user-unpublished', right, 'video');
    await settle();
    expect(streamIds(videos[1]!)).toEqual([]);
    expect(streamIds(videos[0]!)).toEqual(['track-1']);
    expect(log.tracks.at(-1)).toEqual([1]);
  });

  it('plays the first publisher without a choice and unsubscribes what no tile shows', async () => {
    const log = events();
    const player = createAgoraPlayer((slot) => videos[slot] ?? null, log);
    await player.start(SESSION, null, DEFAULT_OPTIONS);
    const left = { uid: 1, hasVideo: true };
    const right = { uid: 2, hasVideo: true };
    client.remoteUsers = [left, right];
    client.emit('user-published', left, 'video');
    client.emit('user-published', right, 'video');
    await settle();
    expect(client.subscribed).toEqual([1]);
    expect(log.active.at(-1)).toBe(1);
    // Both side by side, then back to the right camera alone.
    await player.select([1, 2]);
    expect(client.subscribed).toEqual([1, 2]);
    expect(streamIds(videos[1]!)).toEqual(['track-2']);
    await player.select(2);
    expect(streamIds(videos[0]!)).toEqual(['track-2']);
    expect(videos[1]!.srcObject).toBeNull();
    expect(client.unsubscribed).toEqual([1]);
    // A chosen camera that stops publishing: no picture, no switch to the other camera.
    right.hasVideo = false;
    client.emit('user-unpublished', right, 'video');
    await settle();
    expect(streamIds(videos[0]!)).toEqual([]);
    expect(log.active.at(-1)).toBeNull();
    expect(log.waiting).toBeGreaterThan(1);
  });
});

describe('agora player debugging', () => {
  let video: HTMLVideoElement;

  beforeEach(() => {
    client = new FakeClient();
    clientConfig = null;
    sdkParams.length = 0;
    vi.stubGlobal('MediaStream', FakeStream);
    video = document.createElement('video');
    video.play = async () => undefined;
    video.pause = () => undefined;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function startWith(experiments: Parameters<typeof createAgoraPlayer>[2]) {
    const player = createAgoraPlayer((slot) => (slot ? null : video), events(), experiments);
    await player.start(SESSION, null, DEFAULT_OPTIONS);
    const user = { uid: 1, hasVideo: true };
    client.remoteUsers = [user];
    client.emit('user-published', user, 'video');
    await settle();
    return player;
  }

  it('creates a vp8 client and plays the track itself without experiments', async () => {
    const player = await startWith({ codec: null, clone: false, fakeVideo: false, params: {} });
    expect(clientConfig).toEqual({ mode: 'rtc', codec: 'vp8' });
    expect(sdkParams).toEqual([]);
    expect(streamIds(video)).toEqual(['track-1']);
    client.stats = { '1': { codecType: 'H264', receiveFrameRate: 15, decodeFrameRate: 0 } };
    const report = await player.debugReport?.();
    expect(report?.codec).toBe('vp8');
    expect(report?.experiments).toBe('none');
    expect(report?.tracks).toHaveLength(1);
    const [track] = report!.tracks;
    expect(track?.uid).toBe(1);
    expect(track?.agora.codecType).toBe('H264');
    expect(track?.agora.decodeFrameRate).toBe(0);
    expect(track?.inbound?.codec).toBe('video/H264');
    expect(track?.inbound?.framesReceived).toBe(91);
    expect(track?.transform).toBe(false);
  });

  it('applies the codec, SDK parameters and the clone of the experiments', async () => {
    const player = await startWith({
      codec: 'h264',
      clone: true,
      fakeVideo: false,
      params: { ENABLE_ENCODED_TRANSFORM: false },
    });
    expect(clientConfig).toEqual({ mode: 'rtc', codec: 'h264' });
    expect(sdkParams).toEqual([['ENABLE_ENCODED_TRANSFORM', false]]);
    expect(streamIds(video)).toEqual(['track-1-clone']);
    const report = await player.debugReport?.();
    expect(report?.experiments).toBe('codec=h264 clone ENABLE_ENCODED_TRANSFORM=false');
  });
});
