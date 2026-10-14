/**
 * Planar homographies (3 x 3, row-major, `h[8]` normalised to 1): composition, inversion,
 * mapping points, conversions between pixel and unit frame coordinates, and the least-squares
 * fit from point correspondences (normalised DLT). Pure math, no DOM.
 *
 * Conventions of the panorama calibration (ADR 0037):
 * - a calibration's homography maps a point of the **right** frame to where the same scene
 *   point appears in the **left** frame's coordinates;
 * - coordinates are continuous canvas coordinates: pixel `i` covers `[i, i + 1)` and its centre
 *   is `i + 0.5`, so drawing a frame with `drawImage(frame, 0, 0, w, h)` and transforming the
 *   context by the homography puts every pixel where the homography says;
 * - stored calibrations use **unit** coordinates (`x / width`, `y / height`), so they do not
 *   depend on the resolution the cameras happen to deliver.
 */

export type Homography = readonly [
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
];
export type Point = readonly [number, number];

/** One correspondence: the same scene point in the left and in the right frame. */
export interface Correspondence {
  left: Point;
  right: Point;
}

export const IDENTITY: Homography = [1, 0, 0, 0, 1, 0, 0, 0, 1];

/** A homography from nine numbers (scaled so `h[8]` is 1); null for anything unusable. */
export function homographyOf(values: unknown): Homography | null {
  if (!Array.isArray(values) || values.length !== 9) return null;
  if (!values.every((v) => typeof v === 'number' && Number.isFinite(v))) return null;
  return normalize(values as unknown as Homography);
}

/** Scale so the last entry is 1 (null when it is about 0: a point at infinity). */
export function normalize(h: Homography): Homography | null {
  const s = h[8];
  if (!Number.isFinite(s) || Math.abs(s) < 1e-12) return null;
  const out = h.map((v) => v / s) as unknown as Homography;
  return out.every(Number.isFinite) ? out : null;
}

/** `a * b`: first apply `b`, then `a`. */
export function multiply(a: Homography, b: Homography): Homography {
  const out: number[] = [];
  for (let r = 0; r < 3; r += 1) {
    for (let c = 0; c < 3; c += 1) {
      out.push(a[r * 3]! * b[c]! + a[r * 3 + 1]! * b[3 + c]! + a[r * 3 + 2]! * b[6 + c]!);
    }
  }
  return out as unknown as Homography;
}

/** The inverse (normalised); null when singular. */
export function invert(h: Homography): Homography | null {
  const [a, b, c, d, e, f, g, i, j] = h;
  const A = e * j - f * i;
  const B = -(d * j - f * g);
  const C = d * i - e * g;
  const det = a * A + b * B + c * C;
  if (!Number.isFinite(det) || Math.abs(det) < 1e-12) return null;
  const inv: Homography = [
    A / det,
    -(b * j - c * i) / det,
    (b * f - c * e) / det,
    B / det,
    (a * j - c * g) / det,
    -(a * f - c * d) / det,
    C / det,
    -(a * i - b * g) / det,
    (a * e - b * d) / det,
  ];
  return normalize(inv);
}

