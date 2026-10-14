/**
 * Draws the panorama into a canvas for a calibration homography (`calibration/geometry.ts`):
 * the left frame, then the right frame warped onto it through a panorama-sized scratch canvas
 * whose overlap zone is faded with a linear alpha ramp (`destination-in` with a gradient), so
 * the overlap shows `left * (1 - a) + right * a`. The warp is one `setTransform` when the
 * homography is affine over the frame (every slider calibration: the same picture as the slider
 * layout) and a clipped triangle mesh otherwise (a true perspective warp, see the geometry
 * module). The canvas' backing store follows its CSS width times the device pixel ratio, never
 * more than the frames' own resolution. {@link paintPanorama} draws one picture (the calibration
 * preview uses it on still frames); {@link PanoramaRenderer} keeps a live canvas drawn.
 *
 * Timing ({@link PanoramaClock}): a `<video>` source with `requestVideoFrameCallback` asks for a
 * draw whenever it presents a frame (the cameras send about 6 fps); otherwise, and as a fallback
 * when no frame callback came for {@link VIDEO_IDLE_MS}, a `requestAnimationFrame` loop draws at
 * most {@link PANORAMA_MAX_FPS} times a second. Nothing is drawn while the canvas is not visible
 * (`setVisible`, from an IntersectionObserver) or the clock is stopped.
 *
 * The source elements stay in the page (hidden), so AI assist still grabs the two raw frames.
 */
import {
  affineOf,
  meshGrid,
  scaled,
  stitchGeometry,
  warpMesh,
  type MeshTriangle,
  type StitchGeometry,
} from './calibration/geometry';
import { CORRELATION_WIDTH } from './calibration/correlation';
import type { Homography } from './calibration/homography';
import { manualHomography } from './calibration/manual';
import { DEFAULT_PANORAMA, toGray, type GrayImage } from './panorama';

export type PanoramaSource = HTMLVideoElement | HTMLCanvasElement;

export const PANORAMA_MAX_FPS = 10;
export const FRAME_INTERVAL_MS = 1000 / PANORAMA_MAX_FPS;
/** Without a video frame callback for this long, the capped loop draws instead. */
export const VIDEO_IDLE_MS = 500;

export interface ClockDeps {
  raf(callback: () => void): unknown;
  caf(handle: unknown): void;
  now(): number;
}

function defaultClockDeps(): ClockDeps {
  return {
    raf: (callback) => requestAnimationFrame(() => callback()),
    caf: (handle) => cancelAnimationFrame(handle as number),
    now: () => performance.now(),
  };
}

/**
 * One animation-frame loop that calls `draw` when something asked for it (a video frame, new
 * settings) or, while no video frames arrive, at most every {@link FRAME_INTERVAL_MS}.
 */
export class PanoramaClock {
  private running = false;
  private visible = true;
  private handle: unknown = null;
  private dirty = true;
  private last = Number.NEGATIVE_INFINITY;
  private lastVideo = Number.NEGATIVE_INFINITY;
  private readonly deps: ClockDeps;

  constructor(
    private readonly draw: () => void,
    deps?: ClockDeps,
  ) {
    this.deps = deps ?? defaultClockDeps();
  }

  get active(): boolean {
    return this.running && this.visible;
  }

  start(): void {
    this.running = true;
    this.dirty = true;
    this.schedule();
  }

  stop(): void {
    this.running = false;
    this.cancel();
  }

  /** The canvas came into view or left it (the panel scrolled away, the page hidden). */
  setVisible(visible: boolean): void {
    this.visible = visible;
    if (this.active) {
      this.dirty = true;
      this.schedule();
    } else {
      this.cancel();
    }
  }

  /** A video presented a new frame: draw at the next animation frame. */
  frame(): void {
    this.lastVideo = this.deps.now();
    this.dirty = true;
  }

  /** Something else changed (settings, size): draw at the next animation frame. */
  invalidate(): void {
    this.dirty = true;
  }

  private schedule(): void {
    if (this.handle === null && this.active) this.handle = this.deps.raf(() => this.tick());
  }

  private cancel(): void {
    if (this.handle !== null) this.deps.caf(this.handle);
    this.handle = null;
  }

  private tick(): void {
    this.handle = null;
    if (!this.active) return;
    const now = this.deps.now();
    const fallback = now - this.lastVideo >= VIDEO_IDLE_MS && now - this.last >= FRAME_INTERVAL_MS;
    if (this.dirty || fallback) {
      this.dirty = false;
      this.last = now;
      this.draw();
    }
    this.schedule();
  }
}

/** The size of a source's current frame (0 x 0 before the first one). */
export function sourceSize(source: PanoramaSource | null | undefined): [number, number] {
  if (!source) return [0, 0];
  return 'videoWidth' in source
    ? [source.videoWidth, source.videoHeight]
    : [source.width, source.height];
}

