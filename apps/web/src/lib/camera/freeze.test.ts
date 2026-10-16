import { describe, expect, it } from 'vitest';

import { FREEZE_MS, FreezeWatch, frameCounter } from './freeze';

const live = (frames: number | null) => ({ live: true, frames });
const gone = { live: false, frames: null };

describe('FreezeWatch', () => {
  it('is not frozen while frames keep coming', () => {
    const watch = new FreezeWatch();
    for (let t = 0, f = 1; t <= 5_000; t += 200, f += 2) {
      expect(watch.observe([live(f)], t)).toBe(false);
    }
  });

  it('freezes after a second without a new frame and thaws on the next frame', () => {
    const watch = new FreezeWatch();
    expect(watch.observe([live(10)], 0)).toBe(false);
    expect(watch.observe([live(10)], 600)).toBe(false);
    expect(watch.observe([live(10)], FREEZE_MS)).toBe(false);
    expect(watch.observe([live(10)], FREEZE_MS + 1)).toBe(true);
    expect(watch.observe([live(10)], 3_000)).toBe(true);
    expect(watch.observe([live(11)], 3_200)).toBe(false);
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
    expect(watch.observe([live(5), live(1)], 1_500)).toBe(true);
    expect(watch.observe([live(6), live(2)], 1_700)).toBe(false);
    // Back to one tile: the right one is forgotten.
    expect(watch.observe([live(7)], 1_900)).toBe(false);
    expect(watch.observe([live(8), gone], 2_100)).toBe(false);
  });

  it('trusts the tile status when no counter can be read', () => {
    const watch = new FreezeWatch();
    expect(watch.observe([live(null)], 0)).toBe(false);
    expect(watch.observe([live(null)], 5_000)).toBe(false);
  });

  it('starts over after a reset', () => {
    const watch = new FreezeWatch(500);
    watch.observe([live(1)], 0);
    expect(watch.observe([gone], 100)).toBe(true);
    watch.reset();
    expect(watch.observe([gone], 200)).toBe(false);
    expect(watch.observe([live(3)], 300)).toBe(false);
    expect(watch.observe([live(3)], 900)).toBe(true);
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

  it('reads the decoded frames of a video, else its playback time', () => {
    const video = document.createElement('video');
    Object.defineProperty(video, 'readyState', { value: 0, configurable: true });
    expect(frameCounter(video)).toBeNull();
    Object.defineProperty(video, 'readyState', { value: 4, configurable: true });
    Object.defineProperty(video, 'currentTime', { value: 12.5, configurable: true });
    expect(frameCounter(video)).toBe(12.5);
    Object.defineProperty(video, 'getVideoPlaybackQuality', {
      value: () => ({ totalVideoFrames: 300 }),
      configurable: true,
    });
    expect(frameCounter(video)).toBe(300);
  });
});
