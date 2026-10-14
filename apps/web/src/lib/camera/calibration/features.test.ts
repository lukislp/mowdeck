/**
 * The local `features` method on synthetic pictures: a textured scene seen by a "left" camera
 * and by a "right" camera that is shifted, slightly rolled and (once) mirrored. The recovered
 * homography must put the right frame where the scene says, within about 1 % of the width.
 */
import { describe, expect, it } from 'vitest';

import { flipGray, type GrayImage } from '../panorama';
import { correlationMethod } from './correlation';
import {
  detectFeatures,
  featureCorrespondences,
  hamming,
  matchFeatures,
  PATTERN,
} from './features';
import {
  apply,
  corners,
  fromUnit,
  multiply,
  translation,
  type Homography,
  type Point,
} from './homography';
import { featuresMethod, FEATURES_MIN_INLIERS } from './local-features';
import { manualHomography } from './manual';
import { seededRandom } from './ransac';
import type { CalibrationFrame, CalibrationOptions } from './types';

const W = 320;
const H = 240;
const MARGIN = 120;

/** A garden of overlapping grey rectangles and discs on a gradient, plus a little noise. */
function scene(width: number, height: number, seed = 3): GrayImage {
  const random = seededRandom(seed);
  const data = new Float32Array(width * height);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) data[y * width + x] = 90 + (60 * x) / width;
  }
  for (let i = 0; i < 260; i += 1) {
    const w = 6 + random() * 34;
    const h = 6 + random() * 34;
    const x0 = random() * (width - w);
    const y0 = random() * (height - h);
    const value = random() * 255;
    const disc = random() < 0.3;
    for (let y = Math.floor(y0); y < y0 + h; y += 1) {
      for (let x = Math.floor(x0); x < x0 + w; x += 1) {
        if (disc) {
          const dx = (x - x0 - w / 2) / (w / 2);
          const dy = (y - y0 - h / 2) / (h / 2);
          if (dx * dx + dy * dy > 1) continue;
        }
        data[y * width + x] = value;
      }
    }
  }
  for (let i = 0; i < data.length; i += 1) data[i] = data[i]! + (random() - 0.5) * 6;
  return { width, height, data };
}

function bilinear(image: GrayImage, x: number, y: number): number {
  const x0 = Math.max(0, Math.min(image.width - 2, Math.floor(x)));
  const y0 = Math.max(0, Math.min(image.height - 2, Math.floor(y)));
  const fx = Math.max(0, Math.min(1, x - x0));
  const fy = Math.max(0, Math.min(1, y - y0));
  const at = (xx: number, yy: number) => image.data[yy * image.width + xx] ?? 0;
  return (
    at(x0, y0) * (1 - fx) * (1 - fy) +
    at(x0 + 1, y0) * fx * (1 - fy) +
    at(x0, y0 + 1) * (1 - fx) * fy +
    at(x0 + 1, y0 + 1) * fx * fy
  );
}

/** What a camera sees: frame point `p` (canvas coordinates) shows world point `toWorld(p)`. */
function view(world: GrayImage, toWorld: (p: Point) => Point): GrayImage {
  const data = new Float32Array(W * H);
  for (let y = 0; y < H; y += 1) {
    for (let x = 0; x < W; x += 1) {
      const [wx, wy] = toWorld([x + 0.5, y + 0.5]);
      data[y * W + x] = bilinear(world, wx - 0.5, wy - 0.5);
    }
  }
  return { width: W, height: H, data };
}

function rotation(degrees: number, cx: number, cy: number): Homography {
  const a = (degrees * Math.PI) / 180;
  const c = Math.cos(a);
  const s = Math.sin(a);
  return multiply(
    translation(cx, cy),
    multiply([c, -s, 0, s, c, 0, 0, 0, 1], translation(-cx, -cy)),
  );
}

function frame(gray: GrayImage): CalibrationFrame {
  return { width: gray.width, height: gray.height, gray, capturedAt: 0, jpeg: async () => '' };
}

/** A left and right view where right point p appears at `truth(p)` in the left frame. */
function stereo(truth: Homography, mirror = false) {
  const world = scene(2 * W + 2 * MARGIN, H + 2 * MARGIN);
  const toWorld = ([x, y]: Point): Point => [x + MARGIN, y + MARGIN];
  const left = view(world, toWorld);
  const rawRight = view(world, (p) => toWorld(apply(truth, p)!));
  return { left, right: mirror ? flipGray(rawRight) : rawRight };
}