const hasFrame = (source: PanoramaSource | null | undefined): source is PanoramaSource => {
  const [width, height] = sourceSize(source);
  return width > 0 && height > 0;
};

/** Pixel size of the panorama canvas for a CSS width (DPR aware, never above the frames'). */
export function backingSize(
  geometry: { width: number; height: number },
  cssWidth: number,
  dpr: number,
): { width: number; height: number; scale: number } {
  const wanted = cssWidth > 0 ? cssWidth * (dpr > 0 ? dpr : 1) : geometry.width;
  const scale = Math.min(1, wanted / geometry.width);
  return {
    width: Math.max(1, Math.round(geometry.width * scale)),
    height: Math.max(1, Math.round(geometry.height * scale)),
    scale,
  };
}

type FrameCallbackVideo = HTMLVideoElement & {
  requestVideoFrameCallback(callback: () => void): number;
  cancelVideoFrameCallback(handle: number): void;
};

function frameCallbacks(source: PanoramaSource): source is FrameCallbackVideo {
  return (
    'requestVideoFrameCallback' in source && typeof source.requestVideoFrameCallback === 'function'
  );
}

/** Pixels a mesh triangle's clip region grows outwards, so neighbours overlap (no seams). */
const SEAM = 0.6;

export interface PaintOptions {
  /** The canvas' CSS width (0: the frames' own width) and the device pixel ratio. */
  cssWidth?: number;
  dpr?: number;
  /** Background behind missing parts (a camera without a picture). */
  background?: string;
  /** The scratch canvas for the right frame (reused between frames). */
  scratch(): HTMLCanvasElement;
}

let meshCache: { key: string; mesh: MeshTriangle[] | null } | null = null;

/** The warp mesh of a non-affine geometry (cached for the last homography and frame size). */
function meshOf(geometry: StitchGeometry, width: number, height: number): MeshTriangle[] | null {
  const key = `${geometry.right.join(',')}|${width}x${height}`;
  if (meshCache?.key !== key) {
    const { columns, rows } = meshGrid(geometry.right, width, height);
    meshCache = { key, mesh: warpMesh(geometry.right, width, height, columns, rows) };
  }
  return meshCache.mesh;
}

/** A triangle grown by `by` pixels away from its centroid. */
function grown(points: readonly (readonly [number, number])[], by: number): [number, number][] {
  const cx = (points[0]![0] + points[1]![0] + points[2]![0]) / 3;
  const cy = (points[0]![1] + points[1]![1] + points[2]![1]) / 3;
  return points.map(([x, y]) => {
    const d = Math.hypot(x - cx, y - cy) || 1;
    return [x + ((x - cx) / d) * by, y + ((y - cy) / d) * by];
  });
}

/** Draw the right frame into `context` (panorama pixels times `s`) by the geometry's warp. */
function drawWarped(
  context: CanvasRenderingContext2D,
  source: PanoramaSource,
  geometry: StitchGeometry,
  width: number,
  height: number,
  s: number,
): void {
  if (geometry.affine) {
    context.setTransform(...scaled(affineOf(geometry.right), s));
    context.drawImage(source, 0, 0, width, height);
    context.setTransform(1, 0, 0, 1, 0, 0);
    return;
  }
  for (const triangle of meshOf(geometry, width, height) ?? []) {
    const [a, b, c] = grown(
      triangle.dst.map(([x, y]) => [x * s, y * s] as const),
      SEAM,
    );
    context.save();
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.beginPath();
    context.moveTo(a![0], a![1]);
    context.lineTo(b![0], b![1]);
    context.lineTo(c![0], c![1]);
    context.closePath();
    context.clip();
    context.setTransform(...scaled(triangle.transform, s));
    context.drawImage(source, 0, 0, width, height);
    context.restore();
  }
}

/**
 * Draw one panorama of `left` and `right` for the unit homography into `canvas` (sized to the
 * panorama); false when neither source has a picture yet.
 */
