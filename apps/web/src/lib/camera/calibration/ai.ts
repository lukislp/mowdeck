/**
 * The `ai` method ("KI-gestützt"): the configured vision model only supplies corresponding
 * points (`POST /api/v1/vision/calibration/correspondences`); the homography is fitted here with
 * the same RANSAC as `features`, which also discards the points the model misplaced. The model
 * sees the raw right frame, so a mirrored camera simply shows up as a flipping homography.
 *
 * The Vision call stays behind {@link CorrespondenceSource}: this module knows no HTTP client
 * (`$lib/vision/calibration.ts` implements it).
 */
import { plausible } from './geometry';
import { toUnit, type Correspondence, type Point } from './homography';
import { robustHomography, seededRandom, supportConfidence } from './ransac';
import { CalibrationError, type CalibrationMethod } from './types';

export interface CorrespondenceImage {
  /** JPEG, base64. */
  data: string;
  side: 'left' | 'right';
  capturedAt: number;
}

export interface CorrespondenceAnswer {
  points: { left: Point; right: Point; label: string }[];
  leftSize: { width: number; height: number };
  rightSize: { width: number; height: number };
  model: string;
  costUsd: number | null;
}

/** Where the points come from (the Vision service). */
export interface CorrespondenceSource {
  /** The points of the pair; an aborted `signal` (the run was discarded) cancels the call. */
  correspondences(
    left: CorrespondenceImage,
    right: CorrespondenceImage,
    signal?: AbortSignal,
  ): Promise<CorrespondenceAnswer>;
}

/** Model points are rough: inlier distance as a fraction of the frame width. */
export const AI_THRESHOLD = 0.02;
export const AI_MIN_INLIERS = 5;
/** Model points at which the confidence stops growing with the count. */
const AI_FULL_SUPPORT = 10;

/** The model's points scaled from the sizes it saw to the frame size of the estimate. */
export function scaleAnswer(
  answer: CorrespondenceAnswer,
  width: number,
  height: number,
): Correspondence[] {
  const lx = width / Math.max(1, answer.leftSize.width);
  const ly = height / Math.max(1, answer.leftSize.height);
  const rx = width / Math.max(1, answer.rightSize.width);
  const ry = height / Math.max(1, answer.rightSize.height);
  return answer.points.map((p) => ({
    left: [p.left[0] * lx, p.left[1] * ly],
    right: [p.right[0] * rx, p.right[1] * ry],
  }));
}

export function aiMethod(label: () => string, source: CorrespondenceSource): CalibrationMethod {
  return {
    id: 'ai',
    label,
    async estimate(left, right, options) {
      const [leftJpeg, rightJpeg] = await Promise.all([left.jpeg(), right.jpeg()]);
      options.signal?.throwIfAborted();
      const answer = await source.correspondences(
        { data: leftJpeg, side: 'left', capturedAt: left.capturedAt },
        { data: rightJpeg, side: 'right', capturedAt: right.capturedAt },
        options.signal,
      );
      options.signal?.throwIfAborted();
      const pairs = scaleAnswer(answer, left.width, left.height);
      const fit = robustHomography(pairs, {
        threshold: AI_THRESHOLD * left.width,
        minInliers: AI_MIN_INLIERS,
        random: seededRandom(0xa1),
      });
      if (!fit) throw new CalibrationError('no_match');
      const homography = toUnit(fit.homography, left.width, left.height);
      if (!plausible(homography)) throw new CalibrationError('implausible');
      return {
        method: 'ai',
        homography,
        confidence: supportConfidence(fit.inlierCount, pairs.length, AI_FULL_SUPPORT),
        inliers: fit.inlierCount,
        matches: pairs.length,
        model: answer.model,
        costUsd: answer.costUsd,
      };
    },
  };
}
