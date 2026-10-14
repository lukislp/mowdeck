/**
 * Geometry of the stitched panorama for a calibration homography: the canvas extent, where the
 * left frame lands, the right frame's transform into the panorama, the feathered overlap, and the
 * triangle mesh that draws a true perspective warp with affine canvas transforms. Pure, no DOM;
 * `panorama-renderer.ts` draws what this computes.
 *
 * Warp approach (ADR 0037): when the homography's perspective terms are negligible over the
 * frame (always for the `manual` and `correlation` methods), the right frame is drawn with one
 * `setTransform`, exactly. Otherwise the right frame is cut into a grid of triangles; each
 * triangle is drawn clipped with the affine transform that maps its three corners exactly
 * (piecewise affine). The grid is refined until the largest deviation from the true homography,
 * measured at the cells' centres and edge midpoints, is below half a pixel (at most 32 x 24
 * cells). At 640 x 480 and the mild perspective of two cameras on one mower this is typically
 * 4 x 3 to 8 x 6 cells: a few dozen clipped `drawImage` calls per frame, well within budget at
 * the cameras' 6 fps, with no WebGL context and no per-pixel loop in JavaScript.
 */
import {
  apply,
  corners,
  fromUnit,
  isAffine,
  multiply,
  normalize,
  solveLinear,
  translation,
  type Homography,
  type Point,
} from './homography';

/** Canvas transform `(a, b, c, d, e, f)`: `x' = a x + c y + e`, `y' = b x + d y + f`. */
export type Affine = readonly [number, number, number, number, number, number];
export type Triangle = readonly [Point, Point, Point];

export interface StitchGeometry {
  /** The panorama's size in frame pixels. */
  width: number;
  height: number;
  /** Where the left frame's top left corner lands. */
  origin: Point;
  /** Right frame pixels to panorama pixels. */
  right: Homography;
  /** The right frame's corners in the panorama (clockwise from its top left). */
  quad: readonly [Point, Point, Point, Point];
  /** The panorama columns over which the right frame fades in (see {@link seamBand}). */
  feather: { start: number; end: number } | null;
  /** The right frame can be drawn with one affine transform. */
  affine: boolean;
}

/** The panorama is never wider than this many frames (a wild homography cannot explode it). */
export const MAX_PANORAMA_FRAMES = 3;
/** Rows both frames cover below this fraction of the height: show the union instead. */
const MIN_COMMON_ROWS = 0.25;
export const MESH_MAX_ERROR = 0.5;
export const MESH_MAX_COLUMNS = 32;
/** Overlaps up to this fraction of the frame width are blended across their whole width. */
export const SEAM_FULL_FRACTION = 0.5;
/** Wider overlaps: the right frame fades in over this many columns at the end of the overlap. */
export const SEAM_COLUMNS = 64;

/**
 * The columns over which the right frame fades in, for an overlap from `start` (the right
 * frame's left edge) to `end` (the left frame's right edge) of frames `width` wide. An overlap
 * of up to half a frame (a slider layout) is blended across its whole width. Two cameras of a stereo pair see nearly the same scene, so their
 * overlap is most of the frame; one homography aligns one depth, and blending the whole overlap
 * would double every object at another depth (parallax). Wide overlaps therefore keep the left
 * frame untouched and let the right frame in only over a short ramp at the left frame's right
 * edge: the panorama is the left picture plus the strip only the right camera sees, which is
 * also what Vision should get as "what the mower sees". Null without an overlap.
 */
export function seamBand(
  start: number,
  end: number,
  width: number,
): { start: number; end: number } | null {
  const overlap = end - start;
  if (overlap < 1) return null;
  const band = overlap <= SEAM_FULL_FRACTION * width ? overlap : SEAM_COLUMNS;
  return { start: end - band, end };
}

/** Shoelace area (positive for clockwise corners in y-down canvas coordinates). */
export function signedArea(quad: readonly Point[]): number {
  let sum = 0;
  for (let i = 0; i < quad.length; i += 1) {
    const [x1, y1] = quad[i]!;
    const [x2, y2] = quad[(i + 1) % quad.length]!;
    sum += x1 * y2 - x2 * y1;
  }
  return sum / 2;
}

