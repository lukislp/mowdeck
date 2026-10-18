import { describe, expect, it } from 'vitest';

import {
  FREEZE_CEILING_MS,
  FREEZE_FLOOR_MS,
  FREEZE_POLL_MS,
  FreezeWatch,
  frameCounter,
  freezeThreshold,
  LABEL_COLD_MS,
  LABEL_FLOOR_MS,
  presentedFrames,
} from './freeze';

const live = (frames: number | null) => ({ live: true, frames });
const gone = { live: false, frames: null };

/** Feed a stream with one new frame every `frameMs` ms, read every 200 ms; frozen at any reading? */
function run(watch: FreezeWatch, frameMs: number, untilMs: number, startMs = 0): boolean {
  let frozen = false;
  for (let t = startMs; t <= untilMs; t += FREEZE_POLL_MS) {
    const frames = 1 + Math.floor(t / frameMs);
    if (watch.observe([live(frames)], t)) frozen = true;
  }
  return frozen;
}

describe('freezeThreshold', () => {
  it('is three frame intervals between the floor and the ceiling', () => {
    expect(freezeThreshold(null)).toBe(FREEZE_FLOOR_MS);
    expect(freezeThreshold(40)).toBe(FREEZE_FLOOR_MS);
    expect(freezeThreshold(600)).toBe(1_800);
    expect(freezeThreshold(1_000)).toBe(FREEZE_CEILING_MS);
    expect(freezeThreshold(5_000)).toBe(FREEZE_CEILING_MS);
    expect(freezeThreshold(Number.NaN)).toBe(FREEZE_FLOOR_MS);
  });
});

