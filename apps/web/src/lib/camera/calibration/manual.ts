/**
 * The `manual` method: the sliders (overlap, vertical offset, mirror) as a homography. A pure
 * translation (plus the optional flip), so the panorama looks exactly like the slider layout
 * before calibrations existed; `estimate` just hands back the current slider settings.
 */
import { normalizePanorama, type PanoramaSettings } from '../panorama';
import { IDENTITY, multiply, normalize, translation, type Homography } from './homography';
import type { CalibrationMethod, CalibrationResult } from './types';

/** Unit homography of slider settings: right x 0 lands at `1 - overlap`, row 0 at `offset`. */
export function manualHomography(settings: PanoramaSettings): Homography {
  const { overlap, offset, mirror } = normalizePanorama(settings);
  const flip: Homography = mirror ? [-1, 0, 1, 0, 1, 0, 0, 0, 1] : IDENTITY;
  return normalize(multiply(translation(1 - overlap, offset), flip))!;
}

/** The result for slider settings (also used by `correlation`). */
export function translationResult(
  method: CalibrationResult['method'],
  settings: PanoramaSettings,
  confidence: number,
): CalibrationResult {
  const value = normalizePanorama(settings);
  return {
    method,
    homography: manualHomography(value),
    confidence,
    inliers: 0,
    matches: 0,
    overlapPercent: Math.round(value.overlap * 100),
    offsetPercent: Math.round(value.offset * 100),
  };
}

export function manualMethod(label: () => string): CalibrationMethod {
  return {
    id: 'manual',
    label,
    estimate: async (_left, _right, options) => translationResult('manual', options.manual, 1),
  };
}
