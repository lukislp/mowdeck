/**
 * Capturing the calibration pair from the panel's two live pictures (the DOM side of the
 * calibration): both current frames are drawn into still canvases of the same size (at most
 * {@link CAPTURE_WIDTH} wide), with their luminance for the local methods and a lazy JPEG for
 * the AI method. The still canvases also feed the before/after preview.
 */
import { toGray } from '../panorama';
import { sourceSize, type PanoramaSource } from '../panorama-renderer';
import type { CalibrationFrame } from './types';

/** Width of the captured frames: enough for features, cheap enough to match in a moment. */
export const CAPTURE_WIDTH = 640;
export const CAPTURE_JPEG_QUALITY = 0.85;

export interface CapturedPair {
  left: CalibrationFrame;
  right: CalibrationFrame;
  /** The still frames (preview sources). */
  leftCanvas: HTMLCanvasElement;
  rightCanvas: HTMLCanvasElement;
  at: Date;
}

/** The base64 part of a JPEG data URL. */
function jpegOf(canvas: HTMLCanvasElement): Promise<string> {
  const url = canvas.toDataURL('image/jpeg', CAPTURE_JPEG_QUALITY);
  const comma = url.indexOf(',');
  return Promise.resolve(comma >= 0 ? url.slice(comma + 1) : '');
}

function still(
  source: PanoramaSource,
  width: number,
  height: number,
  at: number,
  doc: Document,
): { frame: CalibrationFrame; canvas: HTMLCanvasElement } | null {
  const canvas = doc.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext('2d', { willReadFrequently: true });
  if (!context) return null;
  context.drawImage(source, 0, 0, width, height);
  const gray = toGray(context.getImageData(0, 0, width, height).data, width, height);
  return {
    canvas,
    frame: { width, height, gray, capturedAt: at, jpeg: () => jpegOf(canvas) },
  };
}

/** Both current frames as a calibration pair; null unless both sources show a picture. */
export function capturePair(
  left: PanoramaSource | null,
  right: PanoramaSource | null,
  doc: Document = document,
  now: () => number = Date.now,
): CapturedPair | null {
  const [lw, lh] = sourceSize(left);
  const [rw, rh] = sourceSize(right);
  if (!left || !right || !lw || !lh || !rw || !rh) return null;
  const width = Math.min(CAPTURE_WIDTH, lw);
  const height = Math.max(1, Math.round((width * lh) / lw));
  const at = now();
  try {
    const a = still(left, width, height, at, doc);
    const b = still(right, width, height, at, doc);
    if (!a || !b) return null;
    return {
      left: a.frame,
      right: b.frame,
      leftCanvas: a.canvas,
      rightCanvas: b.canvas,
      at: new Date(at),
    };
  } catch {
    // A tainted canvas or a lost context: no pair.
    return null;
  }
}
