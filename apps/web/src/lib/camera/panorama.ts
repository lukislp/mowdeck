/**
 * The panorama layout: the left and right front camera blended into one wide picture. A real
 * stitch needs a calibration of both cameras (intrinsics, the angle between them), which we do
 * not have; this is a pragmatic approximation that looks like one:
 *
 * - both frames sit side by side and overlap by `overlap` (a fraction of the frame width, 0 to
 *   {@link OVERLAP_MAX}); in the overlap the right frame fades in with a linear alpha ramp;
 * - `offset` moves the right frame up or down (a fraction of the frame height, ±{@link
 *   OFFSET_MAX}); the picture is cropped to the rows both frames cover, so there are no bands;
 * - `mirror` flips the right frame horizontally (a camera that delivers a mirrored picture).
 *
 * These slider settings are the `manual` calibration method; together with the other methods
 * (local features, AI-assisted points) they live in `calibration/` (ADR 0037), which also keeps
 * the calibration per mower. {@link estimateOverlap} guesses the overlap from the pictures: it compares the right edge of
 * the left frame with the left edge of the right frame (normalised cross-correlation of small
 * greyscale copies) and picks the overlap where they match best.
 *
 * Everything here is pure; the drawing lives in `panorama-renderer.ts`.
 * {@link panoramaGeometry} is the slider layout; the renderer uses the general
 * `calibration/geometry.ts`, which gives the same layout for a slider calibration.
 */

export interface PanoramaSettings {
  /** Overlap of the two frames, a fraction of the frame width (0 to {@link OVERLAP_MAX}). */
  overlap: number;
  /** Vertical shift of the right frame, a fraction of the frame height (positive: down). */
  offset: number;
  /** Flip the right frame horizontally. */
  mirror: boolean;
}

export const OVERLAP_MAX = 0.4;
export const OFFSET_MAX = 0.15;
export const DEFAULT_PANORAMA: Readonly<PanoramaSettings> = {
  overlap: 0.12,
  offset: 0,
  mirror: false,
};

const clamp = (value: number, min: number, max: number): number =>
  Math.min(max, Math.max(min, value));

function finite(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

/** Valid settings from anything (stored JSON, a slider): out-of-range values are clamped. */
export function normalizePanorama(raw: unknown): PanoramaSettings {
  const value = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  return {
    overlap: clamp(finite(value.overlap, DEFAULT_PANORAMA.overlap), 0, OVERLAP_MAX),
    offset: clamp(finite(value.offset, DEFAULT_PANORAMA.offset), -OFFSET_MAX, OFFSET_MAX),
    mirror: value.mirror === true,
  };
}

/** Where a frame lands in the panorama (in frame pixels of the reference size). */
export interface Placement {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface PanoramaGeometry {
  /** The panorama's size in frame pixels. */
  width: number;
  height: number;
  left: Placement;
  right: Placement;
  /** The overlap in frame pixels: the alpha ramp runs from `right.x` to `right.x + overlapPx`. */
  overlapPx: number;
}

/**
 * The layout of the panorama for frames of `width` x `height` (both frames drawn at that size).
 * The right frame starts `overlap * width` before the left frame ends and is shifted by
 * `offset * height`; the result is cropped to the rows both frames cover.
 */
export function panoramaGeometry(
  width: number,
  height: number,
  settings: PanoramaSettings,
): PanoramaGeometry {
  const { overlap, offset } = normalizePanorama(settings);
  const overlapPx = Math.round(overlap * width);
  const shift = Math.round(offset * height);
  const top = Math.max(0, shift);
  return {
    width: Math.max(1, 2 * width - overlapPx),
    height: Math.max(1, height - Math.abs(shift)),
    left: { x: 0, y: top === 0 ? 0 : -top, width, height },
    right: { x: width - overlapPx, y: shift - top, width, height },
    overlapPx,
  };
}

/** A greyscale image (luminance 0 to 255), row by row. */
export interface GrayImage {
  width: number;
  height: number;
  data: ArrayLike<number>;
}

/** Luminance (Rec. 601) of an RGBA buffer, e.g. `ImageData.data`. */
export function toGray(rgba: ArrayLike<number>, width: number, height: number): GrayImage {
  const data = new Float32Array(width * height);
  for (let i = 0; i < data.length; i += 1) {
    const p = i * 4;
    data[i] = 0.299 * (rgba[p] ?? 0) + 0.587 * (rgba[p + 1] ?? 0) + 0.114 * (rgba[p + 2] ?? 0);
  }
  return { width, height, data };
}

/** The image flipped horizontally (to estimate with a mirrored right camera). */
export function flipGray(image: GrayImage): GrayImage {
  const { width, height } = image;
  const data = new Float32Array(width * height);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      data[y * width + x] = image.data[y * width + (width - 1 - x)] ?? 0;
    }
  }
  return { width, height, data };
}

