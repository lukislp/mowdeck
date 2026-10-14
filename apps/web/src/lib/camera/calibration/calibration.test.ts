/**
 * The panorama calibration's pure parts (ADR 0037): homography math, RANSAC, the stitch
 * geometry and warp mesh, the stored calibration with its migration from the slider settings,
 * the `manual` and `ai` methods (mocked points) and the card's flow.
 */
import { describe, expect, it, vi } from 'vitest';

import { ApiProblem } from '$lib/api/problem';
import { failureOf, visionCorrespondences } from '$lib/vision/calibration';

import { DEFAULT_PANORAMA, flipGray, panoramaGeometry, type GrayImage } from '../panorama';
import { aiMethod, scaleAnswer, type CorrespondenceAnswer } from './ai';
import { CalibrationFlow, type FlowDeps } from './flow.svelte';
import {
  affineOf,
  meshError,
  meshGrid,
  mirrored,
  plausible,
  seamBand,
  stitchGeometry,
  triangleAffine,
  warpMesh,
} from './geometry';
import {
  apply,
  fitAffine,
  fitHomography,
  fromUnit,
  homographyOf,
  IDENTITY,
  invert,
  isAffine,
  mirrorX,
  multiply,
  normalize,
  toUnit,
  translation,
  type Correspondence,
  type Homography,
  type Point,
} from './homography';
import { manualHomography, manualMethod } from './manual';
import type { CalibrationMethods } from './methods';
import {
  degenerate,
  ransacHomography,
  robustHomography,
  seededRandom,
  supportConfidence,
} from './ransac';
import {
  calibrationOf,
  defaultCalibration,
  loadCalibration,
  manualCalibration,
  normalizeCalibration,
  panoramaKey,
  saveCalibration,
  sliderSettings,
} from './storage';
import { CalibrationError, type CalibrationFrame, type CalibrationMethod } from './types';

const close = (a: readonly number[], b: readonly number[], digits = 6) =>
  a.forEach((v, i) => expect(v).toBeCloseTo(b[i]!, digits));

/** A mild perspective: shift, roll and a little keystone. */
const PERSPECTIVE: Homography = [0.98, -0.04, 420, 0.03, 1.01, 12, 0.00004, -0.00002, 1];

function grid(h: Homography, n = 6): Correspondence[] {
  const pairs: Correspondence[] = [];
  for (let i = 0; i < n; i += 1) {
    for (let j = 0; j < n; j += 1) {
      const right: Point = [20 + i * 50 + ((j * 7) % 11), 30 + j * 40 + ((i * 5) % 9)];
      pairs.push({ left: apply(h, right)!, right });
    }
  }
  return pairs;
}

