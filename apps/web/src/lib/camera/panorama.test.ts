/** The panorama layout: settings, geometry, the overlap estimate, the clock and the drawing. */
import { describe, expect, it, vi } from 'vitest';

import {
  DEFAULT_PANORAMA,
  estimateOverlap,
  flipGray,
  normalizePanorama,
  OFFSET_MAX,
  OVERLAP_MAX,
  panoramaGeometry,
  stripCorrelation,
  toGray,
  type GrayImage,
} from './panorama';
import { manualHomography } from './calibration/manual';
import {
  backingSize,
  FRAME_INTERVAL_MS,
  grabGray,
  PanoramaClock,
  PanoramaRenderer,
  sourceSize,
  VIDEO_IDLE_MS,
  type PanoramaSource,
} from './panorama-renderer';

/** A deterministic pseudo-random generator (mulberry32). */
function random(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** A textured "garden" wider than one frame: smooth blobs plus fine noise. */
function scene(width: number, height: number, seed = 7): GrayImage {
  const next = random(seed);
  const data = new Float32Array(width * height);
  const phases = Array.from({ length: 6 }, () => [next() * 6.28, 0.02 + next() * 0.15, next()]);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      let v = 128;
      for (const [phase = 0, freq = 0.1, tilt = 0.5] of phases) {
        v += 18 * Math.sin(phase + freq * (x + tilt * y)) * Math.cos(freq * 0.7 * y - phase);
      }
      data[y * width + x] = v + (next() - 0.5) * 40;
    }
  }
  return { width, height, data };
}

/** A window of `width` x `height` from `image` at column `x0`, row `y0`. */
function crop(image: GrayImage, x0: number, y0: number, width: number, height: number): GrayImage {
  const data = new Float32Array(width * height);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      data[y * width + x] = image.data[(y0 + y) * image.width + x0 + x] ?? 0;
    }
  }
  return { width, height, data };
}

/** Left and right frame of a scene that overlap by `overlap` columns, right shifted by `shift`. */
function pair(width: number, height: number, overlap: number, shift = 0, seed = 7) {
  const margin = 20;
  const world = scene(2 * width, height + 2 * margin, seed);
  // Right row y shows left row y + shift (as in the settings: positive offset = right lower).
  const left = crop(world, 0, margin, width, height);
  const right = crop(world, width - overlap, margin + shift, width, height);
  return { left, right };
}

describe('panorama settings', () => {
  it('clamps anything into valid settings', () => {
    expect(normalizePanorama(null)).toEqual(DEFAULT_PANORAMA);
    expect(normalizePanorama({ overlap: 2, offset: -1, mirror: 'yes' })).toEqual({
      overlap: OVERLAP_MAX,
      offset: -OFFSET_MAX,
      mirror: false,
    });
    expect(normalizePanorama({ overlap: Number.NaN, offset: 0.05, mirror: true })).toEqual({
      overlap: DEFAULT_PANORAMA.overlap,
      offset: 0.05,
      mirror: true,
    });
  });
});

describe('panorama geometry', () => {
  it('places the right frame inside the left one by the overlap', () => {
    const g = panoramaGeometry(640, 480, { overlap: 0.12, offset: 0, mirror: false });
    expect(g.overlapPx).toBe(77);
    expect(g.width).toBe(1203);
    expect(g.height).toBe(480);
    expect(g.left).toEqual({ x: 0, y: 0, width: 640, height: 480 });
    expect(g.right).toEqual({ x: 563, y: 0, width: 640, height: 480 });
    expect(panoramaGeometry(640, 480, { overlap: 0, offset: 0, mirror: false }).width).toBe(1280);
  });

  it('crops to the rows both frames cover when the right frame is shifted', () => {
    const down = panoramaGeometry(640, 480, { overlap: 0, offset: 0.1, mirror: false });
    expect(down.height).toBe(432);
    expect(down.left.y).toBe(-48);
    expect(down.right.y).toBe(0);
    const up = panoramaGeometry(640, 480, { overlap: 0, offset: -0.1, mirror: false });
    expect(up.height).toBe(432);
    expect(up.left.y).toBe(0);
    expect(up.right.y).toBe(-48);
  });
});