export interface OverlapEstimate {
  /** The estimated overlap, a fraction of the frame width. */
  overlap: number;
  /** The normalised cross-correlation at that overlap (-1 to 1; 1 is a perfect match). */
  score: number;
  /** The best match lies on the edge of the searched range (the true overlap may be outside). */
  atLimit: boolean;
}

export interface EstimateOptions {
  /** The right frame's vertical shift (a fraction of the height), as in the settings. */
  offset?: number;
  /** Smallest overlap tried (a fraction of the width): narrower strips match anything. */
  min?: number;
  /** Largest overlap tried. */
  max?: number;
  /** Below this correlation there is no usable match (null). */
  minScore?: number;
}

/** Overlaps narrower than this (a fraction of the width) are not tried: too few pixels. */
export const ESTIMATE_MIN_OVERLAP = 0.03;
/** A best match below this correlation is no match. */
export const ESTIMATE_MIN_SCORE = 0.5;

/**
 * Normalised cross-correlation of the left frame's last `k` columns with the right frame's first
 * `k` columns, rows matched with the vertical shift `shift` (right row y against left row
 * y + shift). Null when a strip is flat (no texture to compare).
 */
export function stripCorrelation(
  left: GrayImage,
  right: GrayImage,
  k: number,
  shift = 0,
): number | null {
  const height = Math.min(left.height, right.height);
  const y0 = Math.max(0, -shift);
  const y1 = Math.min(height, height - shift);
  if (k < 1 || k > left.width || k > right.width || y1 - y0 < 2) return null;
  const leftStart = left.width - k;
  let n = 0;
  let sumA = 0;
  let sumB = 0;
  let sumAA = 0;
  let sumBB = 0;
  let sumAB = 0;
  for (let y = y0; y < y1; y += 1) {
    const rowA = (y + shift) * left.width + leftStart;
    const rowB = y * right.width;
    for (let x = 0; x < k; x += 1) {
      const a = left.data[rowA + x] ?? 0;
      const b = right.data[rowB + x] ?? 0;
      sumA += a;
      sumB += b;
      sumAA += a * a;
      sumBB += b * b;
      sumAB += a * b;
      n += 1;
    }
  }
  const varA = sumAA - (sumA * sumA) / n;
  const varB = sumBB - (sumB * sumB) / n;
  if (varA <= 1e-6 * n || varB <= 1e-6 * n) return null;
  return (sumAB - (sumA * sumB) / n) / Math.sqrt(varA * varB);
}

/**
 * Estimate the overlap of two frames of the same size: try every overlap from `min` to `max`
 * (in columns of the given, ideally downscaled, images) and keep the one whose edge strips
 * correlate best. Null when nothing matches well enough (flat pictures, no shared content).
 * An approximation: it assumes the cameras look side by side with parallel axes, which the
 * real cameras only roughly do.
 */
export function estimateOverlap(
  left: GrayImage,
  right: GrayImage,
  options: EstimateOptions = {},
): OverlapEstimate | null {
  const width = Math.min(left.width, right.width);
  const height = Math.min(left.height, right.height);
  if (width < 4 || height < 4) return null;
  const shift = Math.round(clamp(options.offset ?? 0, -OFFSET_MAX, OFFSET_MAX) * height);
  const kMin = Math.max(2, Math.ceil((options.min ?? ESTIMATE_MIN_OVERLAP) * width));
  const kMax = Math.min(width, Math.floor((options.max ?? OVERLAP_MAX) * width));
  let best: { k: number; score: number } | null = null;
  for (let k = kMin; k <= kMax; k += 1) {
    const score = stripCorrelation(left, right, k, shift);
    if (score !== null && (best === null || score > best.score)) best = { k, score };
  }
  if (!best || best.score < (options.minScore ?? ESTIMATE_MIN_SCORE)) return null;
  return {
    overlap: clamp(best.k / width, 0, OVERLAP_MAX),
    score: best.score,
    atLimit: best.k === kMin || best.k === kMax,
  };
}
