/** Both front cameras: pairing, the layouts, per-track numbers, duplicate pictures, the viewer. */
import { describe, expect, it, vi } from 'vitest';

import { m } from '$lib/paraglide/messages';

import { buildSnapshot, EventLog, trackSnapshot, trackSummary } from './diagnostics';
import { BOTH, cameraLabel, cameraOptions } from './labels';
import {
  effectiveLayout,
  LAYOUT_KEY,
  layoutChoice,
  loadLayout,
  NO_DATA_MS,
  saveLayout,
  tilesOf,
  wantedUids,
  type CameraLayout,
  type TileInput,
} from './layout';
import { DEFAULT_OPTIONS } from './options';
import {
  FakePlayer,
  slotsOf,
  type CameraChoice,
  type CameraPlayer,
  type PlayerEvents,
} from './player';
import { meanAbsDifference, SAME_PICTURE_READINGS, SamePictureWatch } from './sameness';
import { placementOf, sideOf, stereoPair, type CameraInfo, type CameraSession } from './session';
import type { PatternContext } from './test-pattern';
import { CameraViewer } from './viewer.svelte';

const NOW = Date.parse('2026-10-11T12:00:00Z');

const LEFT: CameraInfo = { id: '0', name: 'Front left', uid: 1, placement: 'front_left' };
const RIGHT: CameraInfo = { id: '1', name: 'Front right', uid: 2, placement: 'front_right' };
const REAR: CameraInfo = { id: '2', name: 'Rear', uid: 3, placement: 'rear' };

function session(cameras: CameraInfo[] = [LEFT, RIGHT], provider = 'agora'): CameraSession {
  return {
    provider,
    app_id: 'app',
    channel: 'ch',
    token: 't',
    uid: 7,
    cameras,
    encryption: null,
    expires_at: new Date(NOW + 300_000).toISOString(),
    quota: null,
  } as CameraSession;
}

describe('stereo pair', () => {
  it('pairs the front left and front right camera by placement or well-known id', () => {
    expect(stereoPair([REAR, RIGHT, LEFT])).toEqual({ left: LEFT, right: RIGHT });
    // Older sessions without placement: Mammotion slot ids and the simulator's ids.
    const legacy = [
      { id: '0', name: 'Front left', uid: 1 },
      { id: '1', name: 'Front right', uid: 2 },
    ];
    expect(stereoPair(legacy)?.right.uid).toBe(2);
    expect(placementOf({ id: 'front_left' })).toBe('front_left');
    expect(placementOf({ id: 'x', placement: 'rear' })).toBe('rear');
    expect(placementOf({ id: 'x' })).toBeNull();
  });

  it('has no pair without both cameras, without uids or with one uid twice', () => {
    expect(stereoPair([LEFT])).toBeNull();
    expect(stereoPair([LEFT, { ...RIGHT, uid: null }])).toBeNull();
    expect(stereoPair([LEFT, { ...RIGHT, uid: 1 }])).toBeNull();
    expect(stereoPair([])).toBeNull();
  });

  it('names the side of a camera of the pair', () => {
    const pair = stereoPair([LEFT, RIGHT]);
    expect(sideOf(pair, 1)).toBe('left');
    expect(sideOf(pair, 2)).toBe('right');
    expect(sideOf(pair, 3)).toBeNull();
    expect(sideOf(null, 1)).toBeNull();
    expect(sideOf(pair, null)).toBeNull();
  });
});