describe('greyscale helpers', () => {
  it('converts RGBA to luminance and flips horizontally', () => {
    const gray = toGray([255, 255, 255, 255, 0, 0, 0, 255, 255, 0, 0, 255], 3, 1);
    expect(Array.from(gray.data).map((v) => Math.round(v))).toEqual([255, 0, 76]);
    expect(Array.from(flipGray(gray).data).map((v) => Math.round(v))).toEqual([76, 0, 255]);
  });

  it('correlates identical strips perfectly and flat strips not at all', () => {
    const { left } = pair(40, 30, 10);
    const same = crop(left, 30, 0, 10, 30);
    expect(stripCorrelation(left, same, 10)).toBeCloseTo(1, 6);
    const flat = { width: 10, height: 30, data: new Float32Array(300).fill(9) };
    expect(stripCorrelation(left, flat, 10)).toBeNull();
    expect(stripCorrelation(left, same, 0)).toBeNull();
    expect(stripCorrelation(left, same, 11)).toBeNull();
  });
});

describe('overlap estimate', () => {
  it.each([16, 24, 40, 56])('finds an overlap of %i of 160 columns', (overlap) => {
    const { left, right } = pair(160, 90, overlap);
    const estimate = estimateOverlap(left, right);
    expect(estimate).not.toBeNull();
    expect(Math.round((estimate?.overlap ?? 0) * 160)).toBe(overlap);
    expect(estimate?.score).toBeGreaterThan(0.95);
    expect(estimate?.atLimit).toBe(false);
  });

  it('uses the vertical offset to match the rows', () => {
    const { left, right } = pair(160, 90, 30, 9);
    const estimate = estimateOverlap(left, right, { offset: 0.1 });
    expect(Math.round((estimate?.overlap ?? 0) * 160)).toBe(30);
    expect(estimate?.score).toBeGreaterThan(0.95);
  });

  it('finds the overlap of a mirrored right camera after flipping it back', () => {
    const { left, right } = pair(160, 90, 32);
    const mirrored = flipGray(right);
    const estimate = estimateOverlap(left, flipGray(mirrored));
    expect(Math.round((estimate?.overlap ?? 0) * 160)).toBe(32);
  });

  it('reports a match on the edge of the range', () => {
    const { left, right } = pair(160, 90, 64);
    expect(estimateOverlap(left, right)?.atLimit).toBe(true);
  });

  it('gives no estimate for flat, unrelated or tiny pictures', () => {
    const flat = { width: 160, height: 90, data: new Float32Array(160 * 90).fill(100) };
    expect(estimateOverlap(flat, flat)).toBeNull();
    const a = scene(160, 90, 1);
    const b = scene(160, 90, 99);
    const noise = (seed: number): GrayImage => {
      const next = random(seed);
      return {
        width: 160,
        height: 90,
        data: Float32Array.from({ length: 160 * 90 }, () => next() * 255),
      };
    };
    expect(estimateOverlap(noise(3), noise(4))).toBeNull();
    // Unrelated textures may correlate weakly, never like the real overlap.
    expect(estimateOverlap(a, b)?.score ?? 0).toBeLessThan(0.95);
    expect(estimateOverlap(crop(a, 0, 0, 3, 3), crop(a, 0, 0, 3, 3))).toBeNull();
  });
});

/** A manual animation-frame queue and clock. */
function frames() {
  let now = 0;
  let queue: (() => void)[] = [];
  return {
    deps: {
      raf: (callback: () => void) => {
        queue.push(callback);
        return callback;
      },
      caf: (handle: unknown) => {
        queue = queue.filter((c) => c !== handle);
      },
      now: () => now,
    },
    get pending() {
      return queue.length;
    },
    /** Run animation frames every 16 ms for `ms`. */
    run(ms: number, each?: (time: number) => void) {
      const end = now + ms;
      while (now < end) {
        now += 16;
        each?.(now);
        const current = queue;
        queue = [];
        for (const callback of current) callback();
      }
    },
  };
}

