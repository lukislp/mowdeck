/**
 * Robust fit of the right-to-left mapping (RANSAC), shared by the `features` and the `ai` method
 * (ADR 0037): draw a minimal sample of correspondences, fit the model exactly, count the pairs
 * it maps within `threshold` pixels; the largest consensus wins and is refitted on all of its
 * inliers (least squares). Wrong matches, whether from the feature matcher or from a vision model
 * that misplaced a point, are simply outvoted.
 *
 * Two models: a full homography (8 degrees of freedom, samples of four) and an affine map (6,
 * samples of three). {@link robustHomography} fits both and keeps the homography only when it
 * explains clearly more of the correspondences. The reason: the two frames share only a strip
 * (the overlap), and the perspective terms fitted from a narrow strip are poorly determined;
 * extrapolated across the whole right frame they bend it far more than the cameras do. The affine
 * map covers shift, roll, scale and shear and extrapolates calmly; a real perspective difference
 * (cameras toed in, a near scene) shows up as many more homography inliers and is then kept.
 *
 * Pure, deterministic with a seeded random source.
 */
import {
  fitAffine,
  fitHomography,
  transferError,
  type Correspondence,
  type Homography,
} from './homography';

export type MotionModel = 'homography' | 'affine';

/**
 * How much a fit can be trusted: the inlier share, damped while the count is below `full` (the
 * inlier count at which a method's confidence stops growing). Shared by every RANSAC method.
 */
export function supportConfidence(inliers: number, matches: number, full: number): number {
  if (matches <= 0) return 0;
  return Math.min(1, inliers / matches) * Math.min(1, inliers / full);
}

export interface RansacOptions {
  /** Inlier distance in pixels (the left frame's). */
  threshold?: number;
  /** Most hypotheses to try. */
  iterations?: number;
  /** Stop early once this probability of having drawn an all-inlier sample is reached. */
  confidence?: number;
  /** Fewer inliers than this: no result. */
  minInliers?: number;
  /** Random numbers in [0, 1) (seeded in tests). */
  random?: () => number;
  /** The model to fit (default: the full homography). */
  model?: MotionModel;
}

export interface RansacResult {
  homography: Homography;
  model: MotionModel;
  /** Per input pair: whether it agrees with the result. */
  inliers: boolean[];
  inlierCount: number;
  /** Root mean square transfer error of the inliers, in pixels. */
  rmse: number;
}

export const RANSAC_THRESHOLD = 3;
export const RANSAC_ITERATIONS = 2000;
export const RANSAC_MIN_INLIERS = 6;
/** The homography must have this many times the affine map's inliers (plus two) to be kept. */
export const HOMOGRAPHY_GAIN = 1.15;

const SAMPLE: Record<MotionModel, number> = { homography: 4, affine: 3 };
const FIT: Record<MotionModel, (pairs: readonly Correspondence[]) => Homography | null> = {
  homography: fitHomography,
  affine: fitAffine,
};

/** A deterministic pseudo-random generator (mulberry32). */
export function seededRandom(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Twice the signed area of a triangle. */
function area2(a: readonly number[], b: readonly number[], c: readonly number[]): number {
  return (b[0]! - a[0]!) * (c[1]! - a[1]!) - (b[1]! - a[1]!) * (c[0]! - a[0]!);
}

/** Any three points of the sample (in either frame) nearly on a line: no usable hypothesis. */
export function degenerate(sample: readonly Correspondence[], minArea = 4): boolean {
  for (const side of ['left', 'right'] as const) {
    const p = sample.map((s) => s[side]);
    for (let i = 0; i < p.length; i += 1) {
      for (let j = i + 1; j < p.length; j += 1) {
        for (let k = j + 1; k < p.length; k += 1) {
          if (Math.abs(area2(p[i]!, p[j]!, p[k]!)) < minArea) return true;
        }
      }
    }
  }
  return false;
}

function consensus(
  h: Homography,
  pairs: readonly Correspondence[],
  threshold2: number,
): { inliers: boolean[]; count: number; error: number } {
  const inliers: boolean[] = [];
  let count = 0;
  let error = 0;
  for (const pair of pairs) {
    const e = transferError(h, pair);
    const inside = e <= threshold2;
    inliers.push(inside);
    if (inside) {
      count += 1;
      error += e;
    }
  }
  return { inliers, count, error };
}

/** The best mapping of one model for the correspondences, or null without enough consensus. */
export function ransacHomography(
  pairs: readonly Correspondence[],
  options: RansacOptions = {},
): RansacResult | null {
  const model = options.model ?? 'homography';
  const size = SAMPLE[model];
  const fit = FIT[model];
  const threshold = options.threshold ?? RANSAC_THRESHOLD;
  const threshold2 = threshold * threshold;
  const minInliers = Math.max(size, options.minInliers ?? RANSAC_MIN_INLIERS);
  const random = options.random ?? Math.random;
  const confidence = options.confidence ?? 0.999;
  let iterations = options.iterations ?? RANSAC_ITERATIONS;
  const n = pairs.length;
  if (n < minInliers) return null;
  let best: { inliers: boolean[]; count: number; error: number } | null = null;
  let bestHomography: Homography | null = null;
  for (let i = 0; i < iterations; i += 1) {
    const picked = new Set<number>();
    while (picked.size < size) picked.add(Math.floor(random() * n));
    const sample = [...picked].map((k) => pairs[k]!);
    if (degenerate(sample)) continue;
    const h = fit(sample);
    if (!h) continue;
    const scored = consensus(h, pairs, threshold2);
    if (
      !best ||
      scored.count > best.count ||
      (scored.count === best.count && scored.error < best.error)
    ) {
      best = scored;
      bestHomography = h;
      // Adaptive stop: enough iterations for the observed inlier ratio.
      const ratio = scored.count / n;
      const needed = Math.log(1 - confidence) / Math.log(1 - ratio ** size + 1e-12);
      if (Number.isFinite(needed)) iterations = Math.min(iterations, Math.ceil(needed) + 10);
    }
  }
  if (!best || best.count < minInliers) return null;
  // Refit on the consensus (twice: the refined model may gain or lose a few pairs).
  let inliers = best.inliers;
  let homography: Homography | null = bestHomography;
  for (let round = 0; round < 2; round += 1) {
    const fitted = fit(pairs.filter((_, k) => inliers[k]));
    if (!fitted) break;
    const scored = consensus(fitted, pairs, threshold2);
    if (scored.count < minInliers) break;
    homography = fitted;
    inliers = scored.inliers;
  }
  if (!homography) return null;
  const final = consensus(homography, pairs, threshold2);
  return {
    homography,
    model,
    inliers: final.inliers,
    inlierCount: final.count,
    rmse: final.count ? Math.sqrt(final.error / final.count) : 0,
  };
}

/**
 * Fit both models and keep the homography only when it has clearly more inliers than the affine
 * map ({@link HOMOGRAPHY_GAIN}); otherwise the affine map (see the module comment).
 */
export function robustHomography(
  pairs: readonly Correspondence[],
  options: Omit<RansacOptions, 'model'> = {},
): RansacResult | null {
  const seed = options.random;
  const affine = ransacHomography(pairs, { ...options, model: 'affine', random: seed });
  const full = ransacHomography(pairs, { ...options, model: 'homography', random: seed });
  if (!affine) return full;
  if (!full) return affine;
  return full.inlierCount >= HOMOGRAPHY_GAIN * affine.inlierCount + 2 ? full : affine;
}
