/**
 * The calibration methods of the camera panorama behind one interface (ADR 0037).
 *
 * Every method turns a left and a right frame of the same moment into a homography that maps the
 * right frame onto the left one (unit coordinates, see `homography.ts`), with a confidence and
 * the number of point correspondences that support it. The UI, the storage and the renderer only
 * ever see a {@link CalibrationResult} or a stored {@link PanoramaCalibration}; how a method gets
 * there (sliders, a correlation search, local features, points from a vision model) stays inside
 * the method.
 */
import type { GrayImage, PanoramaSettings } from '../panorama';
import type { Homography } from './homography';

/** `correlation` is not offered on its own: it is the fallback of `features`. */
export type CalibrationMethodId = 'manual' | 'correlation' | 'features' | 'ai';

/** The methods the user can choose, in the chooser's order. */
export const CHOOSABLE_METHODS = ['manual', 'features', 'ai'] as const;
export type ChoosableMethod = (typeof CHOOSABLE_METHODS)[number];

/** One captured camera frame, ready for every method. */
export interface CalibrationFrame {
  /** The frame size the estimate works in (the captured copy, at most a few hundred pixels). */
  width: number;
  height: number;
  /** Luminance of the captured copy, `width` x `height`. */
  gray: GrayImage;
  /** When it was grabbed (epoch milliseconds). */
  capturedAt: number;
  /** The captured copy as a JPEG, base64 (for the `ai` method). */
  jpeg(): Promise<string>;
}

export interface CalibrationOptions {
  /** The right camera delivers a mirrored picture (the panel's switch). */
  mirror: boolean;
  /** The slider settings (the `manual` method's input, the correlation's vertical offset). */
  manual: PanoramaSettings;
  signal?: AbortSignal;
}

export interface CalibrationResult {
  /** The method that produced the homography (`correlation` when `features` fell back). */
  method: CalibrationMethodId;
  /** Right frame to left frame, unit coordinates. */
  homography: Homography;
  /** 0 to 1: how much the estimate can be trusted. */
  confidence: number;
  /** Correspondences that agree with the homography (0 for the slider-based methods). */
  inliers: number;
  /** Correspondences the method had to choose from (matches or model points). */
  matches: number;
  /** For the translation-only methods: what the sliders would show. */
  overlapPercent?: number;
  offsetPercent?: number;
  /** Set when the chosen method could not deliver and another one did. */
  fallbackFrom?: CalibrationMethodId;
  /** The vision model that supplied the points (`ai`). */
  model?: string;
  /** What the model call cost (USD; null when the model has no price). */
  costUsd?: number | null;
}

export interface CalibrationMethod {
  readonly id: CalibrationMethodId;
  /** The method's name in the UI. */
  label(): string;
  estimate(
    left: CalibrationFrame,
    right: CalibrationFrame,
    options: CalibrationOptions,
  ): Promise<CalibrationResult>;
}

/** Why a method could not deliver a calibration. */
export type CalibrationFailure =
  | 'no_picture'
  | 'no_match'
  | 'implausible'
  | 'provider_missing'
  | 'quota'
  | 'not_in_plan'
  | 'unreachable'
  | 'failed';

export class CalibrationError extends Error {
  constructor(
    readonly reason: CalibrationFailure,
    message: string = reason,
  ) {
    super(message);
    this.name = 'CalibrationError';
  }
}

/** The calibration kept per mower (`mowdeck.camera.panorama.<mowerId>`). */
export interface PanoramaCalibration {
  method: CalibrationMethodId;
  /** Right frame to left frame, unit coordinates. */
  homography: Homography;
  /** The sliders' values (percent of the frame width and height) for translation methods. */
  overlapPercent?: number;
  offsetPercent?: number;
  mirror: boolean;
  /** When the pair was captured (ISO); null for slider settings. */
  capturedAt: string | null;
  confidence: number;
  inliers?: number;
}