describe('FreezeWatch', () => {
  it('is not frozen while frames keep coming', () => {
    const watch = new FreezeWatch();
    for (let t = 0, f = 1; t <= 5_000; t += 200, f += 2) {
      expect(watch.observe([live(f)], t)).toBe(false);
    }
  });

  it('freezes after 1.5 s without a new frame and thaws on the next frame', () => {
    const watch = new FreezeWatch();
    expect(watch.observe([live(10)], 0)).toBe(false);
    expect(watch.observe([live(10)], 600)).toBe(false);
    expect(watch.observe([live(10)], FREEZE_FLOOR_MS)).toBe(false);
    expect(watch.observe([live(10)], FREEZE_FLOOR_MS + 1)).toBe(true);
    expect(watch.observe([live(10)], 3_000)).toBe(true);
    expect(watch.observe([live(11)], 3_200)).toBe(false);
  });

  it('catches a fast stream that stops within 1.5 s', () => {
    const watch = new FreezeWatch();
    expect(run(watch, 40, 3_000)).toBe(false); // 25 fps
    let frozenAt: number | null = null;
    for (let t = 3_200; t <= 6_000 && frozenAt === null; t += FREEZE_POLL_MS) {
      if (watch.observe([live(76)], t)) frozenAt = t;
    }
    expect(frozenAt).not.toBeNull();
    expect(frozenAt! - 3_000).toBeLessThanOrEqual(FREEZE_FLOOR_MS + FREEZE_POLL_MS);
  });

  it.each([1, 2, 5])('tolerates a steady stream of %i fps', (fps) => {
    const watch = new FreezeWatch();
    expect(run(watch, 1_000 / fps, 30_000)).toBe(false);
  });

  it('still catches a slow stream that stops, within the ceiling', () => {
    const watch = new FreezeWatch();
    expect(run(watch, 1_000, 10_000)).toBe(false); // 1 fps: threshold grows to the ceiling
    const [slot] = watch.view();
    expect(slot?.thresholdMs).toBe(FREEZE_CEILING_MS);
    expect(watch.observe([live(11)], 10_000 + FREEZE_CEILING_MS)).toBe(false);
    expect(watch.observe([live(11)], 10_000 + FREEZE_CEILING_MS + FREEZE_POLL_MS)).toBe(true);
  });

  it('a real freeze does not raise the next threshold', () => {
    const watch = new FreezeWatch();
    expect(run(watch, 40, 2_000)).toBe(false);
    // Stands for 5 s, then runs again.
    expect(watch.observe([live(51)], 7_000)).toBe(true);
    expect(watch.observe([live(60)], 7_200)).toBe(false);
    expect(watch.view()[0]?.thresholdMs).toBe(FREEZE_FLOOR_MS);
    // The next stop is caught as quickly as the first.
    expect(watch.observe([live(60)], 7_200 + FREEZE_FLOOR_MS + 1)).toBe(true);
  });

  it('flags a stream slower than the floor allows (one frame every 2 s)', () => {
    const watch = new FreezeWatch();
    expect(run(watch, 2_000, 10_000)).toBe(true);
  });

  it('does not count a camera that is still connecting', () => {
    const watch = new FreezeWatch();
    expect(watch.observe([gone], 0)).toBe(false);
    expect(watch.observe([gone], 10_000)).toBe(false);
    expect(watch.observe([live(1)], 10_200)).toBe(false);
  });

  it('counts a picture that went away after it was live', () => {
    const watch = new FreezeWatch();
    watch.observe([live(1)], 0);
    expect(watch.observe([gone], 200)).toBe(true);
    // Back with frames: live again at once.
    expect(watch.observe([live(1)], 400)).toBe(false);
    expect(watch.observe([live(2)], 600)).toBe(false);
  });

  it('watches every tile of the side by side view', () => {
    const watch = new FreezeWatch();
    watch.observe([live(1), live(1)], 0);
    expect(watch.observe([live(5), live(1)], 1_600)).toBe(true);
    expect(watch.observe([live(6), live(2)], 1_800)).toBe(false);
    // Back to one tile: the right one is forgotten.
    expect(watch.observe([live(7)], 2_000)).toBe(false);
    expect(watch.observe([live(8), gone], 2_200)).toBe(false);
  });

  it('trusts the tile status when no counter can be read', () => {
    const watch = new FreezeWatch();
    expect(watch.observe([live(null)], 0)).toBe(false);
    expect(watch.observe([live(null)], 5_000)).toBe(false);
  });

  it('starts over after a reset', () => {
    const watch = new FreezeWatch({ floorMs: 500, ceilingMs: 500 });
    watch.observe([live(1)], 0);
    expect(watch.observe([gone], 100)).toBe(true);
    watch.reset();
    expect(watch.observe([gone], 200)).toBe(false);
    expect(watch.observe([live(3)], 300)).toBe(false);
    expect(watch.observe([live(3)], 900)).toBe(true);
  });
});

/** A `<video>` stand-in with `requestVideoFrameCallback` driven by the test. */
class FakeFrameVideo {
  srcObject: unknown = 'stream-a';
  private next = 1;
  callbacks = new Map<number, () => void>();
  requestVideoFrameCallback = (callback: () => void): number => {
    const handle = this.next++;
    this.callbacks.set(handle, callback);
    return handle;
  };
  cancelVideoFrameCallback = (handle: number): void => {
    this.callbacks.delete(handle);
  };
  /** The browser presents one frame: every pending callback runs once. */
  present(): void {
    const pending = [...this.callbacks.values()];
    this.callbacks.clear();
    pending.forEach((callback) => callback());
  }
}

