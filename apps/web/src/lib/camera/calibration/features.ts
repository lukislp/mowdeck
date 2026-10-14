/**
 * Local image features for the `features` calibration method (ADR 0037), in plain TypeScript:
 * an ORB-style detector and descriptor plus brute-force Hamming matching.
 *
 * - Keypoints: FAST-9 corners (16-pixel Bresenham circle, nine contiguous brighter or darker
 *   pixels), ranked by the Harris response, 3 x 3 non-maximum suppression, spread over an
 *   8 x 6 grid so one busy corner of the picture cannot take all of them. The threshold adapts
 *   (20, 10, 5) until enough corners are found.
 * - Orientation: the intensity centroid of a radius-15 disc (as in ORB), so a small roll
 *   between the cameras does not break the descriptors.
 * - Descriptor: 256 intensity comparisons of a 5 x 5 box-smoothed image at point pairs drawn
 *   once from a Gaussian around the keypoint (fixed seed), rotated by the keypoint's angle:
 *   rotated BRIEF, 32 bytes per keypoint.
 * - Matching: brute-force Hamming distance, Lowe's ratio test and a mutual (cross) check.
 *
 * Single scale on purpose: both front cameras are the same model looking at the same scene, so
 * their scale differs by only a few percent, which BRIEF tolerates. Pure: works on a
 * {@link GrayImage}, no DOM. Coordinates of the returned correspondences are continuous canvas
 * coordinates (pixel centre `i + 0.5`), see `homography.ts`.
 */
import type { GrayImage } from '../panorama';
import type { Correspondence } from './homography';
import { seededRandom } from './ransac';

export interface Keypoint {
  x: number;
  y: number;
  /** Harris response (larger: more corner-like). */
  score: number;
  /** Orientation in radians (intensity centroid). */
  angle: number;
}

export interface Features {
  keypoints: Keypoint[];
  /** {@link WORDS} 32-bit words per keypoint, in keypoint order. */
  descriptors: Uint32Array;
}

export interface Match {
  left: number;
  right: number;
  distance: number;
}

export interface DetectOptions {
  /** Most keypoints to keep. */
  maxKeypoints?: number;
  /** FAST thresholds tried in turn until {@link MIN_CORNERS} corners are found. */
  thresholds?: readonly number[];
}

export interface MatchOptions {
  /** Lowe's ratio: best distance must be below `ratio` times the second best. */
  ratio?: number;
  /** Largest Hamming distance (of 256 bits) accepted at all. */
  maxDistance?: number;
  /** Keep only mutual best matches. */
  crossCheck?: boolean;
}

export const WORDS = 8;
export const BITS = WORDS * 32;
/** Keypoints closer to the border than this cannot be described (rotated patch + smoothing). */
export const BORDER = 22;
export const MAX_KEYPOINTS = 600;
const MIN_CORNERS = 150;
const PATCH_RADIUS = 15;
const PATTERN_LIMIT = 13;
const GRID_COLUMNS = 8;
const GRID_ROWS = 6;

const CIRCLE: readonly (readonly [number, number])[] = [
  [0, -3],
  [1, -3],
  [2, -2],
  [3, -1],
  [3, 0],
  [3, 1],
  [2, 2],
  [1, 3],
  [0, 3],
  [-1, 3],
  [-2, 2],
  [-3, 1],
  [-3, 0],
  [-3, -1],
  [-2, -2],
  [-1, -3],
];