/** Largest distance between where two pixel homographies put the right frame's corners. */
function cornerError(a: Homography, b: Homography): number {
  return Math.max(
    ...corners(W, H).map((p) => {
      const [ax, ay] = apply(a, p)!;
      const [bx, by] = apply(b, p)!;
      return Math.hypot(ax - bx, ay - by);
    }),
  );
}

const OPTIONS: CalibrationOptions = {
  mirror: false,
  manual: { overlap: 0.12, offset: 0, mirror: false },
};

describe('ORB-style features', () => {
  it('describes keypoints with 256 bits and matches a picture with itself', () => {
    const image = scene(W, H);
    const features = detectFeatures(image);
    expect(features.keypoints.length).toBeGreaterThan(100);
    expect(features.descriptors.length).toBe(features.keypoints.length * 8);
    expect(PATTERN.length).toBe(256 * 4);
    expect(hamming(features.descriptors, 0, features.descriptors, 0)).toBe(0);
    const matches = matchFeatures(features, features);
    expect(matches.length).toBeGreaterThan(features.keypoints.length * 0.8);
    expect(matches.every((m) => m.left === m.right && m.distance === 0)).toBe(true);
  });

  it('finds nothing on a flat picture and in tiny pictures', () => {
    const flat = { width: W, height: H, data: new Float32Array(W * H).fill(120) };
    expect(detectFeatures(flat).keypoints).toHaveLength(0);
    const tiny = { width: 30, height: 30, data: new Float32Array(900) };
    expect(featureCorrespondences(tiny, tiny).pairs).toHaveLength(0);
  });
});

describe('features method', () => {
  it('recovers a shift with a small roll within tolerance', async () => {
    const truth = multiply(translation(0.7 * W, 6), rotation(2.5, W / 2, H / 2));
    const { left, right } = stereo(truth);
    const method = featuresMethod({ label: () => 'features' });
    const result = await method.estimate(frame(left), frame(right), OPTIONS);
    expect(result.method).toBe('features');
    expect(result.fallbackFrom).toBeUndefined();
    expect(result.inliers).toBeGreaterThanOrEqual(FEATURES_MIN_INLIERS);
    expect(result.confidence).toBeGreaterThan(0.3);
    // The fit only sees the 30 % overlap; extrapolated to the far corners of the right frame it
    // stays within 1 % of the frame width.
    expect(cornerError(fromUnit(result.homography, W, H), truth)).toBeLessThan(0.01 * W);
  });

  it('flips a mirrored right camera back and folds the flip into the homography', async () => {
    const truth = translation(0.75 * W, -4);
    const { left, right } = stereo(truth, true);
    const method = featuresMethod({ label: () => 'features' });
    const result = await method.estimate(frame(left), frame(right), { ...OPTIONS, mirror: true });
    // The mirrored frame's pixel x shows the raw frame's W - x.
    const expected = multiply(truth, [-1, 0, W, 0, 1, 0, 0, 0, 1]);
    expect(cornerError(fromUnit(result.homography, W, H), expected)).toBeLessThan(2);
  });

  it('falls back to the correlation on pictures without features', async () => {
    const flat = { width: W, height: H, data: new Float32Array(W * H).fill(80) };
    const correlation = correlationMethod(() => 'correlation');
    const method = featuresMethod({ label: () => 'features', fallback: correlation });
    await expect(method.estimate(frame(flat), frame(flat), OPTIONS)).rejects.toMatchObject({
      reason: 'no_match',
    });
    // With texture only along the shared edge the correlation still finds the overlap.
    const truth = translation(0.8 * W, 0);
    const { left, right } = stereo(truth);
    const noMatches = featuresMethod({
      label: () => 'features',
      fallback: correlation,
      matcher: async () => () => ({ pairs: [] }),
    });
    const result = await noMatches.estimate(frame(left), frame(right), OPTIONS);
    expect(result.method).toBe('correlation');
    expect(result.fallbackFrom).toBe('features');
    expect(result.overlapPercent).toBe(20);
    expect(cornerError(fromUnit(result.homography, W, H), truth)).toBeLessThan(3);
    expect(result.homography).toEqual(manualHomography({ overlap: 0.2, offset: 0, mirror: false }));
  });
});