describe('camera layout', () => {
  const pair = stereoPair([LEFT, RIGHT]);
  const base: TileInput = {
    layout: 'both',
    pair,
    cameras: [LEFT, RIGHT],
    cameraUid: null,
    activeUid: 1,
    publishing: [1, 2],
    live: [1, 2],
    stalled: [],
  };

  it('shows both front cameras side by side only with a pair', () => {
    expect(effectiveLayout('both', pair)).toBe('both');
    expect(effectiveLayout('both', null)).toBe('single');
    expect(layoutChoice('both', pair, 1)).toEqual([1, 2]);
    expect(layoutChoice('both', null, 1)).toBe(1);
    expect(layoutChoice('single', pair, null)).toBeNull();
    expect(wantedUids(null)).toEqual([]);
    expect(wantedUids(2)).toEqual([2]);
    expect(wantedUids([1, 2])).toEqual([1, 2]);
    expect(slotsOf([1, 2])).toEqual([1, 2]);
    expect(slotsOf(null)).toEqual([null]);
  });

  it('gives every tile its camera, side and status', () => {
    const tiles = tilesOf(base);
    expect(tiles.map((t) => [t.slot, t.uid, t.side, t.status])).toEqual([
      [0, 1, 'left', 'live'],
      [1, 2, 'right', 'live'],
    ]);
    expect(tiles[1]?.camera).toBe(RIGHT);
    // The right camera does not publish: missing, never the left picture instead.
    expect(tilesOf({ ...base, publishing: [1], live: [1] })[1]?.status).toBe('missing');
    // Published but nothing arrived in time: no data.
    expect(tilesOf({ ...base, live: [1], stalled: [2] })[1]?.status).toBe('no_data');
    expect(tilesOf({ ...base, live: [1] })[1]?.status).toBe('connecting');
  });

  it('has one tile in the single view (the choice, else the shown camera)', () => {
    const single = tilesOf({ ...base, layout: 'single', cameraUid: 2, live: [2] });
    expect(single).toHaveLength(1);
    expect(single[0]).toMatchObject({ uid: 2, side: 'right', status: 'live' });
    const first = tilesOf({ ...base, layout: 'single', cameraUid: null, activeUid: null });
    expect(first[0]).toMatchObject({ uid: null, status: 'connecting', camera: undefined });
    // Without a pair "both" falls back to the single view.
    expect(tilesOf({ ...base, pair: null })).toHaveLength(1);
  });

  it('remembers the layout per browser and survives broken storage', () => {
    const store = new Map<string, string>();
    const storage = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
    };
    expect(loadLayout(storage)).toBe('single');
    saveLayout('both', storage);
    expect(store.get(LAYOUT_KEY)).toBe('both');
    expect(loadLayout(storage)).toBe('both');
    const broken = {
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => {
        throw new Error('denied');
      },
    };
    expect(loadLayout(broken)).toBe('single');
    expect(() => saveLayout('both', broken)).not.toThrow();
    expect(loadLayout(null)).toBe('single');
  });
});

describe('camera labels and the both option', () => {
  it('names cameras by placement and offers "Both" with a pair', () => {
    expect(cameraLabel(LEFT)).toBe(m.camera_front_left());
    expect(cameraLabel({ id: 'front_right', name: 'x' })).toBe(m.camera_front_right());
    expect(cameraLabel({ id: '2', name: 'x' })).toBe(m.camera_rear());
    expect(cameraLabel({ id: 'odd', name: 'Odd' })).toBe('Odd');
    const options = cameraOptions([LEFT, RIGHT], [1, 2], null, true);
    expect(options.map((o) => o.value)).toEqual(['1', '2', BOTH]);
    expect(options.at(-1)?.label).toBe(m.camera_both());
    // Offered even while the right camera does not publish: its tile says so.
    expect(cameraOptions([LEFT, RIGHT], [1], null, true).map((o) => o.value)).toEqual(['1', BOTH]);
    expect(cameraOptions([LEFT, RIGHT], [1, 2], null).map((o) => o.value)).toEqual(['1', '2']);
  });
});

describe('per-track diagnostics', () => {
  it('keeps numbers per shown camera and summarises them for a tile', () => {
    const snap = buildSnapshot({
      state: 'CONNECTED',
      uid: 1,
      remote: { receiveFrameRate: 15, receiveResolutionWidth: 1280, receiveResolutionHeight: 720 },
      rtc: { RTT: 40 },
      log: new EventLog(() => NOW),
      tracks: [
        {
          uid: 1,
          remote: {
            receiveFrameRate: 15,
            receiveResolutionWidth: 1280,
            receiveResolutionHeight: 720,
          },
        },
        { uid: 2, remote: { receiveFrameRate: 14.6, receiveBitrate: 800_000 } },
      ],
    });
    expect(snap.tracks.map((t) => [t.uid, t.fps, t.width])).toEqual([
      [1, 15, 1280],
      [2, 15, null],
    ]);
    expect(snap.tracks[1]?.bitrateKbps).toBe(800);
    expect(trackSummary(snap.tracks[0])).toBe('1280x720 · 15 fps');
    expect(trackSummary(snap.tracks[1])).toBe('- · 15 fps');
    expect(trackSummary(trackSnapshot(3, undefined, undefined))).toBeNull();
    expect(trackSummary(undefined)).toBeNull();
    // Without a track list the first camera stands alone; without a camera nothing.
    const one = buildSnapshot({
      state: 'CONNECTED',
      uid: 1,
      remote: undefined,
      rtc: undefined,
      log: new EventLog(),
    });
    expect(one.tracks.map((t) => t.uid)).toEqual([1]);
    const none = buildSnapshot({
      state: 'CONNECTED',
      uid: null,
      remote: undefined,
      rtc: undefined,
      log: new EventLog(),
    });
    expect(none.tracks).toEqual([]);
  });
});