describe('panorama clock', () => {
  it('draws at once and then at most 10 times a second without video frames', () => {
    const clock = frames();
    const draw = vi.fn();
    const c = new PanoramaClock(draw, clock.deps);
    c.start();
    clock.run(16);
    expect(draw).toHaveBeenCalledTimes(1);
    clock.run(1000);
    expect(draw.mock.calls.length).toBeGreaterThanOrEqual(9);
    expect(draw.mock.calls.length).toBeLessThanOrEqual(1 + 1000 / FRAME_INTERVAL_MS);
  });

  it('follows the video frames and skips the fallback while they arrive', () => {
    const clock = frames();
    const draw = vi.fn();
    const c = new PanoramaClock(draw, clock.deps);
    c.start();
    c.frame();
    let next = 160;
    let delivered = 0;
    clock.run(1600, (time) => {
      if (time >= next) {
        next += 160;
        delivered += 1;
        c.frame();
      }
    });
    // The first draw plus one per video frame, nothing in between.
    expect(draw).toHaveBeenCalledTimes(delivered + 1);
    // Frames stop: the capped loop takes over after the idle time.
    draw.mockClear();
    clock.run(VIDEO_IDLE_MS + 500);
    expect(draw.mock.calls.length).toBeGreaterThanOrEqual(4);
  });

  it('pauses while hidden or stopped and redraws after invalidate', () => {
    const clock = frames();
    const draw = vi.fn();
    const c = new PanoramaClock(draw, clock.deps);
    c.start();
    clock.run(16);
    c.setVisible(false);
    expect(c.active).toBe(false);
    expect(clock.pending).toBe(0);
    clock.run(500);
    expect(draw).toHaveBeenCalledTimes(1);
    c.setVisible(true);
    clock.run(16);
    expect(draw).toHaveBeenCalledTimes(2);
    clock.run(16);
    c.invalidate();
    clock.run(16);
    expect(draw).toHaveBeenCalledTimes(3);
    c.stop();
    expect(clock.pending).toBe(0);
    clock.run(500);
    expect(draw).toHaveBeenCalledTimes(3);
  });
});

describe('panorama backing size', () => {
  it('follows the CSS width times the pixel ratio, never above the frames', () => {
    const g = { width: 1203, height: 480 };
    expect(backingSize(g, 400, 2)).toMatchObject({ width: 800, height: 319 });
    expect(backingSize(g, 1000, 3)).toMatchObject({ width: 1203, height: 480, scale: 1 });
    expect(backingSize(g, 0, 2)).toMatchObject({ width: 1203, height: 480 });
    expect(backingSize(g, 601.5, 0)).toMatchObject({ width: 602, height: 240 });
  });
});

/** A recording 2D context. */
function recorder() {
  const calls: unknown[][] = [];
  const gradient = { addColorStop: vi.fn() };
  const context = new Proxy({ calls, gradient } as Record<string, unknown>, {
    get(target, key) {
      if (key in target) return target[key as string];
      if (key === 'createLinearGradient') {
        return (...args: unknown[]) => {
          calls.push(['createLinearGradient', ...args]);
          return gradient;
        };
      }
      return (...args: unknown[]) => calls.push([key, ...args]);
    },
    set(target, key, value) {
      calls.push([`set:${String(key)}`, value]);
      target[`_${String(key)}`] = value;
      return true;
    },
  });
  return { context, calls, gradient };
}

function fakeCanvas() {
  const rec = recorder();
  const canvas = { width: 300, height: 150, getContext: () => rec.context };
  return { canvas: canvas as unknown as HTMLCanvasElement, ...rec };
}

function fakeVideo(width = 640, height = 480) {
  const callbacks: (() => void)[] = [];
  const video = {
    videoWidth: width,
    videoHeight: height,
    requestVideoFrameCallback: vi.fn((cb: () => void) => callbacks.push(cb)),
    cancelVideoFrameCallback: vi.fn(),
  };
  return { video: video as unknown as HTMLVideoElement, callbacks };
}

