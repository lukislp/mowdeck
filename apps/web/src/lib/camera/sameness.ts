/**
 * Two views must never show the same stream without saying so. The two front cameras sit apart,
 * so their pictures always differ a little; when two tiles show (nearly) identical pixels for a
 * few readings in a row, the panel says "both views show the same picture" (a mower that
 * publishes one camera under both uids, or a player that attached one track twice).
 *
 * Pure comparison of small thumbnails; the DOM part only draws a video or canvas into a 32 x 18
 * canvas (tainted or empty sources give null and are never reported).
 */

export const THUMB_WIDTH = 32;
export const THUMB_HEIGHT = 18;
/** Mean absolute difference per channel (0 to 255) at or below which two pictures count as same. */
export const SAME_PICTURE_THRESHOLD = 2;
/** Readings in a row before the notice shows (one reading every few seconds). */
export const SAME_PICTURE_READINGS = 3;
/** How often the panel compares the two tiles. */
export const SAME_PICTURE_INTERVAL_MS = 2_000;

/** Mean absolute difference of two RGBA buffers of equal size (alpha ignored); null otherwise. */
export function meanAbsDifference(a: ArrayLike<number>, b: ArrayLike<number>): number | null {
  if (a.length === 0 || a.length !== b.length || a.length % 4 !== 0) return null;
  let sum = 0;
  let count = 0;
  for (let i = 0; i < a.length; i += 4) {
    for (let c = 0; c < 3; c += 1) {
      sum += Math.abs((a[i + c] ?? 0) - (b[i + c] ?? 0));
      count += 1;
    }
  }
  return sum / count;
}

/** Counts consecutive "same picture" readings; an unknown reading neither counts nor resets. */
export class SamePictureWatch {
  private streak = 0;

  constructor(
    private readonly threshold = SAME_PICTURE_THRESHOLD,
    private readonly readings = SAME_PICTURE_READINGS,
  ) {}

  /** Feed one difference; true while the pictures have been the same long enough. */
  observe(difference: number | null): boolean {
    if (difference === null) return this.same;
    this.streak = difference <= this.threshold ? this.streak + 1 : 0;
    return this.same;
  }

  get same(): boolean {
    return this.streak >= this.readings;
  }

  reset(): void {
    this.streak = 0;
  }
}

/** The size of a playing video or a canvas (0 x 0 before the first frame). */
function sourceSize(source: HTMLVideoElement | HTMLCanvasElement): [number, number] {
  return 'videoWidth' in source
    ? [source.videoWidth, source.videoHeight]
    : [source.width, source.height];
}

/** A tiny RGBA thumbnail of a playing video or a canvas; null without a frame or when tainted. */
export function thumbnail(
  source: HTMLVideoElement | HTMLCanvasElement,
  doc: Document = document,
): Uint8ClampedArray | null {
  const [width, height] = sourceSize(source);
  if (!width || !height) return null;
  try {
    const canvas = doc.createElement('canvas');
    canvas.width = THUMB_WIDTH;
    canvas.height = THUMB_HEIGHT;
    const context = canvas.getContext('2d', { willReadFrequently: true });
    if (!context) return null;
    context.drawImage(source, 0, 0, THUMB_WIDTH, THUMB_HEIGHT);
    return context.getImageData(0, 0, THUMB_WIDTH, THUMB_HEIGHT).data;
  } catch {
    return null;
  }
}