/** The BRIEF point pairs `[x1, y1, x2, y2]`, Gaussian around the keypoint, fixed forever. */
export const PATTERN: Int8Array = (() => {
  const random = seededRandom(0x0b5e7);
  const sigma = (2 * PATCH_RADIUS + 1) / 5;
  const gauss = () => {
    const u = Math.max(1e-12, random());
    const v = random();
    const value = Math.round(sigma * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v));
    return Math.max(-PATTERN_LIMIT, Math.min(PATTERN_LIMIT, value));
  };
  const out = new Int8Array(BITS * 4);
  for (let i = 0; i < BITS; i += 1) {
    let x1 = 0;
    let y1 = 0;
    let x2 = 0;
    let y2 = 0;
    while (x1 === x2 && y1 === y2) {
      x1 = gauss();
      y1 = gauss();
      x2 = gauss();
      y2 = gauss();
    }
    out.set([x1, y1, x2, y2], i * 4);
  }
  return out;
})();

/** Half widths of the orientation disc per row (`|dx| <= UMAX[|dy|]`). */
const UMAX: readonly number[] = Array.from({ length: PATCH_RADIUS + 1 }, (_, dy) =>
  Math.floor(Math.sqrt(PATCH_RADIUS * PATCH_RADIUS - dy * dy)),
);

/** The circle's pixel offsets for a row stride. */
function circleOffsets(width: number): Int32Array {
  return Int32Array.from(CIRCLE, ([dx, dy]) => dy * width + dx);
}

/** A run of at least nine set bits in a 16-bit circular mask. */
function hasArc(mask: number): boolean {
  let m = mask | (mask << 16);
  for (let i = 1; i < 9; i += 1) m &= (mask | (mask << 16)) >>> i;
  return m !== 0;
}

/** FAST-9 corner test at index `p` with threshold `t`. */
function isCorner(data: ArrayLike<number>, p: number, offsets: Int32Array, t: number): boolean {
  const c = data[p]!;
  const hi = c + t;
  const lo = c - t;
  // Quick rejection: of the four compass points, at least two must agree.
  let bright = 0;
  let dark = 0;
  for (const k of [0, 4, 8, 12]) {
    const v = data[p + offsets[k]!]!;
    if (v > hi) bright += 1;
    else if (v < lo) dark += 1;
  }
  if (bright < 2 && dark < 2) return false;
  let brighter = 0;
  let darker = 0;
  for (let k = 0; k < 16; k += 1) {
    const v = data[p + offsets[k]!]!;
    if (v > hi) brighter |= 1 << k;
    else if (v < lo) darker |= 1 << k;
  }
  return hasArc(brighter) || hasArc(darker);
}

/** Harris response from Sobel gradients in a 7 x 7 window around (x, y). */
export function harris(image: GrayImage, x: number, y: number): number {
  const { width, data } = image;
  let xx = 0;
  let yy = 0;
  let xy = 0;
  for (let dy = -3; dy <= 3; dy += 1) {
    for (let dx = -3; dx <= 3; dx += 1) {
      const p = (y + dy) * width + x + dx;
      const gx =
        data[p - width + 1]! +
        2 * data[p + 1]! +
        data[p + width + 1]! -
        data[p - width - 1]! -
        2 * data[p - 1]! -
        data[p + width - 1]!;
      const gy =
        data[p + width - 1]! +
        2 * data[p + width]! +
        data[p + width + 1]! -
        data[p - width - 1]! -
        2 * data[p - width]! -
        data[p - width + 1]!;
      xx += gx * gx;
      yy += gy * gy;
      xy += gx * gy;
    }
  }
  return xx * yy - xy * xy - 0.04 * (xx + yy) * (xx + yy);
}

/** Orientation of the patch around (x, y): the angle of its intensity centroid. */
export function orientation(image: GrayImage, x: number, y: number): number {
  const { width, data } = image;
  let m10 = 0;
  let m01 = 0;
  for (let dy = -PATCH_RADIUS; dy <= PATCH_RADIUS; dy += 1) {
    const span = UMAX[Math.abs(dy)]!;
    const row = (y + dy) * width + x;
    for (let dx = -span; dx <= span; dx += 1) {
      const v = data[row + dx]!;
      m10 += dx * v;
      m01 += dy * v;
    }
  }
  return Math.atan2(m01, m10);
}

