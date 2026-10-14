/**
 * JPEG frames of the camera panel for AI assist. The panel marks every tile's picture (a
 * `<video>`, or the simulator's test pattern `<canvas>`) with `data-camera-video`, plus
 * `data-camera-name`, `data-camera-side` (`left` / `right` for the front cameras) and
 * `data-camera-live` (`false` while the tile shows nothing). Without a live tile no image is
 * sent and the Vision service asks the mower for a photo itself.
 *
 * Stereo: with both front cameras live (and the user's preference on) both frames of the same
 * moment go out as a pair. The two grabs start together; where the browser has
 * `requestVideoFrameCallback` each frame is taken in its own frame callback and timed by the
 * frame's receive time, otherwise both are drawn back to back. A pair further apart than
 * {@link STEREO_MAX_SKEW_MS} is grabbed once more; if it still does not match, only the left
 * frame is sent (never two frames of different moments as a pair).
 */
import type { VisionImage } from './api';

export const CAMERA_VIDEO_SELECTOR = '[data-camera-video]';
export const SNAPSHOT_MAX_WIDTH = 1280;
export const SNAPSHOT_QUALITY = 0.8;
/** The two frames of a stereo pair are at most this far apart. */
export const STEREO_MAX_SKEW_MS = 200;
/** How long to wait for a video's next frame before taking the current one. */
export const FRAME_WAIT_MS = 250;
/** Grabs of a pair before giving up on stereo. */
export const STEREO_ATTEMPTS = 2;

export type CameraElement = HTMLVideoElement | HTMLCanvasElement;
export type CameraSide = 'left' | 'right';

/** A tile's picture with what the panel says about it. */
export interface CameraSource {
  element: CameraElement;
  camera: string | null;
  side: CameraSide | null;
}

/** One frame drawn into a canvas, with its time (epoch milliseconds). */
export interface Grab {
  canvas: HTMLCanvasElement;
  at: number;
}

/** Width and height scaled down (never up) so the width is at most `maxWidth`. */
export function scaledSize(
  width: number,
  height: number,
  maxWidth: number = SNAPSHOT_MAX_WIDTH,
): { width: number; height: number } {
  if (width <= maxWidth) return { width, height };
  const factor = maxWidth / width;
  return { width: maxWidth, height: Math.max(1, Math.round(height * factor)) };
}

/** Standard base64 of `bytes` (chunked, so large images do not overflow the call stack). */
export function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

/** The base64 part of a `data:<type>;base64,<data>` URL; null for anything else. */
export function dataUrlToBase64(url: string): string | null {
  const match = /^data:[^;,]+;base64,(.+)$/.exec(url);
  return match ? match[1]! : null;
}

/** The bytes of a blob as base64. */
export async function blobToBase64(blob: Blob): Promise<string> {
  if (typeof blob.arrayBuffer === 'function') {
    return bytesToBase64(new Uint8Array(await blob.arrayBuffer()));
  }
  // Older engines: read it as a data URL and keep the base64 part.
  const url = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error('read failed'));
    reader.readAsDataURL(blob);
  });
  return dataUrlToBase64(url) ?? '';
}

function sideOf(value: string | undefined): CameraSide | null {
  return value === 'left' || value === 'right' ? value : null;
}

/** The live camera tiles on the page, in page order (left before right). */
export function findCameraSources(root: ParentNode = document): CameraSource[] {
  return [...root.querySelectorAll<CameraElement>(CAMERA_VIDEO_SELECTOR)]
    .filter((element) => element.dataset.cameraLive !== 'false')
    .map((element) => ({
      element,
      camera: element.dataset.cameraName?.trim() || null,
      side: sideOf(element.dataset.cameraSide),
    }));
}

/** The first live camera picture on the page, if the camera panel shows one. */
export function findCameraVideo(root: ParentNode = document): CameraElement | null {
  return findCameraSources(root)[0]?.element ?? null;
}

/** The left and right front camera when both tiles are live. */
export function stereoSources(
  sources: readonly CameraSource[],
): { left: CameraSource; right: CameraSource } | null {
  const left = sources.find((s) => s.side === 'left');
  const right = sources.find((s) => s.side === 'right');
  return left && right && left.element !== right.element ? { left, right } : null;
}

function sizeOf(element: CameraElement): [number, number] {
  return 'videoWidth' in element
    ? [element.videoWidth, element.videoHeight]
    : [element.width, element.height];
}

/** Draw the element's current frame into a new canvas at most `maxWidth` wide; null without one. */
export function grabFrame(
  element: CameraElement,
  at: number = Date.now(),
  maxWidth: number = SNAPSHOT_MAX_WIDTH,
): Grab | null {
  const [width, height] = sizeOf(element);
  if (!width || !height) return null;
  try {
    const size = scaledSize(width, height, maxWidth);
    const canvas = document.createElement('canvas');
    canvas.width = size.width;
    canvas.height = size.height;
    const context = canvas.getContext('2d');
    if (!context) return null;
    context.drawImage(element, 0, 0, size.width, size.height);
    return { canvas, at };
  } catch {
    return null;
  }
}

/** The metadata `requestVideoFrameCallback` hands over (the fields used). */
interface FrameMetadata {
  receiveTime?: number;
  presentationTime?: number;
  expectedDisplayTime?: number;
}