export function paintPanorama(
  canvas: HTMLCanvasElement,
  left: PanoramaSource | null,
  right: PanoramaSource | null,
  homography: Homography,
  options: PaintOptions,
): boolean {
  const reference = hasFrame(left) ? left : hasFrame(right) ? right : null;
  if (!reference) return false;
  const [frameWidth, frameHeight] = sourceSize(reference);
  const geometry =
    stitchGeometry(frameWidth, frameHeight, homography) ??
    stitchGeometry(frameWidth, frameHeight, manualHomography(DEFAULT_PANORAMA))!;
  const size = backingSize(geometry, options.cssWidth ?? 0, options.dpr ?? 1);
  if (canvas.width !== size.width) canvas.width = size.width;
  if (canvas.height !== size.height) canvas.height = size.height;
  const context = canvas.getContext('2d');
  if (!context) return false;
  const s = size.scale;
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.globalCompositeOperation = 'source-over';
  context.fillStyle = options.background ?? '#000';
  context.fillRect(0, 0, size.width, size.height);
  if (hasFrame(left)) {
    context.drawImage(
      left,
      geometry.origin[0] * s,
      geometry.origin[1] * s,
      frameWidth * s,
      frameHeight * s,
    );
  }
  if (hasFrame(right)) {
    const layer = options.scratch();
    if (layer.width !== size.width) layer.width = size.width;
    if (layer.height !== size.height) layer.height = size.height;
    const scratch = layer.getContext('2d');
    if (scratch) {
      scratch.globalCompositeOperation = 'source-over';
      scratch.setTransform(1, 0, 0, 1, 0, 0);
      scratch.clearRect(0, 0, size.width, size.height);
      drawWarped(scratch, right, geometry, frameWidth, frameHeight, s);
      if (hasFrame(left) && geometry.feather) {
        const { start, end } = geometry.feather;
        const gradient = scratch.createLinearGradient(start * s, 0, end * s, 0);
        gradient.addColorStop(0, 'rgba(0, 0, 0, 0)');
        gradient.addColorStop(1, 'rgba(0, 0, 0, 1)');
        scratch.globalCompositeOperation = 'destination-in';
        scratch.fillStyle = gradient;
        scratch.fillRect(0, 0, size.width, size.height);
        scratch.globalCompositeOperation = 'source-over';
      }
      context.drawImage(layer, 0, 0);
    }
  }
  return true;
}

export interface RendererOptions {
  /** The left and right source (the tiles' video or test-pattern canvas). */
  sources(): readonly (PanoramaSource | null)[];
  /** The calibration's homography (right to left, unit coordinates). */
  homography(): Homography;
  /** Called after every drawn frame with the count so far. */
  onFrame?(frames: number): void;
  /** Background behind missing parts (a camera without a picture). */
  background?: string;
  clock?: ClockDeps;
  doc?: Document;
}

export class PanoramaRenderer {
  frames = 0;
  private cssWidth = 0;
  private dpr = 1;
  private running = false;
  private scratch: HTMLCanvasElement | null = null;
  private readonly watched = new Map<FrameCallbackVideo, number>();
  private readonly clock: PanoramaClock;

  constructor(
    private readonly canvas: HTMLCanvasElement,
    private readonly options: RendererOptions,
  ) {
    this.clock = new PanoramaClock(() => this.draw(), options.clock);
  }

  start(): void {
    this.running = true;
    this.clock.start();
  }

  stop(): void {
    this.running = false;
    this.clock.stop();
    for (const [video, handle] of this.watched) video.cancelVideoFrameCallback(handle);
    this.watched.clear();
  }

  setVisible(visible: boolean): void {
    this.clock.setVisible(visible);
  }

  /** The canvas' CSS width and the device pixel ratio (from a ResizeObserver). */
  resize(cssWidth: number, dpr: number): void {
    this.cssWidth = cssWidth;
    this.dpr = dpr;
    this.clock.invalidate();
  }

  /** The calibration changed: draw again. */
  invalidate(): void {
    this.clock.invalidate();
  }

  /** Ask each video source to report its next presented frame. */
  private watch(source: PanoramaSource | null): void {
    if (!source || !frameCallbacks(source) || this.watched.has(source)) return;
    const handle = source.requestVideoFrameCallback(() => {
      this.watched.delete(source);
      if (!this.running) return;
      this.clock.frame();
      this.watch(source);
    });
    this.watched.set(source, handle);
  }

  /** Draw one panorama frame; false when no source has a picture yet. */
  draw(): boolean {
    const [left = null, right = null] = this.options.sources();
    this.watch(left);
    this.watch(right);
    const drawn = paintPanorama(this.canvas, left, right, this.options.homography(), {
      cssWidth: this.cssWidth,
      dpr: this.dpr,
      background: this.options.background,
      scratch: () => (this.scratch ??= (this.options.doc ?? document).createElement('canvas')),
    });
    if (!drawn) return false;
    this.frames += 1;
    this.options.onFrame?.(this.frames);
    return true;
  }
}

/**
 * A small greyscale copy of a source's current frame, as wide as the correlation compares (for
 * the overlap estimate).
 */
export function grabGray(
  source: PanoramaSource | null,
  width = CORRELATION_WIDTH,
  doc: Document = document,
): GrayImage | null {
  const [sourceWidth, sourceHeight] = sourceSize(source);
  if (!source || !sourceWidth || !sourceHeight) return null;
  const height = Math.max(1, Math.round((width * sourceHeight) / sourceWidth));
  try {
    const canvas = doc.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d', { willReadFrequently: true });
    if (!context) return null;
    context.drawImage(source, 0, 0, width, height);
    return toGray(context.getImageData(0, 0, width, height).data, width, height);
  } catch {
    return null;
  }
}