/** The image smoothed with a 5 x 5 box (through an integral image). */
export function boxBlur(image: GrayImage): Float32Array {
  const { width, height, data } = image;
  const stride = width + 1;
  const integral = new Float64Array(stride * (height + 1));
  for (let y = 0; y < height; y += 1) {
    let row = 0;
    for (let x = 0; x < width; x += 1) {
      row += data[y * width + x]!;
      integral[(y + 1) * stride + x + 1] = integral[y * stride + x + 1]! + row;
    }
  }
  const out = new Float32Array(width * height);
  for (let y = 0; y < height; y += 1) {
    const y0 = Math.max(0, y - 2);
    const y1 = Math.min(height, y + 3);
    for (let x = 0; x < width; x += 1) {
      const x0 = Math.max(0, x - 2);
      const x1 = Math.min(width, x + 3);
      const sum =
        integral[y1 * stride + x1]! -
        integral[y0 * stride + x1]! -
        integral[y1 * stride + x0]! +
        integral[y0 * stride + x0]!;
      out[y * width + x] = sum / ((x1 - x0) * (y1 - y0));
    }
  }
  return out;
}

/** FAST-9 corners with their Harris score after 3 x 3 non-maximum suppression. */
function corners(image: GrayImage, threshold: number): Keypoint[] {
  const { width, height, data } = image;
  const offsets = circleOffsets(width);
  const scores = new Float32Array(width * height);
  const found: number[] = [];
  for (let y = BORDER; y < height - BORDER; y += 1) {
    for (let x = BORDER; x < width - BORDER; x += 1) {
      const p = y * width + x;
      if (!isCorner(data, p, offsets, threshold)) continue;
      const score = harris(image, x, y);
      if (score <= 0) continue;
      scores[p] = score;
      found.push(p);
    }
  }
  const kept: Keypoint[] = [];
  for (const p of found) {
    const s = scores[p]!;
    let max = true;
    for (let dy = -1; dy <= 1 && max; dy += 1) {
      for (let dx = -1; dx <= 1; dx += 1) {
        if ((dx || dy) && scores[p + dy * width + dx]! > s) {
          max = false;
          break;
        }
      }
    }
    if (max) kept.push({ x: p % width, y: Math.floor(p / width), score: s, angle: 0 });
  }
  return kept;
}

/** The strongest keypoints, at most `max`, spread over a coarse grid. */
function spread(points: Keypoint[], width: number, height: number, max: number): Keypoint[] {
  const sorted = [...points].sort((a, b) => b.score - a.score);
  const cells = GRID_COLUMNS * GRID_ROWS;
  const cap = Math.max(1, Math.ceil((2 * max) / cells));
  const used = new Array<number>(cells).fill(0);
  const out: Keypoint[] = [];
  for (const point of sorted) {
    if (out.length >= max) break;
    const cx = Math.min(GRID_COLUMNS - 1, Math.floor((point.x / width) * GRID_COLUMNS));
    const cy = Math.min(GRID_ROWS - 1, Math.floor((point.y / height) * GRID_ROWS));
    const cell = cy * GRID_COLUMNS + cx;
    if (used[cell]! >= cap) continue;
    used[cell] = used[cell]! + 1;
    out.push(point);
  }
  return out;
}

