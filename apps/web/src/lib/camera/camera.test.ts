import { describe, expect, it, vi } from 'vitest';

import { ApiProblem, PROBLEM_PREFIX } from '$lib/api/problem';
import { m } from '$lib/paraglide/messages';

import { CameraPlayerFactory } from './factory';
import { cameraLabel, cameraOptions } from './labels';
import { attachTracks, detachTracks, videoPublishers, type TrackSource } from './media';
import { FakePlayer, type CameraPlayer, type PlayerEvents } from './player';
import {
  CameraError,
  MIN_RENEW_DELAY_MS,
  cameraErrorOf,
  cameraUid,
  decodeSalt,
  renewDelayMs,
  sameChannel,
  type CameraSession,
} from './session';
import {
  clockText,
  drawTestPattern,
  positionText,
  type PatternColors,
  type PatternContext,
} from './test-pattern';
import { CameraViewer, type ViewerDeps } from './viewer.svelte';

const NOW = Date.parse('2026-10-10T12:00:00Z');

function session(changes: Partial<CameraSession> = {}): CameraSession {
  return {
    provider: 'fake',
    app_id: '',
    channel: 'sim-1',
    token: '',
    uid: 0,
    cameras: [
      { id: 'front', name: 'Front', uid: 1 },
      { id: 'rear', name: 'Rear', uid: 2 },
    ],
    encryption: null,
    expires_at: new Date(NOW + 300_000).toISOString(),
    quota: null,
    ...changes,
  };
}

const COLORS: PatternColors = {
  bars: ['white', 'yellow', 'cyan'],
  background: 'grey',
  ink: 'black',
  inkInverse: 'white',
  sweep: 'blue',
};

function recordingContext(): PatternContext & { calls: string[]; texts: string[] } {
  const calls: string[] = [];
  const texts: string[] = [];
  return {
    calls,
    texts,
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 1,
    font: '',
    textAlign: 'start',
    textBaseline: 'alphabetic',
    fillRect: () => calls.push('fillRect'),
    strokeRect: () => calls.push('strokeRect'),
    fillText: (text: string) => {
      calls.push('fillText');
      texts.push(text);
    },
    beginPath: () => calls.push('beginPath'),
    arc: () => calls.push('arc'),
    stroke: () => calls.push('stroke'),
    moveTo: () => calls.push('moveTo'),
    lineTo: () => calls.push('lineTo'),
  };
}

class StubPlayer implements CameraPlayer {
  started: (number | null)[] = [];
  renewed: string[] = [];
  selected: (number | null)[] = [];
  muted: boolean[] = [];
  stopped = 0;
  failRenew = false;
  failStart: Error | null = null;

  constructor(readonly events: PlayerEvents) {}

  async start(_session: CameraSession, uid: number | null): Promise<void> {
    if (this.failStart) throw this.failStart;
    this.started.push(uid);
    this.events.onPublishers?.([1, 2]);
    this.events.onActive?.(uid ?? 1);
    this.events.onLive();
  }

  async renew(next: CameraSession): Promise<void> {
    if (this.failRenew) throw new Error('renew failed');
    this.renewed.push(next.token);
  }

  async select(uid: number | null): Promise<void> {
    this.selected.push(uid);
    this.events.onActive?.(uid);
  }

  setMuted(muted: boolean): void {
    this.muted.push(muted);
  }

  async stop(): Promise<void> {
    this.stopped += 1;
  }
}