describe('FreezeWatch label ("Bild steht") and hold', () => {
  /**
   * A bursty but alive 4G stream: 10 fps, then a 1.8 s gap (frames held up), then a burst, every
   * 3 s. Returns at which readings the hold and the label were on.
   */
  function bursty(watch: FreezeWatch, untilMs: number): { hold: number[]; label: number[] } {
    const hold: number[] = [];
    const label: number[] = [];
    let frames = 1;
    let lastFrameAt = 0;
    for (let t = 0; t <= untilMs; t += FREEZE_POLL_MS) {
      const phase = t % 3_000;
      const flowing = phase < 1_200; // 1.2 s of frames, then 1.8 s without
      if (flowing && t - lastFrameAt >= 100) {
        frames += 2;
        lastFrameAt = t;
      }
      if (watch.observe([live(frames)], t)) hold.push(t);
      if (watch.labelled) label.push(t);
    }
    return { hold, label };
  }

  it('shows no label on a bursty stream with 1.8 s gaps; the hold may hold during a gap', () => {
    const watch = new FreezeWatch();
    const { hold, label } = bursty(watch, 30_000);
    expect(label).toEqual([]);
    // The hold keeps its strict 1.5 s floor (the drive view's safety): a 1.8 s gap holds.
    expect(hold.length).toBeGreaterThan(0);
    expect(hold.every((t) => t % 3_000 > 1_200 + FREEZE_FLOOR_MS - FREEZE_POLL_MS)).toBe(true);
  });

  it('a true freeze holds after 1.5 s and labels after 2.5 s; the next frame clears both', () => {
    const watch = new FreezeWatch();
    for (let t = 0, f = 1; t <= 3_000; t += FREEZE_POLL_MS, f += 2) watch.observe([live(f)], t);
    const stoppedAt = 3_000;
    let holdAt: number | null = null;
    let labelAt: number | null = null;
    for (let t = stoppedAt + FREEZE_POLL_MS; t <= 8_000; t += FREEZE_POLL_MS) {
      if (watch.observe([live(31)], t) && holdAt === null) holdAt = t;
      if (watch.labelled && labelAt === null) labelAt = t;
    }
    expect(holdAt! - stoppedAt).toBeLessThanOrEqual(FREEZE_FLOOR_MS + FREEZE_POLL_MS);
    expect(labelAt! - stoppedAt).toBeGreaterThan(LABEL_FLOOR_MS);
    expect(labelAt! - stoppedAt).toBeLessThanOrEqual(LABEL_FLOOR_MS + FREEZE_POLL_MS);
    expect(watch.observe([live(32)], 8_200)).toBe(false);
    expect(watch.labelled).toBe(false);
  });

  it('never labels a tile that is connecting or has presented no frame (cold start)', () => {
    const watch = new FreezeWatch();
    for (let t = 0; t <= 10_000; t += FREEZE_POLL_MS) {
      watch.observe([{ live: false, frames: null }], t);
      expect(watch.labelled).toBe(false);
    }
    // Live, but no frame counter yet: still nothing to call frozen.
    for (let t = 10_200; t <= 20_000; t += FREEZE_POLL_MS) {
      expect(watch.observe([live(null)], t)).toBe(false);
      expect(watch.labelled).toBe(false);
    }
  });

  it('right after the start a first frame with a slow second one is no label yet', () => {
    const watch = new FreezeWatch();
    watch.observe([live(1)], 0);
    let label = false;
    for (let t = FREEZE_POLL_MS; t < LABEL_COLD_MS; t += FREEZE_POLL_MS) {
      watch.observe([live(1)], t);
      label ||= watch.labelled;
    }
    expect(label).toBe(false);
    // A picture that never moves on is still labelled, after the cold-start limit.
    watch.observe([live(1)], LABEL_COLD_MS + FREEZE_POLL_MS);
    expect(watch.labelled).toBe(true);
  });

  it('a reconnecting tile holds the mower but shows no label (the status says connecting)', () => {
    const watch = new FreezeWatch();
    for (let t = 0, f = 1; t <= 2_000; t += FREEZE_POLL_MS, f += 2) watch.observe([live(f)], t);
    expect(watch.observe([gone], 2_200)).toBe(true);
    expect(watch.labelled).toBe(false);
    expect(watch.observe([gone], 9_000)).toBe(true);
    expect(watch.labelled).toBe(false);
  });
});