type FrameCallbackVideo = HTMLVideoElement & {
  requestVideoFrameCallback?: (callback: (now: number, meta: FrameMetadata) => void) => number;
  cancelVideoFrameCallback?: (handle: number) => void;
};

/** A `performance.now()` time as epoch milliseconds. */
export function epochOf(performanceTime: number, origin: number = performance.timeOrigin): number {
  return Math.round(origin + performanceTime);
}

/**
 * The next frame of a video, taken inside its frame callback and timed by when it arrived
 * (receive time, else presentation time); a canvas, or a browser without frame callbacks, gives
 * the current frame at once. After `waitMs` without a frame the current one is taken.
 */
export function grabNextFrame(
  element: CameraElement,
  { waitMs = FRAME_WAIT_MS, maxWidth = SNAPSHOT_MAX_WIDTH } = {},
): Promise<Grab | null> {
  const video = element as FrameCallbackVideo;
  if (!('videoWidth' in element) || typeof video.requestVideoFrameCallback !== 'function') {
    return Promise.resolve(grabFrame(element, Date.now(), maxWidth));
  }
  return new Promise((resolve) => {
    let done = false;
    const timer = setTimeout(() => {
      if (done) return;
      done = true;
      if (typeof video.cancelVideoFrameCallback === 'function') {
        video.cancelVideoFrameCallback(handle);
      }
      resolve(grabFrame(element, Date.now(), maxWidth));
    }, waitMs);
    const handle = video.requestVideoFrameCallback!((now, meta) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      const when = meta.receiveTime ?? meta.presentationTime ?? meta.expectedDisplayTime ?? now;
      resolve(grabFrame(element, epochOf(when), maxWidth));
    });
  });
}

/** Two grabs belong to the same moment. */
export function samePair(left: Grab, right: Grab, maxSkewMs: number = STEREO_MAX_SKEW_MS): boolean {
  return Math.abs(left.at - right.at) <= maxSkewMs;
}

/** Both frames of the same moment, or null when they could not be paired. */
export async function captureStereo(
  left: CameraElement,
  right: CameraElement,
  {
    maxSkewMs = STEREO_MAX_SKEW_MS,
    attempts = STEREO_ATTEMPTS,
    grab = grabNextFrame,
  }: {
    maxSkewMs?: number;
    attempts?: number;
    grab?: (element: CameraElement) => Promise<Grab | null>;
  } = {},
): Promise<[Grab, Grab] | null> {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const [l, r] = await Promise.all([grab(left), grab(right)]);
    if (!l || !r) return null;
    if (samePair(l, r, maxSkewMs)) return [l, r];
  }
  return null;
}

/** A grab as a JPEG for the Vision API, with the camera when known; null when it fails. */
export async function encodeGrab(
  grab: Grab,
  meta: { camera?: string | null; side?: CameraSide | null } = {},
  quality: number = SNAPSHOT_QUALITY,
): Promise<VisionImage | null> {
  try {
    const canvas = grab.canvas;
    const blob = await new Promise<Blob | null>((resolve) => {
      if (typeof canvas.toBlob === 'function') canvas.toBlob(resolve, 'image/jpeg', quality);
      else resolve(null);
    });
    const data = blob
      ? await blobToBase64(blob)
      : dataUrlToBase64(canvas.toDataURL('image/jpeg', quality));
    if (!data) return null;
    const image: VisionImage = { media_type: 'image/jpeg', data };
    if (meta.camera) image.camera = meta.camera;
    if (meta.side) {
      image.side = meta.side;
      image.captured_at = new Date(grab.at).toISOString();
    }
    return image;
  } catch {
    return null;
  }
}

/**
 * One JPEG frame of `video` (or a canvas), at most `maxWidth` wide, as an image for the Vision
 * API; null when there is no frame yet or the canvas is unavailable (or tainted). Never throws.
 */
export async function snapshotVideo(
  video: CameraElement,
  { maxWidth = SNAPSHOT_MAX_WIDTH, quality = SNAPSHOT_QUALITY } = {},
): Promise<VisionImage | null> {
  const grab = grabFrame(video, Date.now(), maxWidth);
  return grab ? encodeGrab(grab, {}, quality) : null;
}

/**
 * The frames of the page's camera panel: both front cameras of the same moment when `stereo`
 * is asked and both tiles are live, else the first live tile; empty without a camera.
 */
export async function cameraImages(
  root: ParentNode = document,
  { stereo = false }: { stereo?: boolean } = {},
): Promise<VisionImage[]> {
  const sources = findCameraSources(root);
  const pair = stereo ? stereoSources(sources) : null;
  if (pair) {
    const grabs = await captureStereo(pair.left.element, pair.right.element);
    if (grabs) {
      const images = await Promise.all([
        encodeGrab(grabs[0], pair.left),
        encodeGrab(grabs[1], pair.right),
      ]);
      if (images[0] && images[1]) return [images[0], images[1]];
    }
  }
  const first = sources[0];
  if (!first) return [];
  const grab = grabFrame(first.element);
  const image = grab ? await encodeGrab(grab, { camera: first.camera }) : null;
  return image ? [image] : [];
}