/** Map a point; null when it lands at (or behind) infinity. */
export function apply(h: Homography, [x, y]: Point): Point | null {
  const w = h[6] * x + h[7] * y + h[8];
  if (!(w > 1e-9)) return null;
  return [(h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w];
}

export function translation(tx: number, ty: number): Homography {
  return [1, 0, tx, 0, 1, ty, 0, 0, 1];
}

export function scaling(sx: number, sy: number): Homography {
  return [sx, 0, 0, 0, sy, 0, 0, 0, 1];
}

/** Flip horizontally within a frame `width` wide (`x -> width - x`). */
export function mirrorX(width: number): Homography {
  return [-1, 0, width, 0, 1, 0, 0, 0, 1];
}

/** A pixel homography of `width` x `height` frames in unit coordinates. */
export function toUnit(h: Homography, width: number, height: number): Homography {
  return normalize(multiply(scaling(1 / width, 1 / height), multiply(h, scaling(width, height))))!;
}

/** A unit homography in pixel coordinates of `width` x `height` frames. */
export function fromUnit(h: Homography, width: number, height: number): Homography {
  return normalize(multiply(scaling(width, height), multiply(h, scaling(1 / width, 1 / height))))!;
}

/** The four corners of a `width` x `height` frame, clockwise from the top left. */
export function corners(width: number, height: number): [Point, Point, Point, Point] {
  return [
    [0, 0],
    [width, 0],
    [width, height],
    [0, height],
  ];
}

/**
 * True when the perspective terms are negligible over a `width` x `height` frame: the
 * homogeneous `w` stays within `tolerance` of 1 at every corner, so an affine transform
 * (`setTransform`) draws it within a fraction of a pixel.
 */
export function isAffine(h: Homography, width: number, height: number, tolerance = 1e-3): boolean {
  const n = normalize(h);
  if (!n) return false;
  return corners(width, height).every(([x, y]) => Math.abs(n[6] * x + n[7] * y) <= tolerance);
}

/** Squared distance between where `h` maps the right point and the left point. */
export function transferError(h: Homography, pair: Correspondence): number {
  const mapped = apply(h, pair.right);
  if (!mapped) return Number.POSITIVE_INFINITY;
  const dx = mapped[0] - pair.left[0];
  const dy = mapped[1] - pair.left[1];
  return dx * dx + dy * dy;
}

/** Hartley normalisation: centroid to the origin, mean distance sqrt(2). */
function normalizer(points: readonly Point[]): Homography | null {
  const n = points.length;
  let cx = 0;
  let cy = 0;
  for (const [x, y] of points) {
    cx += x;
    cy += y;
  }
  cx /= n;
  cy /= n;
  let mean = 0;
  for (const [x, y] of points) mean += Math.hypot(x - cx, y - cy);
  mean /= n;
  if (!(mean > 1e-9)) return null;
  const s = Math.SQRT2 / mean;
  return [s, 0, -s * cx, 0, s, -s * cy, 0, 0, 1];
}

/** Solve `A x = b` (n x n, row-major) by Gaussian elimination with partial pivoting. */
export function solveLinear(a: number[], b: number[], n: number): number[] | null {
  const m = a.slice();
  const v = b.slice();
  for (let col = 0; col < n; col += 1) {
    let pivot = col;
    for (let r = col + 1; r < n; r += 1) {
      if (Math.abs(m[r * n + col]!) > Math.abs(m[pivot * n + col]!)) pivot = r;
    }
    const p = m[pivot * n + col]!;
    if (Math.abs(p) < 1e-12) return null;
    if (pivot !== col) {
      for (let c = 0; c < n; c += 1) {
        const t = m[col * n + c]!;
        m[col * n + c] = m[pivot * n + c]!;
        m[pivot * n + c] = t;
      }
      const t = v[col]!;
      v[col] = v[pivot]!;
      v[pivot] = t;
    }
    for (let r = col + 1; r < n; r += 1) {
      const factor = m[r * n + col]! / m[col * n + col]!;
      if (factor === 0) continue;
      for (let c = col; c < n; c += 1) m[r * n + c] = m[r * n + c]! - factor * m[col * n + c]!;
      v[r] = v[r]! - factor * v[col]!;
    }
  }
  const x = new Array<number>(n).fill(0);
  for (let r = n - 1; r >= 0; r -= 1) {
    let sum = v[r]!;
    for (let c = r + 1; c < n; c += 1) sum -= m[r * n + c]! * x[c]!;
    x[r] = sum / m[r * n + r]!;
  }
  return x.every(Number.isFinite) ? x : null;
}

/**
 * The homography (right to left) that fits the correspondences best in the least-squares
 * sense: normalised DLT with `h[8] = 1` (four pairs give the exact solution). Null for fewer
 * than four pairs or a degenerate configuration (collinear points).
 */
export function fitHomography(pairs: readonly Correspondence[]): Homography | null {
  if (pairs.length < 4) return null;
  const tl = normalizer(pairs.map((p) => p.left));
  const tr = normalizer(pairs.map((p) => p.right));
  if (!tl || !tr) return null;
  const ata = new Array<number>(64).fill(0);
  const atb = new Array<number>(8).fill(0);
  const add = (row: number[], rhs: number) => {
    for (let i = 0; i < 8; i += 1) {
      atb[i] = atb[i]! + row[i]! * rhs;
      for (let j = 0; j < 8; j += 1) ata[i * 8 + j] = ata[i * 8 + j]! + row[i]! * row[j]!;
    }
  };
  for (const pair of pairs) {
    const [x, y] = apply(tr, pair.right)!;
    const [u, v] = apply(tl, pair.left)!;
    add([x, y, 1, 0, 0, 0, -u * x, -u * y], u);
    add([0, 0, 0, x, y, 1, -v * x, -v * y], v);
  }
  const h = solveLinear(ata, atb, 8);
  if (!h) return null;
  const normalised = [...h, 1] as unknown as Homography;
  const tlInv = invert(tl);
  if (!tlInv) return null;
  return normalize(multiply(tlInv, multiply(normalised, tr)));
}

/**
 * The affine map (right to left, no perspective) that fits the correspondences best in the
 * least-squares sense; three pairs give the exact solution. Null for fewer than three pairs or
 * collinear points.
 */
export function fitAffine(pairs: readonly Correspondence[]): Homography | null {
  if (pairs.length < 3) return null;
  const ata = new Array<number>(9).fill(0);
  const atu = new Array<number>(3).fill(0);
  const atv = new Array<number>(3).fill(0);
  // Centre the right points for conditioning.
  let cx = 0;
  let cy = 0;
  for (const { right } of pairs) {
    cx += right[0];
    cy += right[1];
  }
  cx /= pairs.length;
  cy /= pairs.length;
  for (const { left, right } of pairs) {
    const row = [right[0] - cx, right[1] - cy, 1];
    for (let i = 0; i < 3; i += 1) {
      atu[i] = atu[i]! + row[i]! * left[0];
      atv[i] = atv[i]! + row[i]! * left[1];
      for (let j = 0; j < 3; j += 1) ata[i * 3 + j] = ata[i * 3 + j]! + row[i]! * row[j]!;
    }
  }
  const u = solveLinear(ata, atu, 3);
  const v = solveLinear(ata, atv, 3);
  if (!u || !v) return null;
  const centred: Homography = [u[0]!, u[1]!, u[2]!, v[0]!, v[1]!, v[2]!, 0, 0, 1];
  return normalize(multiply(centred, translation(-cx, -cy)));
}