describe('presentedFrames', () => {
  it('is null without requestVideoFrameCallback', () => {
    expect(presentedFrames({ srcObject: null })).toBeNull();
  });

  it('counts presented frames with one callback chain', () => {
    const video = new FakeFrameVideo();
    expect(presentedFrames(video)).toBe(0);
    video.present();
    video.present();
    // Reading again does not start a second chain (no double counting).
    expect(presentedFrames(video)).toBe(2);
    expect(video.callbacks.size).toBe(1);
    video.present();
    expect(presentedFrames(video)).toBe(3);
  });

  it('stands while no frame is presented (a frozen stream)', () => {
    const video = new FakeFrameVideo();
    presentedFrames(video);
    video.present();
    expect(presentedFrames(video)).toBe(1);
    expect(presentedFrames(video)).toBe(1);
  });

  it('starts a fresh chain for a new stream and keeps rising', () => {
    const video = new FakeFrameVideo();
    presentedFrames(video);
    video.present();
    video.srcObject = 'stream-b';
    expect(presentedFrames(video)).toBe(1);
    expect(video.callbacks.size).toBe(1);
    video.present();
    expect(presentedFrames(video)).toBe(2);
  });
});

describe('frameCounter', () => {
  it('reads the test pattern canvas', () => {
    const canvas = document.createElement('canvas');
    expect(frameCounter(canvas)).toBeNull();
    canvas.dataset.frames = '42';
    expect(frameCounter(canvas)).toBe(42);
    expect(frameCounter(null)).toBeNull();
  });

  it('never uses the playback time (a MediaStream clock runs on while the picture stands)', () => {
    const video = document.createElement('video');
    Object.defineProperty(video, 'readyState', { value: 4, configurable: true });
    Object.defineProperty(video, 'currentTime', { value: 12.5, configurable: true });
    expect(frameCounter(video)).toBeNull();
  });

  it('falls back to the decoded frames without requestVideoFrameCallback', () => {
    const video = document.createElement('video');
    let total = 0;
    Object.defineProperty(video, 'getVideoPlaybackQuality', {
      value: () => ({ totalVideoFrames: total, droppedVideoFrames: 0 }),
      configurable: true,
    });
    // Firefox reports 0 for a MediaStream: nothing to check.
    expect(frameCounter(video)).toBeNull();
    total = 300;
    expect(frameCounter(video)).toBe(300);
  });

  it('prefers presented frames over a decoded counter that stands (WebKit)', () => {
    const video = document.createElement('video');
    const fake = new FakeFrameVideo();
    Object.defineProperty(video, 'requestVideoFrameCallback', {
      value: fake.requestVideoFrameCallback,
      configurable: true,
    });
    Object.defineProperty(video, 'cancelVideoFrameCallback', {
      value: fake.cancelVideoFrameCallback,
      configurable: true,
    });
    Object.defineProperty(video, 'getVideoPlaybackQuality', {
      value: () => ({ totalVideoFrames: 7, droppedVideoFrames: 0 }),
      configurable: true,
    });
    // No frame presented yet: not started (the decoded counter is not trusted instead).
    expect(frameCounter(video)).toBeNull();
    fake.present();
    expect(frameCounter(video)).toBe(1);
    fake.present();
    expect(frameCounter(video)).toBe(2);

    // Through the watch: a picture that keeps presenting is live although the decoded
    // counter stands, and one that stops presenting freezes although it would not.
    const watch = new FreezeWatch();
    let frozen = false;
    for (let t = 0; t <= 4_000; t += FREEZE_POLL_MS) {
      fake.present();
      frozen ||= watch.observe([live(frameCounter(video))], t);
    }
    expect(frozen).toBe(false);
    for (let t = 4_200; t <= 6_000; t += FREEZE_POLL_MS) {
      frozen ||= watch.observe([live(frameCounter(video))], t);
    }
    expect(frozen).toBe(true);
  });
});
