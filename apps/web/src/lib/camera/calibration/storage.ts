/**
 * The panorama calibration kept per mower in `localStorage` (`mowdeck.camera.panorama.<id>`,
 * the key the slider settings used). Stored as `{version: 2, method, homography, ...}`; an
 * entry from before calibrations (`{overlap, offset, mirror}`) reads as a `manual` calibration
 * with the equivalent translation homography, so nobody loses their sliders. The default slider
 * settings remove the entry. Anything broken reads as the default. Pure apart from the storage
 * handed in.
 */
import { DEFAULT_PANORAMA, normalizePanorama, type PanoramaSettings } from '../panorama';
import { mirrored, plausible } from './geometry';
import { homographyOf } from './homography';
import { manualHomography } from './manual';
import type { CalibrationMethodId, CalibrationResult, PanoramaCalibration } from './types';

export const PANORAMA_KEY_PREFIX = 'mowdeck.camera.panorama.';
export const CALIBRATION_VERSION = 2;
const METHODS: readonly CalibrationMethodId[] = ['manual', 'correlation', 'features', 'ai'];

export function panoramaKey(mowerId: string): string {
  return `${PANORAMA_KEY_PREFIX}${mowerId}`;
}

/** A `manual` calibration from slider settings. */
export function manualCalibration(settings: PanoramaSettings): PanoramaCalibration {
  const value = normalizePanorama(settings);
  return {
    method: 'manual',
    homography: manualHomography(value),
    overlapPercent: Math.round(value.overlap * 100),
    offsetPercent: Math.round(value.offset * 100),
    mirror: value.mirror,
    capturedAt: null,
    confidence: 1,
  };
}

export function defaultCalibration(): PanoramaCalibration {
  return manualCalibration(DEFAULT_PANORAMA);
}

/** The slider settings of a calibration (the stored percentages; defaults otherwise). */
export function sliderSettings(calibration: PanoramaCalibration): PanoramaSettings {
  return normalizePanorama({
    overlap: (calibration.overlapPercent ?? DEFAULT_PANORAMA.overlap * 100) / 100,
    offset: (calibration.offsetPercent ?? DEFAULT_PANORAMA.offset * 100) / 100,
    mirror: calibration.mirror,
  });
}

/** The calibration a method's result becomes when the user applies it. */
export function calibrationOf(result: CalibrationResult, capturedAt: Date): PanoramaCalibration {
  const translationOnly = result.overlapPercent !== undefined;
  return {
    method: result.method,
    homography: result.homography,
    ...(translationOnly
      ? { overlapPercent: result.overlapPercent, offsetPercent: result.offsetPercent ?? 0 }
      : {}),
    mirror: mirrored(result.homography),
    capturedAt: capturedAt.toISOString(),
    confidence: Math.max(0, Math.min(1, result.confidence)),
    inliers: result.inliers,
  };
}

function finiteOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

/** A valid calibration from anything stored (version 2, the old slider settings, garbage). */
export function normalizeCalibration(raw: unknown): PanoramaCalibration {
  if (!raw || typeof raw !== 'object') return defaultCalibration();
  const value = raw as Record<string, unknown>;
  if (value.version !== CALIBRATION_VERSION) {
    // Before calibrations: {overlap, offset, mirror} fractions.
    return manualCalibration(normalizePanorama(value));
  }
  const method = METHODS.includes(value.method as CalibrationMethodId)
    ? (value.method as CalibrationMethodId)
    : 'manual';
  const overlapPercent = finiteOr(value.overlapPercent, DEFAULT_PANORAMA.overlap * 100);
  const offsetPercent = finiteOr(value.offsetPercent, 0);
  const sliders = normalizePanorama({
    overlap: overlapPercent / 100,
    offset: offsetPercent / 100,
    mirror: value.mirror === true,
  });
  if (method === 'manual') return manualCalibration(sliders);
  const homography = homographyOf(value.homography);
  if (!homography || !plausible(homography)) return manualCalibration(sliders);
  const translationOnly = method === 'correlation';
  return {
    method,
    homography,
    ...(translationOnly
      ? {
          overlapPercent: Math.round(sliders.overlap * 100),
          offsetPercent: Math.round(sliders.offset * 100),
        }
      : {}),
    mirror: mirrored(homography),
    capturedAt: typeof value.capturedAt === 'string' ? value.capturedAt : null,
    confidence: Math.max(0, Math.min(1, finiteOr(value.confidence, 0))),
    ...(typeof value.inliers === 'number' && value.inliers >= 0
      ? { inliers: Math.round(value.inliers) }
      : {}),
  };
}

/** True for the default slider settings (nothing worth storing). */
export function isDefaultCalibration(calibration: PanoramaCalibration): boolean {
  const base = defaultCalibration();
  return (
    calibration.method === 'manual' &&
    calibration.overlapPercent === base.overlapPercent &&
    calibration.offsetPercent === base.offsetPercent &&
    calibration.mirror === base.mirror
  );
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

/** The mower's calibration (the default when none or unreadable). */
export function loadCalibration(
  mowerId: string,
  storage: Pick<Storage, 'getItem'> | null = safeStorage(),
): PanoramaCalibration {
  try {
    const raw = storage?.getItem(panoramaKey(mowerId));
    return normalizeCalibration(raw ? JSON.parse(raw) : null);
  } catch {
    return defaultCalibration();
  }
}

/** Store the mower's calibration; the default slider settings remove the entry. */
export function saveCalibration(
  mowerId: string,
  calibration: PanoramaCalibration,
  storage: Pick<Storage, 'setItem' | 'removeItem'> | null = safeStorage(),
): void {
  const value = normalizeCalibration({ ...calibration, version: CALIBRATION_VERSION });
  try {
    if (isDefaultCalibration(value)) storage?.removeItem(panoramaKey(mowerId));
    else
      storage?.setItem(
        panoramaKey(mowerId),
        JSON.stringify({ version: CALIBRATION_VERSION, ...value }),
      );
  } catch {
    // private mode: the calibration lasts until the page closes
  }
}