function memoryStorage() {
  const store = new Map<string, string>();
  return {
    store,
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
}

describe('homography math', () => {
  it('composes, inverts and converts between pixel and unit coordinates', () => {
    const inv = invert(PERSPECTIVE)!;
    close(normalize(multiply(PERSPECTIVE, inv))!, IDENTITY);
    const unit = toUnit(PERSPECTIVE, 640, 480);
    close(fromUnit(unit, 640, 480), PERSPECTIVE);
    const [x, y] = apply(PERSPECTIVE, [320, 240])!;
    const [ux, uy] = apply(unit, [0.5, 0.5])!;
    expect(ux * 640).toBeCloseTo(x, 6);
    expect(uy * 480).toBeCloseTo(y, 6);
    expect(invert([1, 2, 3, 2, 4, 6, 0, 0, 1])).toBeNull();
    expect(homographyOf([2, 0, 0, 0, 2, 0, 0, 0, 2])).toEqual([1, 0, 0, 0, 1, 0, 0, 0, 1]);
    expect(homographyOf([1, 0, 0])).toBeNull();
    expect(homographyOf([1, 0, 0, 0, 1, 0, 0, 0, Number.NaN])).toBeNull();
    expect(apply([1, 0, 0, 0, 1, 0, -1, 0, 1], [2, 0])).toBeNull();
  });

  it('tells affine from perspective homographies over a frame', () => {
    expect(isAffine(translation(500, 3), 640, 480)).toBe(true);
    expect(isAffine(mirrorX(640), 640, 480)).toBe(true);
    expect(isAffine(PERSPECTIVE, 640, 480)).toBe(false);
  });

  it('fits a homography exactly from four pairs and by least squares from many', () => {
    const pairs = grid(PERSPECTIVE);
    close(fitHomography(pairs.slice(0, 4))!, PERSPECTIVE, 4);
    close(fitHomography(pairs)!, PERSPECTIVE, 6);
    expect(fitHomography(pairs.slice(0, 3))).toBeNull();
    const line = [0, 1, 2, 3].map((i) => ({ left: [i, i] as Point, right: [i, i] as Point }));
    expect(fitHomography(line)).toBeNull();
    const affine: Homography = [0.99, -0.05, 400, 0.05, 0.99, -8, 0, 0, 1];
    close(fitAffine(grid(affine))!, affine, 6);
    expect(fitAffine(pairs.slice(0, 2))).toBeNull();
  });
});

describe('RANSAC', () => {
  it('rejects outliers and recovers the homography', () => {
    const random = seededRandom(9);
    const pairs = grid(PERSPECTIVE).map((p) => ({
      left: [p.left[0] + (random() - 0.5) * 0.6, p.left[1] + (random() - 0.5) * 0.6] as Point,
      right: p.right,
    }));
    // A third of wrong matches anywhere in the frame.
    for (let i = 0; i < 18; i += 1) {
      pairs.push({
        left: [random() * 640, random() * 480],
        right: [random() * 640, random() * 480],
      });
    }
    const fit = ransacHomography(pairs, { random: seededRandom(1) })!;
    expect(fit.model).toBe('homography');
    expect(fit.inlierCount).toBe(36);
    expect(fit.inliers.slice(36).every((inside) => !inside)).toBe(true);
    expect(fit.rmse).toBeLessThan(0.6);
    const [x, y] = apply(fit.homography, [600, 450])!;
    const [tx, ty] = apply(PERSPECTIVE, [600, 450])!;
    expect(Math.hypot(x - tx, y - ty)).toBeLessThan(2);
  });

  it('gives no result without consensus and skips collinear samples', () => {
    const random = seededRandom(4);
    const noise = Array.from({ length: 30 }, () => ({
      left: [random() * 640, random() * 480] as Point,
      right: [random() * 640, random() * 480] as Point,
    }));
    expect(ransacHomography(noise, { random: seededRandom(2), minInliers: 10 })).toBeNull();
    expect(ransacHomography(noise.slice(0, 3))).toBeNull();
    const line = [0, 1, 2, 3].map((i) => ({
      left: [i * 10, 5] as Point,
      right: [i * 10, 7] as Point,
    }));
    expect(degenerate(line)).toBe(true);
    expect(degenerate(grid(PERSPECTIVE).slice(0, 4))).toBe(false);
  });

  it('prefers the affine map unless the homography explains clearly more', () => {
    const shift: Homography = [1, 0, 450, 0, 1, 4, 0, 0, 1];
    const random = seededRandom(5);
    const jittered = grid(shift).map((p) => ({
      left: [p.left[0] + (random() - 0.5), p.left[1] + (random() - 0.5)] as Point,
      right: p.right,
    }));
    expect(robustHomography(jittered, { random: seededRandom(3) })?.model).toBe('affine');
    // Strong keystone over the whole frame: only the homography fits.
    const keystone: Homography = [1, 0, 300, 0, 1, 0, 0.0008, 0, 1];
    const fit = robustHomography(grid(keystone, 8), { random: seededRandom(3) });
    expect(fit?.model).toBe('homography');
  });

  it('rates a fit by its inlier share, damped below the full support', () => {
    expect(supportConfidence(40, 40, 40)).toBe(1);
    expect(supportConfidence(20, 40, 40)).toBeCloseTo(0.25, 6);
    expect(supportConfidence(8, 10, 10)).toBeCloseTo(0.64, 6);
    expect(supportConfidence(0, 0, 10)).toBe(0);
  });
});

describe('stitch geometry', () => {
  it('gives the slider layout for a slider calibration', () => {
    for (const settings of [
      { overlap: 0.25, offset: 0, mirror: false },
      { overlap: 0.12, offset: 0.1, mirror: true },
      { overlap: 0.3, offset: -0.05, mirror: false },
    ]) {
      const geometry = stitchGeometry(640, 480, manualHomography(settings))!;
      const slider = panoramaGeometry(640, 480, settings);
      expect(geometry.width).toBe(slider.width);
      expect(geometry.height).toBe(slider.height);
      expect(geometry.origin).toEqual([slider.left.x, slider.left.y]);
      expect(geometry.affine).toBe(true);
      const topLeft = apply(geometry.right, settings.mirror ? [640, 0] : [0, 0])!;
      expect(Math.abs(topLeft[0] - slider.right.x)).toBeLessThanOrEqual(1);
      expect(Math.abs(topLeft[1] - slider.right.y)).toBeLessThanOrEqual(1);
      // A slider overlap is blended across its whole width.
      const feather = geometry.feather!;
      expect(Math.abs(feather.start - slider.right.x)).toBeLessThanOrEqual(1);
      expect(feather.end).toBe(640);
    }
    expect(
      stitchGeometry(640, 480, manualHomography({ overlap: 0, offset: 0, mirror: false }))!.feather,
    ).toBeNull();
  });

  it('blends a narrow overlap wholly and a stereo pair only at the left frame edge', () => {
    expect(seamBand(540, 640, 640)).toEqual({ start: 540, end: 640 });
    expect(seamBand(320, 640, 640)).toEqual({ start: 320, end: 640 });
    expect(seamBand(640, 640, 640)).toBeNull();
    // Two cameras seeing nearly the same scene: the left frame stays, the right one fades in
    // over the last 64 columns and adds only the strip beyond the left frame.
    expect(seamBand(40, 640, 640)).toEqual({ start: 576, end: 640 });
    expect(seamBand(0, 1280, 1280)).toEqual({ start: 1216, end: 1280 });
  });

  it('spans the warped frame, keeps sane bounds and refuses points at infinity', () => {
    const unit = toUnit(PERSPECTIVE, 640, 480);
    const geometry = stitchGeometry(640, 480, unit)!;
    expect(geometry.affine).toBe(false);
    const xs = geometry.quad.map((p) => p[0]);
    expect(Math.max(...xs)).toBeLessThanOrEqual(geometry.width + 1);
    expect(geometry.width).toBeGreaterThan(1000);
    expect(geometry.height).toBeLessThanOrEqual(480);
    const far = stitchGeometry(640, 480, [1, 0, 9, 0, 1, 0, 0, 0, 1])!;
    // Far to the right: cut at two frames (three when it also reaches left of the left frame).
    expect(far.width).toBe(2 * 640);
    expect(stitchGeometry(640, 480, [1, 0, 0, 0, 1, 0, -2, 0, 1])).toBeNull();
    expect(affineOf([2, 0, 5, 0, 2, 7, 0, 0, 1])).toEqual([2, 0, 0, 2, 5, 7]);
  });

  it('cuts a perspective warp into exact triangles fine enough for half a pixel', () => {
    const h = stitchGeometry(640, 480, toUnit(PERSPECTIVE, 640, 480))!.right;
    const { columns, rows } = meshGrid(h, 640, 480);
    expect(columns).toBeGreaterThanOrEqual(2);
    expect(columns).toBeLessThanOrEqual(32);
    expect(meshError(h, 640, 480, columns, rows)).toBeLessThanOrEqual(0.5);
    const mesh = warpMesh(h, 640, 480, columns, rows)!;
    expect(mesh).toHaveLength(columns * rows * 2);
    // Every triangle's transform maps its own corners exactly onto the homography.
    const first = mesh[0]!;
    const [a, b, c, d, e, f] = first.transform;
    const [x, y] = [640 / columns, 0];
    close([a * x + c * y + e, b * x + d * y + f], apply(h, [x, y])!, 6);
    expect(meshGrid(translation(5, 5), 640, 480)).toEqual({ columns: 2, rows: 2 });
    expect(
      triangleAffine(
        [
          [0, 0],
          [1, 0],
          [2, 0],
        ],
        [
          [0, 0],
          [1, 0],
          [2, 0],
        ],
      ),
    ).toBeNull();
  });

  it('accepts side-by-side calibrations and rejects wild ones', () => {
    expect(plausible(manualHomography(DEFAULT_PANORAMA))).toBe(true);
    expect(plausible(manualHomography({ overlap: 0.2, offset: 0, mirror: true }))).toBe(true);
    expect(mirrored(manualHomography({ overlap: 0.2, offset: 0, mirror: true }))).toBe(true);
    expect(plausible([0.2, 0, 0.5, 0, 0.2, 0, 0, 0, 1])).toBe(false); // shrunk to 4 %
    expect(plausible([1, 0, 5, 0, 1, 0, 0, 0, 1])).toBe(false); // far away
    expect(plausible([1, 0, 0, 0, 1, 0, -2, 0, 1])).toBe(false); // corner at infinity
  });
});

describe('stored calibration', () => {
  it('migrates slider settings stored before calibrations into a manual calibration', () => {
    const storage = memoryStorage();
    storage.setItem(
      panoramaKey('m1'),
      JSON.stringify({ overlap: 0.2, offset: -0.05, mirror: true }),
    );
    const loaded = loadCalibration('m1', storage);
    expect(loaded).toMatchObject({
      method: 'manual',
      overlapPercent: 20,
      offsetPercent: -5,
      mirror: true,
      capturedAt: null,
      confidence: 1,
    });
    expect(loaded.homography).toEqual(
      manualHomography({ overlap: 0.2, offset: -0.05, mirror: true }),
    );
    expect(sliderSettings(loaded)).toEqual({ overlap: 0.2, offset: -0.05, mirror: true });
  });

  it('keeps calibrations per mower and drops the entry for the default sliders', () => {
    const storage = memoryStorage();
    expect(loadCalibration('m1', storage)).toEqual(defaultCalibration());
    const unit = toUnit(PERSPECTIVE, 640, 480);
    const applied = calibrationOf(
      { method: 'features', homography: unit, confidence: 0.8, inliers: 42, matches: 60 },
      new Date('2026-10-13T08:00:00Z'),
    );
    saveCalibration('m1', applied, storage);
    const raw = JSON.parse(storage.store.get('mowdeck.camera.panorama.m1') ?? '{}');
    expect(raw).toMatchObject({ version: 2, method: 'features', inliers: 42, confidence: 0.8 });
    const loaded = loadCalibration('m1', storage);
    expect(loaded.method).toBe('features');
    expect(loaded.capturedAt).toBe('2026-10-13T08:00:00.000Z');
    close(loaded.homography, unit);
    expect(loadCalibration('m2', storage)).toEqual(defaultCalibration());
    saveCalibration('m1', manualCalibration(DEFAULT_PANORAMA), storage);
    expect(storage.store.has('mowdeck.camera.panorama.m1')).toBe(false);
  });

  it('reads anything broken as a usable calibration', () => {
    expect(normalizeCalibration(null)).toEqual(defaultCalibration());
    expect(
      normalizeCalibration({ version: 2, method: 'ai', homography: [1, 0, 9, 0, 1, 0, 0, 0, 1] }),
    ).toMatchObject({ method: 'manual' });
    expect(normalizeCalibration({ version: 2, method: 'nope', overlapPercent: 30 })).toMatchObject({
      method: 'manual',
      overlapPercent: 30,
    });
    const storage = memoryStorage();
    storage.store.set(panoramaKey('m1'), '{not json');
    expect(loadCalibration('m1', storage)).toEqual(defaultCalibration());
    const broken = {
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => {
        throw new Error('denied');
      },
      removeItem: () => {
        throw new Error('denied');
      },
    };
    expect(loadCalibration('m1', broken)).toEqual(defaultCalibration());
    expect(() =>
      saveCalibration('m1', manualCalibration({ ...DEFAULT_PANORAMA, overlap: 0.3 }), broken),
    ).not.toThrow();
    expect(loadCalibration('m1', null)).toEqual(defaultCalibration());
  });
});

function frame(width = 640, height = 480): CalibrationFrame {
  return {
    width,
    height,
    gray: { width, height, data: new Float32Array(width * height) },
    capturedAt: 1_000,
    jpeg: async () => 'anBlZw==',
  };
}

const OPTIONS = { mirror: false, manual: { overlap: 0.3, offset: 0.02, mirror: false } };

describe('manual method', () => {
  it('hands back the sliders as a translation homography', async () => {
    const result = await manualMethod(() => 'manual').estimate(frame(), frame(), OPTIONS);
    expect(result).toMatchObject({
      method: 'manual',
      confidence: 1,
      inliers: 0,
      overlapPercent: 30,
      offsetPercent: 2,
    });
    close(apply(result.homography, [0, 0])!, [0.7, 0.02]);
  });
});

describe('ai method', () => {
  /** Model points of a 70 % shift seen at half resolution, two of them misplaced. */
  function answer(): CorrespondenceAnswer {
    const truth = translation(224, 5); // in the model's 320 x 240 pixels
    const points = [
      [10, 20],
      [40, 200],
      [70, 110],
      [20, 160],
      [85, 40],
      [55, 230],
      [30, 80],
      [90, 190],
    ].map(([x, y]) => ({ right: [x!, y!] as Point, left: apply(truth, [x!, y!])!, label: 'p' }));
    points.push({ right: [12, 12], left: [300, 230], label: 'wrong' });
    points.push({ right: [80, 220], left: [5, 5], label: 'wrong' });
    return {
      points,
      leftSize: { width: 320, height: 240 },
      rightSize: { width: 320, height: 240 },
      model: 'fake/recover',
      costUsd: 0.0072,
    };
  }

  it('fits the homography from the model points and ignores the misplaced ones', async () => {
    const source = { correspondences: vi.fn(async () => answer()) };
    const result = await aiMethod(() => 'ai', source).estimate(frame(), frame(), OPTIONS);
    expect(source.correspondences).toHaveBeenCalledWith(
      { data: 'anBlZw==', side: 'left', capturedAt: 1_000 },
      { data: 'anBlZw==', side: 'right', capturedAt: 1_000 },
      undefined,
    );
    expect(result).toMatchObject({
      method: 'ai',
      inliers: 8,
      matches: 10,
      model: 'fake/recover',
      costUsd: 0.0072,
    });
    // 8 of 10 points agree, and 8 is below the full support of 10 points: 0.8 * 0.8.
    expect(result.confidence).toBeCloseTo(0.64, 5);
    // 224 of 320 model pixels is 70 % of the width, 5 of 240 about 2 % of the height.
    close(apply(result.homography, [0, 0])!, [0.7, 5 / 240], 4);
    expect(scaleAnswer(answer(), 640, 480)[0]).toEqual({ left: [468, 50], right: [20, 40] });
  });

  it('says why when the points do not make a calibration', async () => {
    const few = { ...answer(), points: answer().points.slice(0, 3) };
    const method = aiMethod(() => 'ai', { correspondences: async () => few });
    await expect(method.estimate(frame(), frame(), OPTIONS)).rejects.toMatchObject({
      reason: 'no_match',
    });
    const failing = aiMethod(() => 'ai', {
      correspondences: async () => {
        throw new CalibrationError('provider_missing');
      },
    });
    await expect(failing.estimate(frame(), frame(), OPTIONS)).rejects.toMatchObject({
      reason: 'provider_missing',
    });
  });

  it('hands the signal to the source and gives up once it is aborted', async () => {
    const controller = new AbortController();
    const source = {
      correspondences: vi.fn(
        (_left: unknown, _right: unknown, signal?: AbortSignal) =>
          new Promise<CorrespondenceAnswer>((_, reject) => {
            signal?.addEventListener('abort', () => reject(signal.reason as Error));
          }),
      ),
    };
    const pending = aiMethod(() => 'ai', source).estimate(frame(), frame(), {
      ...OPTIONS,
      signal: controller.signal,
    });
    await vi.waitFor(() => expect(source.correspondences).toHaveBeenCalledOnce());
    expect(source.correspondences.mock.calls[0]?.[2]).toBe(controller.signal);
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    // A source that answers although the run was aborted does not produce a result either.
    const late = aiMethod(() => 'ai', { correspondences: async () => answer() });
    const aborted = new AbortController();
    aborted.abort();
    await expect(
      late.estimate(frame(), frame(), { ...OPTIONS, signal: aborted.signal }),
    ).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('turns Vision problems into calibration failures', async () => {
    const problem = (slug: string, status: number) =>
      new ApiProblem({ type: `urn:mowdeck:problem:${slug}`, status, detail: slug });
    expect(failureOf(problem('vision-provider-missing', 409))).toBe('provider_missing');
    expect(failureOf(problem('vision-quota', 429))).toBe('quota');
    expect(failureOf(problem('feature-not-in-plan', 403))).toBe('not_in_plan');
    expect(failureOf(problem('vision-provider-failed', 502))).toBe('failed');
    // Vision's time budget, the browser's deadline or no connection: the same advice.
    expect(failureOf(problem('vision-provider-timeout', 504))).toBe('unreachable');
    expect(failureOf(ApiProblem.network())).toBe('unreachable');
    const body = {
      points: [{ left: [500, 40], right: [10, 40], label: 'post' }],
      left_size: { width: 640, height: 480 },
      right_size: { width: 640, height: 480 },
      model: 'fake/recover',
      cost: { input_tokens: 1, output_tokens: 1, cost_usd: 0.01, latency_ms: 5, hosted: true },
      dropped: 0,
    };
    const POST = vi.fn(async () => ({ data: body, response: new Response(null, { status: 200 }) }));
    const source = visionCorrespondences('m1', { POST } as never);
    const signal = new AbortController().signal;
    const got = await source.correspondences(
      { data: 'a', side: 'left', capturedAt: 0 },
      { data: 'b', side: 'right', capturedAt: 0 },
      signal,
    );
    expect(got).toEqual({
      points: [{ left: [500, 40], right: [10, 40], label: 'post' }],
      leftSize: { width: 640, height: 480 },
      rightSize: { width: 640, height: 480 },
      model: 'fake/recover',
      costUsd: 0.01,
    });
    const [path, init] = POST.mock.calls[0] as unknown as [
      string,
      { body: Record<string, unknown>; signal?: AbortSignal },
    ];
    expect(path).toBe('/api/v1/vision/calibration/correspondences');
    expect(init.body).toMatchObject({
      mower_id: 'm1',
      left: { media_type: 'image/jpeg', side: 'left' },
    });
    // The flow's signal reaches the request, so a discarded run cancels the call.
    expect(init.signal).toBe(signal);
    const refused = visionCorrespondences('m1', {
      POST: async () => ({
        error: { type: 'urn:mowdeck:problem:vision-provider-missing', status: 409, detail: 'none' },
        response: new Response(null, { status: 409 }),
      }),
    } as never);
    await expect(
      refused.correspondences(
        { data: 'a', side: 'left', capturedAt: 0 },
        { data: 'b', side: 'right', capturedAt: 0 },
      ),
    ).rejects.toMatchObject({ reason: 'provider_missing' });
  });
});

describe('calibration flow', () => {
  function methods(estimate: CalibrationMethod['estimate']): CalibrationMethods {
    const method = (id: CalibrationMethod['id']): CalibrationMethod => ({
      id,
      label: () => id,
      estimate,
    });
    return {
      manual: method('manual'),
      correlation: method('correlation'),
      features: method('features'),
      ai: method('ai'),
    };
  }
  const pair = () => ({
    left: frame(),
    right: frame(),
    leftCanvas: {} as HTMLCanvasElement,
    rightCanvas: {} as HTMLCanvasElement,
    at: new Date('2026-10-13T09:00:00Z'),
  });
  const blind: FlowDeps['grab'] = () => [null, null];

  /** A textured world two frames wide, cut into a left and a right frame sharing `overlap` columns. */
  function textured(width: number, height: number, overlap: number) {
    const random = seededRandom(11);
    const world = new Float32Array(2 * width * height);
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < 2 * width; x += 1) {
        world[y * 2 * width + x] =
          128 +
          30 * Math.sin(0.11 * x + 0.3 * y) +
          25 * Math.cos(0.07 * x - 0.2 * y) +
          20 * Math.sin(0.23 * x * y * 0.01) +
          (random() - 0.5) * 40;
      }
    }
    const crop = (x0: number): GrayImage => {
      const data = new Float32Array(width * height);
      for (let y = 0; y < height; y += 1) {
        data.set(world.subarray(y * 2 * width + x0, y * 2 * width + x0 + width), y * width);
      }
      return { width, height, data };
    };
    return { left: crop(0), right: crop(width - overlap) };
  }

  it('runs the chosen method on the captured pair and applies the result', async () => {
    const unit = manualHomography({ overlap: 0.2, offset: 0, mirror: false });
    const estimate = vi.fn(async () => ({
      method: 'features' as const,
      homography: unit,
      confidence: 0.9,
      inliers: 30,
      matches: 40,
    }));
    const flow = new CalibrationFlow({
      methods: methods(estimate),
      capture: pair,
      grab: blind,
      options: () => OPTIONS,
    });
    flow.choose('features');
    await flow.run();
    expect(flow.phase).toBe('result');
    expect(estimate).toHaveBeenCalledWith(
      expect.anything(),
      expect.anything(),
      expect.objectContaining({ mirror: false }),
    );
    const applied = flow.apply()!;
    expect(applied).toMatchObject({
      method: 'features',
      confidence: 0.9,
      inliers: 30,
      capturedAt: '2026-10-13T09:00:00.000Z',
    });
    expect(flow.phase).toBe('idle');
    expect(flow.apply()).toBeNull();
  });

  it('reports a missing picture and method failures, and forgets them on discard', async () => {
    const failing = new CalibrationFlow({
      methods: methods(async () => {
        throw new CalibrationError('quota');
      }),
      capture: pair,
      grab: blind,
      options: () => OPTIONS,
    });
    failing.choose('ai');
    await failing.run();
    expect(failing.phase).toBe('error');
    expect(failing.failure).toBe('quota');
    failing.discard();
    expect(failing.failure).toBeNull();
    const noPicture = new CalibrationFlow({
      methods: methods(vi.fn()),
      capture: () => null,
      grab: blind,
      options: () => OPTIONS,
    });
    await noPicture.run();
    expect(noPicture.failure).toBe('no_picture');
    const crashing = new CalibrationFlow({
      methods: methods(async () => {
        throw new Error('boom');
      }),
      capture: pair,
      grab: blind,
      options: () => OPTIONS,
    });
    await crashing.run();
    expect(crashing.failure).toBe('failed');
    expect(crashing.detail).toBe('boom');
    // Switching the method drops a pending result.
    crashing.choose('features');
    expect(crashing.phase).toBe('idle');
  });

  it('aborts a running method on discard and never shows its late answer', async () => {
    let signal: AbortSignal | undefined;
    let settle: ((result: Awaited<ReturnType<CalibrationMethod['estimate']>>) => void) | null =
      null;
    const estimate = vi.fn<CalibrationMethod['estimate']>(
      (_left, _right, options) =>
        new Promise((resolve, reject) => {
          signal = options.signal;
          settle = resolve;
          options.signal?.addEventListener('abort', () => reject(options.signal?.reason as Error));
        }),
    );
    const flow = new CalibrationFlow({
      methods: methods(estimate),
      capture: pair,
      grab: blind,
      options: () => OPTIONS,
    });
    flow.choose('ai');
    const run = flow.run();
    expect(flow.phase).toBe('running');
    expect(signal?.aborted).toBe(false);
    flow.discard();
    expect(signal?.aborted).toBe(true);
    await run;
    expect(flow.phase).toBe('idle');
    expect(flow.candidate).toBeNull();
    expect(flow.failure).toBeNull();
    // A method that ignores the signal and answers later still changes nothing.
    const second = flow.run();
    flow.discard();
    settle!({
      method: 'ai',
      homography: manualHomography(DEFAULT_PANORAMA),
      confidence: 1,
      inliers: 9,
      matches: 9,
    });
    await second;
    expect(flow.phase).toBe('idle');
    expect(flow.candidate).toBeNull();
  });

  it('estimates the overlap for the sliders from the live pictures ("Auto")', () => {
    const { left, right } = textured(160, 90, 32);
    let mirror = false;
    const flow = new CalibrationFlow({
      methods: methods(vi.fn()),
      capture: pair,
      grab: () => [left, mirror ? flipGray(right) : right],
      options: () => ({ mirror, manual: { ...DEFAULT_PANORAMA, mirror } }),
    });
    const overlap = flow.autoOverlap();
    expect(Math.round((overlap ?? 0) * 160)).toBe(32);
    expect(flow.auto).toMatchObject({ kind: 'done', percent: 20 });
    expect(flow.auto?.score).toBeGreaterThan(0.9);
    // A mirrored right camera is flipped back before the match.
    mirror = true;
    expect(Math.round((flow.autoOverlap() ?? 0) * 160)).toBe(32);
    flow.discard();
    expect(flow.auto).toBeNull();
    const dark = new CalibrationFlow({
      methods: methods(vi.fn()),
      capture: pair,
      grab: blind,
      options: () => OPTIONS,
    });
    expect(dark.autoOverlap()).toBeNull();
    expect(dark.auto).toEqual({ kind: 'failed', percent: 0, score: 0 });
  });
});
