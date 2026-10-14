/**
 * The `features` method ("Automatisch (lokal)"): ORB-style keypoints and descriptors in both
 * frames (`features.ts`, loaded on demand as its own chunk), brute-force Hamming matching with
 * the ratio test, and a RANSAC homography (`ransac.ts`). Everything runs in the browser; no
 * picture leaves it. When too few matches agree (a flat lawn, a blurred picture) it falls back
 * to `correlation` and says so in the result.
 *
 * A mirrored right camera is flipped back before matching (descriptors are not mirror
 * invariant) and the flip is folded into the homography.
 */
import { flipGray } from '../panorama';
import { plausible } from './geometry';
import { mirrorX, multiply, normalize, toUnit, type Correspondence } from './homography';
import { robustHomography, seededRandom, supportConfidence } from './ransac';
import {
  CalibrationError,
  type CalibrationFrame,
  type CalibrationMethod,
  type CalibrationOptions,
  type CalibrationResult,
} from './types';

/** Fewer RANSAC inliers than this is no calibration (then `correlation` is tried). */
export const FEATURES_MIN_INLIERS = 12;
/** Inliers at which the confidence stops growing with the count. */
const FULL_SUPPORT = 40;
/** Inlier distance at 640 px frame width (scaled with the frame). */
const THRESHOLD_AT_640 = 3;

type Matcher = (
  left: CalibrationFrame['gray'],
  right: CalibrationFrame['gray'],
) => { pairs: Correspondence[] };

/** The matcher chunk, loaded the first time someone calibrates. */
async function loadMatcher(): Promise<Matcher> {
  const { featureCorrespondences } = await import('./features');
  return featureCorrespondences;
}

export interface FeaturesMethodDeps {
  label: () => string;
  /** The method to fall back on (`correlation`). */
  fallback?: CalibrationMethod;
  /** Override the matcher (tests). */
  matcher?: () => Promise<Matcher>;
  /** Seed of the RANSAC sampling (fixed: the same pair gives the same calibration). */
  seed?: number;
}

export function featuresMethod(deps: FeaturesMethodDeps): CalibrationMethod {
  const load = deps.matcher ?? loadMatcher;
  async function local(
    left: CalibrationFrame,
    right: CalibrationFrame,
    options: CalibrationOptions,
  ): Promise<CalibrationResult | null> {
    const match = await load();
    options.signal?.throwIfAborted();
    const rightGray = options.mirror ? flipGray(right.gray) : right.gray;
    const { pairs } = match(left.gray, rightGray);
    const fit = robustHomography(pairs, {
      threshold: THRESHOLD_AT_640 * (left.width / 640),
      minInliers: FEATURES_MIN_INLIERS,
      random: seededRandom(deps.seed ?? 0x5eed),
    });
    if (!fit) return null;
    const pixel = options.mirror
      ? normalize(multiply(fit.homography, mirrorX(right.width)))
      : fit.homography;
    if (!pixel) return null;
    const homography = toUnit(pixel, left.width, left.height);
    if (!plausible(homography)) return null;
    return {
      method: 'features',
      homography,
      confidence: supportConfidence(fit.inlierCount, pairs.length, FULL_SUPPORT),
      inliers: fit.inlierCount,
      matches: pairs.length,
    };
  }
  return {
    id: 'features',
    label: deps.label,
    async estimate(left, right, options) {
      const result = await local(left, right, options);
      if (result) return result;
      if (!deps.fallback) throw new CalibrationError('no_match');
      const fallback = await deps.fallback.estimate(left, right, options);
      return { ...fallback, fallbackFrom: 'features' };
    },
  };
}