describe('same picture guard', () => {
  it('compares thumbnails and reports only after several equal readings', () => {
    const a = new Uint8ClampedArray([10, 20, 30, 255, 40, 50, 60, 255]);
    const b = new Uint8ClampedArray([12, 18, 30, 0, 40, 50, 66, 255]);
    expect(meanAbsDifference(a, a)).toBe(0);
    expect(meanAbsDifference(a, b)).toBeCloseTo(10 / 6);
    expect(meanAbsDifference(a, new Uint8ClampedArray(4))).toBeNull();
    expect(meanAbsDifference([], [])).toBeNull();
    const watch = new SamePictureWatch();
    for (let i = 1; i < SAME_PICTURE_READINGS; i += 1) expect(watch.observe(0)).toBe(false);
    expect(watch.observe(null)).toBe(false); // unknown: neither counts nor resets
    expect(watch.observe(1)).toBe(true);
    expect(watch.observe(null)).toBe(true);
    expect(watch.observe(40)).toBe(false); // different pictures again
    watch.observe(0);
    watch.reset();
    expect(watch.same).toBe(false);
  });
});

function recordingContext(): PatternContext & { texts: string[]; fills: string[] } {
  const texts: string[] = [];
  const fills: string[] = [];
  const ctx = {
    texts,
    fills,
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 1,
    font: '',
    textAlign: 'start' as CanvasTextAlign,
    textBaseline: 'alphabetic' as CanvasTextBaseline,
    fillRect() {
      fills.push(String(ctx.fillStyle));
    },
    strokeRect() {},
    fillText(text: string) {
      texts.push(text);
    },
    beginPath() {},
    arc() {},
    stroke() {},
    moveTo() {},
    lineTo() {},
  };
  return ctx;
}

function canvasWith(ctx: PatternContext): HTMLCanvasElement {
  const canvas = document.createElement('canvas');
  canvas.getContext = (() => ctx) as unknown as HTMLCanvasElement['getContext'];
  return canvas;
}

describe('FakePlayer with two tiles', () => {
  it('draws each camera on its own canvas, the right one mirrored and labelled', async () => {
    const left = recordingContext();
    const right = recordingContext();
    const canvases = [canvasWith(left), canvasWith(right)];
    const events = {
      onLive: vi.fn(),
      onWaiting: vi.fn(),
      onTokenExpiring: vi.fn(),
      onFailed: vi.fn(),
      onActive: vi.fn(),
      onTracks: vi.fn(),
    };
    const player = new FakePlayer(
      (slot) => canvases[slot] ?? null,
      {
        label: (uid) => (uid === 1 ? 'Sim · Front left' : 'Sim · Front right'),
        mirror: (uid) => uid === 2,
        position: () => null,
        colors: () => ({
          bars: ['white', 'yellow', 'cyan'],
          background: 'grey',
          ink: 'black',
          inkInverse: 'white',
          sweep: 'blue',
        }),
      },
      events,
      () => NOW,
    );
    await player.start(session([LEFT, RIGHT], 'fake'), [1, 2]);
    expect(events.onTracks).toHaveBeenLastCalledWith([1, 2]);
    expect(events.onActive).toHaveBeenLastCalledWith(1);
    expect(left.texts).toContain('Sim · Front left');
    expect(right.texts).toContain('Sim · Front right');
    // Background first, then the bars: left to right, mirrored on the right camera.
    expect(left.fills.slice(1, 4)).toEqual(['white', 'yellow', 'cyan']);
    expect(right.fills.slice(1, 4)).toEqual(['cyan', 'yellow', 'white']);
    expect(canvases[1]?.dataset.frames).toBe('1');
    // A camera the session does not list delivers nothing: no picture, not reported live.
    await player.select([1, 9]);
    expect(events.onTracks).toHaveBeenLastCalledWith([1]);
    expect(canvases[1]?.dataset.frames).toBe('1');
    await player.select(2);
    expect(player.slots).toEqual([2]);
    await player.stop();
  });
});