function convex(quad: readonly Point[]): boolean {
  let sign = 0;
  for (let i = 0; i < quad.length; i += 1) {
    const [ax, ay] = quad[i]!;
    const [bx, by] = quad[(i + 1) % quad.length]!;
    const [cx, cy] = quad[(i + 2) % quad.length]!;
    const cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx);
    if (Math.abs(cross) < 1e-12) return false;
    const s = Math.sign(cross);
    if (sign && s !== sign) return false;
    sign = s;
  }
  return true;
}

/** The right frame's corners under a unit homography (null when one is at infinity). */
export function unitQuad(h: Homography): [Point, Point, Point, Point] | null {
  const mapped = corners(1, 1).map((p) => apply(h, p));
  return mapped.every((p) => p !== null) ? (mapped as [Point, Point, Point, Point]) : null;
}

/**
 * Whether a unit homography can be a calibration of two side-by-side cameras: the right frame
 * maps to a convex quadrilateral of 0.4 to 2.5 times its area whose centre lies near the left
 * frame (not flung across the scene). Mirroring (negative area) is allowed.
 */
export function plausible(h: Homography): boolean {
  const quad = unitQuad(h);
  if (!quad || !convex(quad)) return false;
  const area = Math.abs(signedArea(quad));
  if (area < 0.4 || area > 2.5) return false;
  const cx = quad.reduce((s, p) => s + p[0], 0) / 4;
  const cy = quad.reduce((s, p) => s + p[1], 0) / 4;
  return cx >= -0.25 && cx <= 2.25 && cy >= -0.75 && cy <= 1.75;
}

/** True when the homography flips the right frame (a mirrored camera). */
export function mirrored(h: Homography): boolean {
  const quad = unitQuad(h);
  return !!quad && signedArea(quad) < 0;
}

const clamp = (value: number, min: number, max: number): number =>
  Math.min(max, Math.max(min, value));

/**
 * The stitched panorama for frames of `width` x `height` and a unit homography (right to left).
 * Horizontally it spans both frames; vertically it is cropped to the rows both frames cover
 * (like the slider layout), unless they share less than a quarter of the height. Null when the
 * homography sends a corner to infinity.
 */
export function stitchGeometry(
  width: number,
  height: number,
  unit: Homography,
): StitchGeometry | null {
  const h = fromUnit(unit, width, height);
  const mapped = corners(width, height).map((p) => apply(h, p));
  if (mapped.some((p) => p === null)) return null;
  const [q0, q1, q2, q3] = mapped as [Point, Point, Point, Point];
  const xs = [q0[0], q1[0], q2[0], q3[0]];
  const minX = clamp(Math.min(0, ...xs), -width, 0);
  const maxX = clamp(Math.max(width, ...xs), width, (MAX_PANORAMA_FRAMES - 1) * width);
  let top = Math.max(0, q0[1], q1[1]);
  let bottom = Math.min(height, q2[1], q3[1]);
  if (bottom - top < MIN_COMMON_ROWS * height) {
    top = clamp(Math.min(0, q0[1], q1[1]), -height / 2, 0);
    bottom = clamp(Math.max(height, q2[1], q3[1]), height, 1.5 * height);
  }
  const origin: Point = [-Math.round(minX) || 0, -Math.round(top) || 0];
  const right = normalize(multiply(translation(origin[0], origin[1]), h))!;
  const quad = [q0, q1, q2, q3].map(([x, y]) => [x + origin[0], y + origin[1]] as Point) as [
    Point,
    Point,
    Point,
    Point,
  ];
  // The right frame's left edge (per row, its leftmost point) to the left frame's right edge.
  const start = Math.max(
    origin[0],
    Math.min(quad[0][0], quad[1][0]),
    Math.min(quad[3][0], quad[2][0]),
  );
  const end = origin[0] + width;
  return {
    width: Math.max(1, Math.round(maxX - minX)),
    height: Math.max(1, Math.round(bottom - top)),
    origin,
    right,
    quad,
    feather: seamBand(start, end, width),
    affine: isAffine(h, width, height),
  };
}

/** The canvas transform of an affine homography (normalised; perspective terms ignored). */
export function affineOf(h: Homography): Affine {
  const n = normalize(h) ?? h;
  return [n[0], n[3], n[1], n[4], n[2], n[5]];
}

/** `transform` after scaling everything by `s` (the canvas' backing scale). */
export function scaled(transform: Affine, s: number): Affine {
  return transform.map((v) => v * s) as unknown as Affine;
}