/** Detect, orient and describe keypoints of a greyscale image. */
export function detectFeatures(image: GrayImage, options: DetectOptions = {}): Features {
  const max = options.maxKeypoints ?? MAX_KEYPOINTS;
  const { width, height } = image;
  if (width <= 2 * BORDER || height <= 2 * BORDER) {
    return { keypoints: [], descriptors: new Uint32Array(0) };
  }
  let found: Keypoint[] = [];
  for (const threshold of options.thresholds ?? [20, 10, 5]) {
    found = corners(image, threshold);
    if (found.length >= MIN_CORNERS) break;
  }
  const keypoints = spread(found, width, height, max).map((k) => ({
    ...k,
    angle: orientation(image, k.x, k.y),
  }));
  const smooth = boxBlur(image);
  const descriptors = new Uint32Array(keypoints.length * WORDS);
  keypoints.forEach((k, index) => {
    const cos = Math.cos(k.angle);
    const sin = Math.sin(k.angle);
    const at = (px: number, py: number) => {
      const rx = Math.round(cos * px - sin * py);
      const ry = Math.round(sin * px + cos * py);
      return smooth[(k.y + ry) * width + k.x + rx]!;
    };
    for (let bit = 0; bit < BITS; bit += 1) {
      const o = bit * 4;
      if (at(PATTERN[o]!, PATTERN[o + 1]!) < at(PATTERN[o + 2]!, PATTERN[o + 3]!)) {
        const w = index * WORDS + (bit >>> 5);
        descriptors[w] = (descriptors[w]! | (1 << (bit & 31))) >>> 0;
      }
    }
  });
  return { keypoints, descriptors };
}

function popcount(v: number): number {
  let x = v - ((v >>> 1) & 0x55555555);
  x = (x & 0x33333333) + ((x >>> 2) & 0x33333333);
  return (((x + (x >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24;
}

/** Hamming distance of descriptor `i` of `a` and descriptor `j` of `b`. */
export function hamming(a: Uint32Array, i: number, b: Uint32Array, j: number): number {
  let d = 0;
  for (let w = 0; w < WORDS; w += 1) d += popcount((a[i * WORDS + w]! ^ b[j * WORDS + w]!) >>> 0);
  return d;
}

/** For each descriptor of `a`: its best and second best distance in `b`, and the best index. */
function nearest(a: Features, b: Features): { index: number; best: number; second: number }[] {
  const out: { index: number; best: number; second: number }[] = [];
  for (let i = 0; i < a.keypoints.length; i += 1) {
    let index = -1;
    let best = Number.POSITIVE_INFINITY;
    let second = Number.POSITIVE_INFINITY;
    for (let j = 0; j < b.keypoints.length; j += 1) {
      const d = hamming(a.descriptors, i, b.descriptors, j);
      if (d < best) {
        second = best;
        best = d;
        index = j;
      } else if (d < second) {
        second = d;
      }
    }
    out.push({ index, best, second });
  }
  return out;
}

/** Brute-force matches from `left` to `right` that pass the ratio test (and the cross check). */
export function matchFeatures(
  left: Features,
  right: Features,
  options: MatchOptions = {},
): Match[] {
  const ratio = options.ratio ?? 0.8;
  const maxDistance = options.maxDistance ?? 64;
  const forward = nearest(left, right);
  const backward = options.crossCheck === false ? null : nearest(right, left);
  const matches: Match[] = [];
  forward.forEach(({ index, best, second }, i) => {
    if (index < 0 || best > maxDistance) return;
    if (Number.isFinite(second) && best >= ratio * second) return;
    if (backward && backward[index]?.index !== i) return;
    matches.push({ left: i, right: index, distance: best });
  });
  return matches;
}

export interface FeatureMatches {
  pairs: Correspondence[];
  leftKeypoints: number;
  rightKeypoints: number;
}

/** Detect in both images and match: the correspondences in canvas coordinates. */
export function featureCorrespondences(
  left: GrayImage,
  right: GrayImage,
  options: DetectOptions & MatchOptions = {},
): FeatureMatches {
  const a = detectFeatures(left, options);
  const b = detectFeatures(right, options);
  const pairs = matchFeatures(a, b, options).map(({ left: i, right: j }) => {
    const p = a.keypoints[i]!;
    const q = b.keypoints[j]!;
    return { left: [p.x + 0.5, p.y + 0.5], right: [q.x + 0.5, q.y + 0.5] } as Correspondence;
  });
  return { pairs, leftKeypoints: a.keypoints.length, rightKeypoints: b.keypoints.length };
}