describe('panorama renderer', () => {
  it('draws the left frame, then the right one faded in over the overlap', () => {
    const target = fakeCanvas();
    const scratch = fakeCanvas();
    const left = fakeVideo();
    const right = fakeVideo();
    const onFrame = vi.fn();
    const renderer = new PanoramaRenderer(target.canvas, {
      sources: () => [left.video, right.video],
      homography: () => manualHomography({ overlap: 0.25, offset: 0, mirror: true }),
      onFrame,
      doc: { createElement: () => scratch.canvas } as unknown as Document,
    });
    renderer.resize(0, 1);
    expect(renderer.draw()).toBe(true);
    expect(target.canvas.width).toBe(1120);
    expect(target.canvas.height).toBe(480);
    const drawn = target.calls.filter((c) => c[0] === 'drawImage');
    expect(drawn[0]).toEqual(['drawImage', left.video, 0, 0, 640, 480]);
    expect(drawn[1]).toEqual(['drawImage', scratch.canvas, 0, 0]);
    // The right frame mirrored and placed (one affine transform: x -> 1120 - x) into the
    // panorama-sized layer, then masked with a ramp over the 160 px overlap.
    const warp = scratch.calls.find((c) => c[0] === 'setTransform' && c[1] !== 1);
    expect(warp?.slice(1).map((v) => (Math.abs(Number(v)) < 1e-9 ? 0 : v))).toEqual([
      -1, 0, 0, 1, 1120, 0,
    ]);
    expect(scratch.calls).toContainEqual(['drawImage', right.video, 0, 0, 640, 480]);
    expect(scratch.calls).toContainEqual(['createLinearGradient', 480, 0, 640, 0]);
    expect(scratch.calls).toContainEqual(['set:globalCompositeOperation', 'destination-in']);
    expect(scratch.gradient.addColorStop).toHaveBeenCalledWith(0, 'rgba(0, 0, 0, 0)');
    expect(onFrame).toHaveBeenCalledWith(1);
    // Each video reports its next frame.
    expect(left.callbacks).toHaveLength(1);
    expect(right.callbacks).toHaveLength(1);
  });

  it('draws one camera alone, scales to the CSS width and stops the frame callbacks', () => {
    const target = fakeCanvas();
    const scratch = fakeCanvas();
    const right = fakeVideo();
    const missing = fakeVideo(0, 0);
    const renderer = new PanoramaRenderer(target.canvas, {
      sources: () => [missing.video, right.video],
      homography: () => manualHomography({ overlap: 0, offset: 0, mirror: false }),
      doc: { createElement: () => scratch.canvas } as unknown as Document,
      clock: { raf: () => 1, caf: () => undefined, now: () => 0 },
    });
    renderer.start();
    renderer.resize(320, 2);
    expect(renderer.draw()).toBe(true);
    expect(target.canvas.width).toBe(640);
    expect(target.canvas.height).toBe(240);
    // No ramp without a left picture to blend with.
    expect(scratch.calls.some((c) => c[0] === 'createLinearGradient')).toBe(false);
    right.callbacks[0]?.();
    expect(right.callbacks).toHaveLength(2);
    renderer.stop();
    expect(
      (right.video as unknown as { cancelVideoFrameCallback: () => void }).cancelVideoFrameCallback,
    ).toHaveBeenCalled();
    right.callbacks[1]?.();
    expect(right.callbacks).toHaveLength(2);
  });

  it('warps a perspective calibration with a clipped triangle mesh', () => {
    const target = fakeCanvas();
    const scratch = fakeCanvas();
    const left = fakeVideo();
    const right = fakeVideo();
    // A shift with a little keystone (unit coordinates).
    const keystone = [0.95, 0, 0.75, 0.02, 1, 0, 0.12, 0, 1] as const;
    const renderer = new PanoramaRenderer(target.canvas, {
      sources: () => [left.video, right.video],
      homography: () => keystone,
      doc: { createElement: () => scratch.canvas } as unknown as Document,
    });
    expect(renderer.draw()).toBe(true);
    const clips = scratch.calls.filter((c) => c[0] === 'clip').length;
    const draws = scratch.calls.filter((c) => c[0] === 'drawImage' && c[1] === right.video).length;
    expect(clips).toBeGreaterThanOrEqual(8);
    expect(draws).toBe(clips);
    expect(scratch.calls.filter((c) => c[0] === 'save').length).toBe(clips);
    expect(scratch.calls.filter((c) => c[0] === 'restore').length).toBe(clips);
    expect(scratch.calls).toContainEqual(['set:globalCompositeOperation', 'destination-in']);
    // Drawing again reuses the cached mesh.
    expect(renderer.draw()).toBe(true);
  });

  it('draws nothing before any camera has a picture', () => {
    const target = fakeCanvas();
    const renderer = new PanoramaRenderer(target.canvas, {
      sources: () => [null, fakeVideo(0, 0).video],
      homography: () => manualHomography(DEFAULT_PANORAMA),
    });
    expect(renderer.draw()).toBe(false);
    expect(renderer.frames).toBe(0);
  });

  it('measures videos and canvases and grabs nothing without a frame', () => {
    expect(sourceSize(fakeVideo(320, 240).video)).toEqual([320, 240]);
    expect(sourceSize({ width: 64, height: 36 } as unknown as PanoramaSource)).toEqual([64, 36]);
    expect(sourceSize(null)).toEqual([0, 0]);
    expect(grabGray(null)).toBeNull();
    expect(grabGray(fakeVideo(0, 0).video)).toBeNull();
  });

  it('grabs a small greyscale copy of a frame', () => {
    const pixels = new Uint8ClampedArray(160 * 120 * 4).fill(200);
    const context = { drawImage: vi.fn(), getImageData: () => ({ data: pixels }) };
    const doc = {
      createElement: () => ({ width: 0, height: 0, getContext: () => context }),
    } as unknown as Document;
    const gray = grabGray(fakeVideo().video, 160, doc);
    expect(gray?.width).toBe(160);
    expect(gray?.height).toBe(120);
    expect(Math.round(gray?.data[0] ?? 0)).toBe(200);
  });
});