class TilePlayer implements CameraPlayer {
  started: CameraChoice[] = [];
  selected: CameraChoice[] = [];
  constructor(
    readonly events: PlayerEvents,
    private readonly delivers: number[],
  ) {}
  async start(_s: CameraSession, choice: CameraChoice): Promise<void> {
    this.started.push(choice);
    this.events.onPublishers?.([1, 2]);
    this.show(choice);
    this.events.onLive();
  }
  async select(choice: CameraChoice): Promise<void> {
    this.selected.push(choice);
    this.show(choice);
  }
  private show(choice: CameraChoice): void {
    const uids = slotsOf(choice).map((u) => u ?? 1);
    this.events.onActive?.(uids[0] ?? null);
    this.events.onTracks?.(uids.filter((u) => this.delivers.includes(u)));
  }
  async renew(): Promise<void> {}
  setMuted(): void {}
  async stop(): Promise<void> {}
}

function viewerHarness(layout: CameraLayout = 'single', delivers = [1, 2]) {
  const players: TilePlayer[] = [];
  const timers: { fn: () => void; ms: number; cleared: boolean }[] = [];
  const layouts: CameraLayout[] = [];
  const viewer = new CameraViewer({
    request: async () => session(),
    createPlayer: async (_s, events) => {
      const player = new TilePlayer(events, delivers);
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
    saveOptions: () => undefined,
    debug: () => false,
    layout,
    saveLayout: (l) => layouts.push(l),
  });
  const noData = () => timers.filter((t) => t.ms === NO_DATA_MS && !t.cleared);
  return { viewer, players, layouts, noData };
}

describe('viewer with both front cameras', () => {
  it('shows both side by side, saves the layout and goes back to one camera', async () => {
    const { viewer, players, layouts } = viewerHarness();
    await viewer.open('m1');
    expect(players[0]?.started).toEqual([null]);
    expect(viewer.pair).not.toBeNull();
    expect(viewer.both).toBe(false);
    await viewer.showBoth();
    expect(players[0]?.selected.at(-1)).toEqual([1, 2]);
    expect(viewer.both).toBe(true);
    expect(viewer.live).toEqual([1, 2]);
    expect(viewer.tiles.map((t) => t.status)).toEqual(['live', 'live']);
    expect(layouts).toEqual(['both']);
    await viewer.select(2);
    expect(players[0]?.selected.at(-1)).toBe(2);
    expect(viewer.both).toBe(false);
    expect(viewer.tiles[0]).toMatchObject({ uid: 2, side: 'right' });
    expect(layouts).toEqual(['both', 'single']);
  });

  it('opens in the stored side-by-side layout', async () => {
    const { viewer, players } = viewerHarness('both');
    await viewer.open('m1');
    expect(players[0]?.started).toEqual([[1, 2]]);
    expect(viewer.both).toBe(true);
  });

  it('reports a camera that delivers nothing in time as "no data", never as the other one', async () => {
    const { viewer, noData } = viewerHarness('both', [1]);
    await viewer.open('m1');
    expect(viewer.tiles.map((t) => t.status)).toEqual(['live', 'connecting']);
    const timer = noData().at(-1);
    expect(timer).toBeDefined();
    timer?.fn();
    expect(viewer.stalled).toEqual([2]);
    expect(viewer.tiles.map((t) => [t.uid, t.status])).toEqual([
      [1, 'live'],
      [2, 'no_data'],
    ]);
    // Choosing again restarts the wait.
    await viewer.showBoth();
    expect(viewer.stalled).toEqual([]);
  });

  it('clears the stalled mark when the camera starts to deliver', async () => {
    const { viewer, players, noData } = viewerHarness('both', [1]);
    await viewer.open('m1');
    noData().at(-1)?.fn();
    expect(viewer.stalled).toEqual([2]);
    players[0]?.events.onTracks?.([1, 2]);
    expect(viewer.stalled).toEqual([]);
    expect(viewer.tiles[1]?.status).toBe('live');
  });

  it('arms no timer for the first publisher and ignores "both" without a pair', async () => {
    const { viewer, noData, players } = viewerHarness();
    await viewer.open('m1');
    expect(noData()).toHaveLength(0);
    await viewer.select(1);
    expect(noData()).toHaveLength(1);
    viewer.session = session([LEFT]);
    await viewer.showBoth();
    expect(players[0]?.selected).toEqual([1]);
    await viewer.close();
    expect(noData()).toHaveLength(0);
    expect(viewer.live).toEqual([]);
  });
});
