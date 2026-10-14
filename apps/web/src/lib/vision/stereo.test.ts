/** Stereo frames for AI assist: the camera tiles, pairing both frames of the same moment. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  cameraImages,
  captureStereo,
  encodeGrab,
  epochOf,
  findCameraSources,
  findCameraVideo,
  grabNextFrame,
  samePair,
  STEREO_MAX_SKEW_MS,
  stereoSources,
  type Grab,
} from './snapshot';

const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10]);

function tile(
  kind: 'video' | 'canvas',
  attrs: Record<string, string>,
  size: [number, number] = [640, 360],
): HTMLVideoElement | HTMLCanvasElement {
  const element = document.createElement(kind);
  element.setAttribute('data-camera-video', '');
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  if (element instanceof HTMLVideoElement) {
    Object.defineProperty(element, 'videoWidth', { value: size[0] });
    Object.defineProperty(element, 'videoHeight', { value: size[1] });
  } else {
    element.width = size[0];
    element.height = size[1];
  }
  document.body.append(element);
  return element;
}

function mockCanvas(): { draws: unknown[] } {
  const draws: unknown[] = [];
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => ({ drawImage: (source: unknown) => draws.push(source) }) as never,
  );
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((callback: BlobCallback) =>
    callback(new Blob([JPEG])),
  );
  return { draws };
}

function grab(at: number): Grab {
  return { canvas: document.createElement('canvas'), at };
}

describe('camera tiles on the page', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = '';
  });

  it('lists live tiles with camera and side, skipping tiles without a picture', () => {
    const left = tile('video', {
      'data-camera-name': 'Front left',
      'data-camera-side': 'left',
      'data-camera-live': 'true',
    });
    tile('video', { 'data-camera-name': 'Front right', 'data-camera-live': 'false' });
    const right = tile('canvas', {
      'data-camera-name': 'Front right',
      'data-camera-side': 'right',
    });
    const sources = findCameraSources();
    expect(sources.map((s) => [s.element, s.camera, s.side])).toEqual([
      [left, 'Front left', 'left'],
      [right, 'Front right', 'right'],
    ]);
    expect(findCameraVideo()).toBe(left);
    expect(stereoSources(sources)?.right.element).toBe(right);
    expect(stereoSources([sources[0]!])).toBeNull();
  });
});

describe('stereo pairing', () => {
  it('accepts two grabs within 200 ms and retries once before giving up', async () => {
    expect(STEREO_MAX_SKEW_MS).toBe(200);
    expect(samePair(grab(1000), grab(1180))).toBe(true);
    expect(samePair(grab(1000), grab(1250))).toBe(false);
    const left = document.createElement('video');
    const right = document.createElement('video');
    const times = [1000, 1400, 2000, 2050];
    const grabs = vi.fn(async () => grab(times.shift() ?? 0));
    const pair = await captureStereo(left, right, { grab: grabs });
    expect(pair?.map((g) => g.at)).toEqual([2000, 2050]);
    expect(grabs).toHaveBeenCalledTimes(4);
    const apart = [0, 900, 0, 900];
    expect(
      await captureStereo(left, right, { grab: async () => grab(apart.shift() ?? 0) }),
    ).toBeNull();
    expect(await captureStereo(left, right, { grab: async () => null })).toBeNull();
  });

  it('takes the next video frame in its frame callback, timed by its receive time', async () => {
    mockCanvas();
    const video = document.createElement('video');
    Object.defineProperty(video, 'videoWidth', { value: 320 });
    Object.defineProperty(video, 'videoHeight', { value: 180 });
    Object.defineProperty(video, 'requestVideoFrameCallback', {
      value: (cb: (now: number, meta: object) => void) => {
        queueMicrotask(() => cb(500, { receiveTime: 480 }));
        return 1;
      },
    });
    const taken = await grabNextFrame(video);
    expect(taken?.at).toBe(epochOf(480));
    expect(epochOf(480, 1_000)).toBe(1_480);
  });

  it('takes the current frame when no frame comes in time', async () => {
    vi.useFakeTimers();
    try {
      mockCanvas();
      const cancel = vi.fn();
      const video = document.createElement('video');
      Object.defineProperty(video, 'videoWidth', { value: 320 });
      Object.defineProperty(video, 'videoHeight', { value: 180 });
      Object.defineProperty(video, 'requestVideoFrameCallback', { value: () => 7 });
      Object.defineProperty(video, 'cancelVideoFrameCallback', { value: cancel });
      const pending = grabNextFrame(video, { waitMs: 100 });
      vi.advanceTimersByTime(100);
      expect((await pending)?.canvas.width).toBe(320);
      expect(cancel).toHaveBeenCalledWith(7);
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
    }
  });
});

describe('frames for the Vision API', () => {
  beforeEach(() => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-10-11T12:00:00.120Z'));
  });

  afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = '';
  });

  it('marks a stereo frame with camera, side and capture time', async () => {
    mockCanvas();
    const image = await encodeGrab(grab(Date.parse('2026-10-11T12:00:00.120Z')), {
      camera: 'Front left',
      side: 'left',
    });
    expect(image).toEqual({
      media_type: 'image/jpeg',
      data: '/9j/4AAQ',
      camera: 'Front left',
      side: 'left',
      captured_at: '2026-10-11T12:00:00.120Z',
    });
  });

  it('sends both front cameras as a pair when asked, else the first tile alone', async () => {
    const { draws } = mockCanvas();
    const left = tile('canvas', { 'data-camera-name': 'Front left', 'data-camera-side': 'left' });
    const right = tile('canvas', {
      'data-camera-name': 'Front right',
      'data-camera-side': 'right',
    });
    const pair = await cameraImages(document, { stereo: true });
    expect(pair.map((i) => [i.camera, i.side])).toEqual([
      ['Front left', 'left'],
      ['Front right', 'right'],
    ]);
    expect(pair[0]?.captured_at).toBe(pair[1]?.captured_at);
    expect(draws.slice(0, 2)).toEqual([left, right]);
    const single = await cameraImages(document);
    expect(single).toEqual([{ media_type: 'image/jpeg', data: '/9j/4AAQ', camera: 'Front left' }]);
  });

  it('falls back to one frame without a second live tile', async () => {
    mockCanvas();
    tile('video', { 'data-camera-name': 'Front left', 'data-camera-side': 'left' });
    tile('video', {
      'data-camera-name': 'Front right',
      'data-camera-side': 'right',
      'data-camera-live': 'false',
    });
    const images = await cameraImages(document, { stereo: true });
    expect(images).toHaveLength(1);
    expect(images[0]?.side).toBeUndefined();
  });
});