/** The affine transform that maps the three corners of `src` onto those of `dst`. */
export function triangleAffine(src: Triangle, dst: Triangle): Affine | null {
  const a = src.flatMap(([x, y]) => [x, y, 1]);
  const xs = solveLinear(
    a,
    dst.map((p) => p[0]),
    3,
  );
  const ys = solveLinear(
    a,
    dst.map((p) => p[1]),
    3,
  );
  if (!xs || !ys) return null;
  return [xs[0]!, ys[0]!, xs[1]!, ys[1]!, xs[2]!, ys[2]!];
}

function applyAffine([a, b, c, d, e, f]: Affine, [x, y]: Point): Point {
  return [a * x + c * y + e, b * x + d * y + f];
}

export interface MeshTriangle {
  /** The triangle in the panorama (the clip region). */
  dst: Triangle;
  /** Frame pixels to panorama pixels for this triangle. */
  transform: Affine;
}

/** Cut the frame into `columns` x `rows` cells of two triangles, each with its exact affine. */
export function warpMesh(
  h: Homography,
  width: number,
  height: number,
  columns: number,
  rows: number,
): MeshTriangle[] | null {
  const grid: Point[][] = [];
  for (let r = 0; r <= rows; r += 1) {
    const line: Point[] = [];
    for (let c = 0; c <= columns; c += 1) line.push([(c / columns) * width, (r / rows) * height]);
    grid.push(line);
  }
  const out: MeshTriangle[] = [];
  for (let r = 0; r < rows; r += 1) {
    for (let c = 0; c < columns; c += 1) {
      const p00 = grid[r]![c]!;
      const p10 = grid[r]![c + 1]!;
      const p01 = grid[r + 1]![c]!;
      const p11 = grid[r + 1]![c + 1]!;
      for (const src of [
        [p00, p10, p11],
        [p00, p11, p01],
      ] as Triangle[]) {
        const dst = src.map((p) => apply(h, p));
        if (dst.some((p) => p === null)) return null;
        const transform = triangleAffine(src, dst as unknown as Triangle);
        if (!transform) return null;
        out.push({ dst: dst as unknown as Triangle, transform });
      }
    }
  }
  return out;
}

/** The largest distance between the mesh's piecewise affine map and the homography. */
export function meshError(
  h: Homography,
  width: number,
  height: number,
  columns: number,
  rows: number,
): number {
  const mesh = warpMesh(h, width, height, columns, rows);
  if (!mesh) return Number.POSITIVE_INFINITY;
  const cw = width / columns;
  const ch = height / rows;
  let worst = 0;
  for (let r = 0; r < rows; r += 1) {
    for (let c = 0; c < columns; c += 1) {
      const x = c * cw;
      const y = r * ch;
      const upper = mesh[(r * columns + c) * 2]!;
      const lower = mesh[(r * columns + c) * 2 + 1]!;
      // Points inside the upper triangle (p00, p10, p11) and the lower one (p00, p11, p01).
      const probes: [MeshTriangle, Point][] = [
        [upper, [x + cw * 0.75, y + ch * 0.25]],
        [upper, [x + cw * 0.5, y + ch * 0.05]],
        [lower, [x + cw * 0.25, y + ch * 0.75]],
        [lower, [x + cw * 0.05, y + ch * 0.5]],
      ];
      for (const [triangle, point] of probes) {
        const exact = apply(h, point);
        if (!exact) return Number.POSITIVE_INFINITY;
        const approx = applyAffine(triangle.transform, point);
        worst = Math.max(worst, Math.hypot(exact[0] - approx[0], exact[1] - approx[1]));
      }
    }
  }
  return worst;
}

/** The coarsest grid (4:3 steps from 2 x 2) whose error stays below `maxError` pixels. */
export function meshGrid(
  h: Homography,
  width: number,
  height: number,
  maxError = MESH_MAX_ERROR,
): { columns: number; rows: number } {
  let columns = 2;
  let rows = 2;
  while (columns < MESH_MAX_COLUMNS && meshError(h, width, height, columns, rows) > maxError) {
    columns = Math.min(MESH_MAX_COLUMNS, columns * 2);
    rows = Math.max(2, Math.round((columns * 3) / 4));
  }
  return { columns, rows };
}