function harness(overrides: Partial<ViewerDeps> = {}) {
  const players: StubPlayer[] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const request = vi.fn(async (_id: string) => session());
  const deps: ViewerDeps = {
    request,
    createPlayer: async (_session, events) => {
      const player = new StubPlayer(events);
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
    ...overrides,
  };
  return { viewer: new CameraViewer(deps), players, timers, request };
}

describe('camera session helpers', () => {
  it('renews a minute before the expiry, never sooner than 5 s', () => {
    expect(renewDelayMs(session(), NOW)).toBe(240_000);
    const short = session({ expires_at: new Date(NOW + 60_000).toISOString() });
    expect(renewDelayMs(short, NOW)).toBe(MIN_RENEW_DELAY_MS);
    const mid = session({ expires_at: new Date(NOW + 100_000).toISOString() });
    expect(renewDelayMs(mid, NOW)).toBe(40_000);
    expect(renewDelayMs({ expires_at: 'nonsense' }, NOW)).toBe(MIN_RENEW_DELAY_MS);
  });

  it('maps refusals to error kinds', () => {
    const problem = (status: number, slug = '') =>
      new ApiProblem({ status, type: slug ? `${PROBLEM_PREFIX}${slug}` : undefined });
    expect(cameraErrorOf(problem(409, 'camera-unsupported'))).toBe('unsupported');
    expect(cameraErrorOf(problem(403, 'feature-not-in-plan'))).toBe('not_in_plan');
    expect(cameraErrorOf(problem(404, 'not-found'))).toBe('not_found');
    expect(cameraErrorOf(problem(503, 'unavailable'))).toBe('unavailable');
    expect(cameraErrorOf(ApiProblem.network())).toBe('unavailable');
    expect(cameraErrorOf(problem(500))).toBe('failed');
  });

  it('decodes the salt and compares channels', () => {
    expect([...decodeSalt('AAEC/w==')]).toEqual([0, 1, 2, 255]);
    expect(cameraUid({ id: 'x', name: 'X', uid: 3 })).toBe(3);
    expect(cameraUid({ id: 'x', name: 'X' })).toBeNull();
    expect(cameraUid(undefined)).toBeNull();
    expect(sameChannel(session(), session({ token: 'new' }))).toBe(true);
    expect(sameChannel(session(), session({ channel: 'other' }))).toBe(false);
  });
});

describe('test pattern', () => {
  it('draws bars, the sweep, the clock, the label and the position', () => {
    const ctx = recordingContext();
    drawTestPattern(
      ctx,
      640,
      360,
      { now: NOW, label: 'Luba · Front', position: '48.13715, 11.57612', tick: 7 },
      COLORS,
    );
    expect(ctx.calls.filter((c) => c === 'fillRect').length).toBe(5); // background, 3 bars, band
    expect(ctx.calls).toContain('arc');
    expect(ctx.texts).toEqual([clockText(NOW), 'Luba · Front', '48.13715, 11.57612']);
  });

  it('leaves the position out without a fix and survives empty bar lists', () => {
    const ctx = recordingContext();
    drawTestPattern(
      ctx,
      320,
      180,
      { now: NOW, label: 'L', position: null, tick: 0 },
      {
        ...COLORS,
        bars: [],
      },
    );
    expect(ctx.texts).toHaveLength(2);
  });

  it('formats clock and position', () => {
    expect(clockText(new Date(2026, 9, 10, 7, 5, 9).getTime())).toBe('07:05:09');
    expect(positionText({ lat: 48.1371549, lon: 11.5761249 })).toBe('48.13715, 11.57612');
    expect(positionText(null)).toBeNull();
    expect(positionText({ lat: Number.NaN, lon: 1 })).toBeNull();
  });
});

describe('FakePlayer', () => {
  it('draws frames on the canvas until stopped', async () => {
    vi.useFakeTimers();
    try {
      const ctx = recordingContext();
      const canvas = document.createElement('canvas');
      canvas.getContext = (() => ctx) as unknown as HTMLCanvasElement['getContext'];
      const events = {
        onLive: vi.fn(),
        onWaiting: vi.fn(),
        onTokenExpiring: vi.fn(),
        onFailed: vi.fn(),
        onPublishers: vi.fn(),
        onActive: vi.fn(),
      };
      const player = new FakePlayer(
        canvas,
        { label: () => 'Sim', position: () => ({ lat: 1, lon: 2 }), colors: () => COLORS },
        events,
        () => NOW,
      );
      await player.start(session(), null);
      expect(events.onLive).toHaveBeenCalledOnce();
      // Every simulated camera publishes; without a choice the first one shows.
      expect(events.onPublishers).toHaveBeenCalledWith([1, 2]);
      expect(events.onActive).toHaveBeenLastCalledWith(1);
      vi.advanceTimersByTime(350);
      expect(Number(canvas.dataset.frames)).toBe(4);
      await player.select(2);
      expect(player.uid).toBe(2);
      expect(events.onActive).toHaveBeenLastCalledWith(2);
      await player.renew(session());
      expect(player.renewed).toBe(1);
      player.setMuted(false);
      await player.stop();
      const frames = canvas.dataset.frames;
      vi.advanceTimersByTime(500);
      expect(canvas.dataset.frames).toBe(frames);
      expect(ctx.texts).toContain('1.00000, 2.00000');
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps the last frame of a canvas marked to freeze', () => {
    const ctx = recordingContext();
    const canvas = document.createElement('canvas');
    canvas.getContext = (() => ctx) as unknown as HTMLCanvasElement['getContext'];
    const player = new FakePlayer(
      canvas,
      { label: () => '', position: () => null, colors: () => COLORS },
      { onLive() {}, onWaiting() {}, onTokenExpiring() {}, onFailed() {} },
    );
    player.draw();
    expect(canvas.dataset.frames).toBe('1');
    canvas.dataset.freeze = 'true';
    player.draw();
    player.draw();
    expect(canvas.dataset.frames).toBe('1');
    delete canvas.dataset.freeze;
    player.draw();
    expect(canvas.dataset.frames).toBe('4');
  });

  it('plays the pattern through the tile video as a stream when asked', async () => {
    const ctx = recordingContext();
    const canvas = document.createElement('canvas');
    canvas.getContext = (() => ctx) as unknown as HTMLCanvasElement['getContext'];
    const stopped: string[] = [];
    const stream = { getTracks: () => [{ stop: () => void stopped.push('v') }] };
    let captures = 0;
    canvas.captureStream = (() => {
      captures += 1;
      return stream;
    }) as unknown as HTMLCanvasElement['captureStream'];
    const video = document.createElement('video');
    video.play = async () => undefined;
    const player = new FakePlayer(
      canvas,
      { label: () => '', position: () => null, colors: () => COLORS },
      { onLive() {}, onWaiting() {}, onTokenExpiring() {}, onFailed() {} },
      undefined,
      (slot) => (slot ? null : video),
    );
    player.draw();
    player.draw();
    // One stream per canvas, however often it draws.
    expect(captures).toBe(1);
    expect(video.srcObject).toBe(stream);
    expect(video.muted).toBe(true);
    await player.stop();
    expect(video.srcObject).toBeNull();
    expect(stopped).toEqual(['v']);
  });

  it('does nothing without a 2D context', () => {
    const canvas = document.createElement('canvas');
    canvas.getContext = (() => null) as unknown as HTMLCanvasElement['getContext'];
    const player = new FakePlayer(
      canvas,
      { label: () => '', position: () => null, colors: () => COLORS },
      { onLive() {}, onWaiting() {}, onTokenExpiring() {}, onFailed() {} },
    );
    player.draw();
    expect(canvas.dataset.frames).toBeUndefined();
  });
});

describe('CameraPlayerFactory', () => {
  const events: PlayerEvents = {
    onLive() {},
    onWaiting() {},
    onTokenExpiring() {},
    onFailed() {},
  };
  const source = { label: () => '', position: () => null, colors: () => COLORS };

  it('uses the test pattern for fake sessions and loads Agora for real ones', async () => {
    const canvas = document.createElement('canvas');
    const video = document.createElement('video');
    const agora = new StubPlayer(events);
    const loadAgora = vi.fn(async () => ({ createAgoraPlayer: () => agora }));
    const factory = new CameraPlayerFactory({
      canvas: () => canvas,
      video: () => video,
      source,
      loadAgora,
    });
    expect(await factory.create(session(), events)).toBeInstanceOf(FakePlayer);
    expect(loadAgora).not.toHaveBeenCalled();
    expect(await factory.create(session({ provider: 'agora' }), events)).toBe(agora);
    expect(loadAgora).toHaveBeenCalledOnce();
  });

  it('reports missing elements and a failed SDK load', async () => {
    const empty = new CameraPlayerFactory({ canvas: () => null, video: () => null, source });
    await expect(empty.create(session(), events)).rejects.toBeInstanceOf(CameraError);
    await expect(empty.create(session({ provider: 'agora' }), events)).rejects.toBeInstanceOf(
      CameraError,
    );
    const broken = new CameraPlayerFactory({
      canvas: () => null,
      video: () => document.createElement('video'),
      source,
      loadAgora: async () => Promise.reject(new Error('chunk failed')),
    });
    await expect(broken.create(session({ provider: 'agora' }), events)).rejects.toMatchObject({
      kind: 'browser',
    });
  });
});

describe('CameraViewer', () => {
  it('opens a session, plays the first publisher muted and schedules the renewal', async () => {
    const { viewer, players, timers, request } = harness();
    await viewer.open('m1');
    expect(request).toHaveBeenCalledWith('m1');
    expect(viewer.phase).toBe('live');
    // No choice yet: the player shows the first camera that publishes video.
    expect(viewer.cameraUid).toBeNull();
    expect(viewer.activeUid).toBe(1);
    expect(viewer.publishing).toEqual([1, 2]);
    expect(viewer.missing).toBe(false);
    expect(viewer.cameras.map((c) => c.id)).toEqual(['front', 'rear']);
    expect(players[0]?.started).toEqual([null]);
    expect(players[0]?.muted).toEqual([true]);
    expect(timers[0]?.ms).toBe(240_000);
  });

  it('renews the token on the same channel and rejoins on another', async () => {
    const { viewer, players, timers, request } = harness();
    await viewer.open('m1');
    request.mockResolvedValueOnce(session({ token: 'second' }));
    timers[0]?.fn();
    await vi.waitFor(() => expect(viewer.renewals).toBe(1));
    expect(players[0]?.renewed).toEqual(['second']);
    expect(viewer.session?.token).toBe('second');
    request.mockResolvedValueOnce(session({ channel: 'moved' }));
    await viewer.refresh();
    expect(players).toHaveLength(2);
    expect(players[0]?.stopped).toBe(1);
    expect(viewer.session?.channel).toBe('moved');
    expect(viewer.phase).toBe('live');
  });

  it('rejoins when the renewal fails and renews when the player asks', async () => {
    const { viewer, players, request } = harness();
    await viewer.open('m1');
    players[0]!.failRenew = true;
    await viewer.refresh();
    expect(players).toHaveLength(2);
    request.mockResolvedValueOnce(session({ token: 'third' }));
    players[1]!.events.onTokenExpiring();
    await vi.waitFor(() => expect(players[1]?.renewed).toEqual(['third']));
  });

  it('switches cameras and sound', async () => {
    const { viewer, players } = harness();
    await viewer.open('m1');
    await viewer.select(2);
    expect(viewer.cameraUid).toBe(2);
    expect(viewer.activeUid).toBe(2);
    expect(players[0]?.selected).toEqual([2]);
    viewer.toggleMute();
    expect(viewer.muted).toBe(false);
    expect(players[0]?.muted).toEqual([true, false]);
  });

  it('turns refusals into error kinds with the reference', async () => {
    const refused = new ApiProblem({
      status: 409,
      type: `${PROBLEM_PREFIX}camera-unsupported`,
      correlationId: 'corr-1',
    });
    const { viewer, players } = harness({ request: async () => Promise.reject(refused) });
    await viewer.open('m1');
    expect(viewer.phase).toBe('error');
    expect(viewer.error).toBe('unsupported');
    expect(viewer.correlationId).toBe('corr-1');
    expect(players).toHaveLength(0);
  });

  it('refuses real video without a secure context or with the switch off', async () => {
    const agora = session({ provider: 'agora', app_id: 'app', token: 't' });
    const insecure = harness({ request: async () => agora, secure: () => false });
    await insecure.viewer.open('m1');
    expect(insecure.viewer.error).toBe('insecure');
    const disabled = harness({ request: async () => agora, cameraEnabled: async () => false });
    await disabled.viewer.open('m1');
    expect(disabled.viewer.error).toBe('disabled');
    const allowed = harness({ request: async () => agora });
    await allowed.viewer.open('m1');
    expect(allowed.viewer.phase).toBe('live');
  });

  it('reports a player that cannot start and a stream that ends', async () => {
    const failing = harness({
      createPlayer: async (_s, events) => {
        const player = new StubPlayer(events);
        player.failStart = new CameraError('browser');
        return player;
      },
    });
    await failing.viewer.open('m1');
    expect(failing.viewer.error).toBe('browser');
    const { viewer, players } = harness();
    await viewer.open('m1');
    players[0]!.events.onWaiting();
    expect(viewer.phase).toBe('waiting');
    players[0]!.events.onFailed('expired');
    await vi.waitFor(() => expect(viewer.error).toBe('expired'));
    expect(players[0]?.stopped).toBe(1);
  });

  it('closes cleanly and ignores late answers', async () => {
    let resolve: (value: CameraSession) => void = () => undefined;
    const pending = new Promise<CameraSession>((r) => (resolve = r));
    const { viewer, players } = harness({ request: () => pending });
    const opening = viewer.open('m1');
    await viewer.close();
    resolve(session());
    await opening;
    expect(viewer.phase).toBe('closed');
    expect(players).toHaveLength(0);
    await viewer.refresh(); // nothing to refresh
    expect(viewer.session).toBeNull();
  });

  it('stops the player on close', async () => {
    const { viewer, players } = harness();
    await viewer.open('m1');
    await viewer.close();
    expect(players[0]?.stopped).toBe(1);
    expect(viewer.phase).toBe('closed');
    expect(viewer.publishing).toBeNull();
    expect(viewer.activeUid).toBeNull();
  });

  it('calls a chosen camera that stops publishing missing instead of switching', async () => {
    const { viewer, players } = harness();
    await viewer.open('m1');
    await viewer.select(2);
    expect(viewer.missing).toBe(false);
    const events = players[0]!.events;
    events.onPublishers?.([1]);
    events.onActive?.(null);
    expect(viewer.missing).toBe(true);
    expect(viewer.activeUid).toBeNull();
    expect(viewer.cameraUid).toBe(2);
    // Nobody publishes at all: that is waiting, not a missing camera.
    events.onPublishers?.([]);
    expect(viewer.missing).toBe(false);
    events.onPublishers?.([1, 2]);
    expect(viewer.missing).toBe(false);
  });
});

/** A fake remote track: what `getMediaStreamTrack()` hands out. */
function track(id: string): TrackSource & { media: MediaStreamTrack } {
  const media = { id, kind: id.startsWith('a') ? 'audio' : 'video' } as MediaStreamTrack;
  return { media, getMediaStreamTrack: () => media };
}

/** A fake `<video>` that records what the player does with it. */
function fakeVideo(failPlay = false) {
  const calls: string[] = [];
  const attributes: Record<string, string> = {};
  const listeners = new Map<string, (() => void)[]>();
  const video = {
    addEventListener: (name: string, listener: () => void) =>
      listeners.set(name, [...(listeners.get(name) ?? []), listener]),
    fire: (name: string) => listeners.get(name)?.forEach((listener) => listener()),
    listeners,
    muted: false,
    playsInline: false,
    srcObject: null as MediaStream | null,
    setAttribute: (name: string, value: string) => (attributes[name] = value),
    play: vi.fn(async () => {
      calls.push('play');
      if (failPlay) throw new Error('NotAllowedError');
    }),
    pause: vi.fn(() => calls.push('pause')),
  };
  return { video, el: video as unknown as HTMLVideoElement, calls, attributes };
}

describe('media attach and detach', () => {
  const stream = (tracks: MediaStreamTrack[]) => ({ tracks }) as unknown as MediaStream;

  it('puts the raw tracks into our element, muted and inline, and plays it', async () => {
    const { video, el, calls, attributes } = fakeVideo();
    const v = track('v1');
    const a = track('a1');
    await attachTracks(el, [v, null, a], true, stream);
    expect((video.srcObject as unknown as { tracks: MediaStreamTrack[] }).tracks).toEqual([
      v.media,
      a.media,
    ]);
    expect(video.muted).toBe(true);
    expect(video.playsInline).toBe(true);
    expect(attributes.playsinline).toBe('');
    expect(calls).toEqual(['play']);
  });

  it('survives a refused autoplay and empties the element on detach', async () => {
    const { video, el, calls } = fakeVideo(true);
    await expect(attachTracks(el, [track('v1')], false, stream)).resolves.toBeUndefined();
    expect(video.muted).toBe(false);
    detachTracks(el);
    expect(video.srcObject).toBeNull();
    expect(calls).toEqual(['play', 'pause']);
  });

  it('resumes a live picture the browser paused, at most once a second', async () => {
    const { video, el, calls } = fakeVideo();
    let now = 10_000;
    await attachTracks(el, [track('v1')], true, stream, () => now);
    // A second attach does not add a second listener.
    await attachTracks(el, [track('v1')], true, stream, () => now);
    expect(video.listeners.get('pause')).toHaveLength(1);
    calls.length = 0;
    video.fire('pause');
    await Promise.resolve();
    await Promise.resolve();
    expect(calls).toEqual(['play']);
    now += 500;
    video.fire('pause');
    await Promise.resolve();
    await Promise.resolve();
    expect(calls).toEqual(['play']);
    now += 600;
    video.fire('pause');
    await Promise.resolve();
    await Promise.resolve();
    expect(calls).toEqual(['play', 'play']);
    // Our own pause on detach is never undone.
    now += 5_000;
    detachTracks(el);
    video.fire('pause');
    await Promise.resolve();
    await Promise.resolve();
    expect(calls).toEqual(['play', 'play', 'pause']);
  });

  it('lists the uids that publish video', () => {
    const users = [
      { uid: 1, hasVideo: true },
      { uid: 2, hasVideo: false },
      { uid: '3', hasVideo: true },
      { uid: 'viewer', hasVideo: true },
    ];
    expect(videoPublishers(users)).toEqual([1, 3]);
    expect(videoPublishers(users, 1)).toEqual([3]);
  });
});

describe('camera labels and picker', () => {
  const cameras = [
    { id: '0', name: 'Front left', uid: 1 },
    { id: '1', name: 'Front right', uid: 2 },
    { id: '2', name: 'Rear', uid: 3 },
  ];

  it('names the cameras from their ids', () => {
    expect(cameraLabel(cameras[0]!)).toBe(m.camera_front_left());
    expect(cameraLabel(cameras[1]!)).toBe(m.camera_front_right());
    expect(cameraLabel(cameras[2]!)).toBe(m.camera_rear());
    expect(cameraLabel({ id: 'rear', name: 'Rear' })).toBe(m.camera_rear());
    expect(cameraLabel({ id: 'front', name: 'Front' })).toBe(m.camera_front());
    expect(cameraLabel({ id: 'roof', name: 'Roof' })).toBe('Roof');
  });

  it('lists only publishing cameras, keeps a missing choice and adds unknown publishers', () => {
    expect(cameraOptions(cameras, [1, 2], null).map((o) => o.value)).toEqual(['1', '2']);
    expect(cameraOptions(cameras, [1], 3).map((o) => o.value)).toEqual(['1', '3']);
    expect(cameraOptions(cameras, null, null)).toHaveLength(3);
    expect(cameraOptions(cameras, [1, 9], null)).toEqual([
      { value: '1', label: m.camera_front_left() },
      { value: '9', label: m.camera_unknown({ uid: 9 }) },
    ]);
    expect(cameraOptions([{ id: 'x', name: 'X' }], [], null)).toEqual([]);
  });
});
