/**
 * The `correlation` method: the existing overlap estimate (normalised cross-correlation of the
 * frames' facing edges, `panorama.ts`) as a calibration. Translation only, so its result is a
 * slider setting; it is the fallback of `features` when too few features match.
 */
import { estimateOverlap, flipGray, type GrayImage } from '../panorama';
import { translationResult } from './manual';
import { CalibrationError, type CalibrationMethod } from './types';

/** Width of the copies the correlation compares (it searches every overlap: keep it small). */
export const CORRELATION_WIDTH = 160;

/** A box-averaged copy at most `width` pixels wide (the image itself when already smaller). */
export function downscaleGray(image: GrayImage, width = CORRELATION_WIDTH): GrayImage {
  if (image.width <= width) return image;
  const factor = image.width / width;
  const height = Math.max(1, Math.round(image.height / factor));
  const data = new Float32Array(width * height);
  for (let y = 0; y < height; y += 1) {
    const y0 = Math.floor(y * factor);
    const y1 = Math.max(y0 + 1, Math.min(image.height, Math.floor((y + 1) * factor)));
    for (let x = 0; x < width; x += 1) {
      const x0 = Math.floor(x * factor);
      const x1 = Math.max(x0 + 1, Math.min(image.width, Math.floor((x + 1) * factor)));
      let sum = 0;
      for (let yy = y0; yy < y1; yy += 1) {
        for (let xx = x0; xx < x1; xx += 1) sum += image.data[yy * image.width + xx] ?? 0;
      }
      data[y * width + x] = sum / ((x1 - x0) * (y1 - y0));
    }
  }
  return { width, height, data };
}

export function correlationMethod(label: () => string): CalibrationMethod {
  return {
    id: 'correlation',
    label,
    estimate: async (left, right, options) => {
      const a = downscaleGray(left.gray);
      const scaled = downscaleGray(right.gray);
      const b = options.mirror ? flipGray(scaled) : scaled;
      const estimate = estimateOverlap(a, b, { offset: options.manual.offset });
      if (!estimate) throw new CalibrationError('no_match');
      return translationResult(
        'correlation',
        { ...options.manual, overlap: estimate.overlap, mirror: options.mirror },
        Math.max(0, Math.min(1, estimate.score)),
      );
    },
  };
}
