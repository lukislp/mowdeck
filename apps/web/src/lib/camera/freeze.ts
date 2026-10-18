/**
 * Picture freeze detection (ADR 0044). The drive view must never show a frozen picture as if it
 * were live, and it holds the mower while the picture stands. The camera stage reads a frame
 * counter of every shown tile a few times a second and feeds it to a {@link FreezeWatch}:
 *
 * - a tile that is live and whose counter has not moved for longer than its threshold is frozen.
 *   The threshold follows the tile's own frame interval: three typical intervals, never below
 *   {@link FREEZE_FLOOR_MS} and never above {@link FREEZE_CEILING_MS}, so a steady but slow
 *   stream (a docked mower over 4G at 1 to 5 fps) is not mistaken for a frozen one, while a
 *   fast stream that stops is caught after 1.5 s. Only gaps that did not count as frozen teach
 *   the interval, so a real freeze never makes the next one harder to see;
 * - a tile that was live during this watch and is no longer (reconnecting, the track went away)
 *   counts as frozen too: the picture is gone, the driver is blind;
 * - a tile that has never been live yet (still connecting) is not frozen: nothing is shown that
 *   could be mistaken for a live picture, and the status says it is connecting.
 *
 * Two outputs with their own thresholds (owner report 2026-10-18: "Bild steht" flickered every
 * 2 to 3 s on a LUBA streaming over 4G while mowing, and showed while the picture was still
 * being set up):
 *
 * - **Hold** ({@link FreezeWatch.observe}'s result): the strict signal the drive view holds the
 *   mower on. It keeps the rules above (1.5 s floor, a picture that went away counts), so a real
 *   freeze stops the mower within about 1.5 to 2 s. A bursty but alive stream may trigger it for
 *   a moment; holding briefly is the safe side.
 * - **Label** ({@link FreezeWatch.labelled}): what the stage shows as "Bild steht". It has
 *   hysteresis: a live tile's gap must last at least {@link LABEL_FLOOR_MS} (and never less than
 *   the hold threshold) before the label shows, and it clears at the next new frame. Before the
 *   tile has presented {@link LABEL_WARM_FRAMES} frames (cold start: the decoder and the jitter
 *   buffer are still settling) the gap must last {@link LABEL_COLD_MS}. A tile that is not live
 *   (connecting, reconnecting) never shows the label: the status says it is connecting.
 *
 * Frame counters ({@link frameCounter}): the test pattern's canvas counts its frames
 * (`data-frames`). A `<video>` counts the frames the browser actually presented, through
 * `requestVideoFrameCallback` (Chrome, Edge, Safari 15.4+, Firefox 132+). Only where that is
 * missing does it fall back to `getVideoPlaybackQuality().totalVideoFrames`. The playback time
 * is never used: for a WebRTC `MediaStream` it is a wall clock that keeps running while the
 * picture stands (measured in Chromium and Firefox), and Firefox reports no playback quality
 * for a `MediaStream` at all. No SDK import, unit tested with plain values and fake elements.
 */

/** A live tile without a new frame for longer than this is frozen, however fast its stream. */
export const FREEZE_FLOOR_MS = 1_500;
/** A slow stream's threshold never grows beyond this (a picture that old is not live). */
export const FREEZE_CEILING_MS = 3_000;
/** The threshold is this many typical frame intervals of the tile. */
export const FREEZE_INTERVALS = 3;
/** The stage reads the counters this often. */
export const FREEZE_POLL_MS = 200;
/** The label "Bild steht" shows only after a gap of at least this long (hysteresis). */
export const LABEL_FLOOR_MS = 2_500;
/** Before a tile has presented {@link LABEL_WARM_FRAMES} frames its label needs this long a gap. */
export const LABEL_COLD_MS = 5_000;
/** New frames a tile presents before the label uses {@link LABEL_FLOOR_MS}. */
export const LABEL_WARM_FRAMES = 3;
/** Weight of the newest gap in the tile's typical frame interval. */
const INTERVAL_WEIGHT = 0.3;

/** The freeze threshold of a tile whose typical frame interval is `intervalMs` (null: unknown). */
export function freezeThreshold(
  intervalMs: number | null,
  floorMs: number = FREEZE_FLOOR_MS,
  ceilingMs: number = FREEZE_CEILING_MS,
): number {
  if (intervalMs === null || !Number.isFinite(intervalMs)) return floorMs;
  return Math.min(ceilingMs, Math.max(floorMs, intervalMs * FREEZE_INTERVALS));
}

/** One shown tile at one reading. */
export interface TileReading {
  /** The tile shows a live picture (the viewer's tile status). */
  live: boolean;
  /** Frame counter of its picture element (null: none readable). */
  frames: number | null;
}

/** What the watch knows about one tile (its interval and threshold). */
export interface SlotView {
  /** Typical gap between two new frames in ms (null: not seen two frames yet). */
  intervalMs: number | null;
  /** The tile is frozen after this long without a new frame. */
  thresholdMs: number;
  /** Time since the last new frame (or since the tile went live). */
  sinceMs: number;
}

interface SlotState {
  everLive: boolean;
  frames: number | null;
  changedAt: number;
  interval: number | null;
  /** New frames seen since the tile (last) went live. */
  seen: number;
}

export interface FreezeLimits {
  floorMs?: number;
  ceilingMs?: number;
}

export class FreezeWatch {
  #slots: SlotState[] = [];
  #now = 0;
  #labelled = false;
  private readonly floorMs: number;
  private readonly ceilingMs: number;

  constructor(limits: FreezeLimits = {}) {
    this.floorMs = limits.floorMs ?? FREEZE_FLOOR_MS;
    this.ceilingMs = Math.max(this.floorMs, limits.ceilingMs ?? FREEZE_CEILING_MS);
  }

  /** One reading of every shown tile (slot order); true while any of them is frozen. */
  observe(tiles: readonly TileReading[], now: number): boolean {
    this.#now = now;
    this.#slots.length = Math.min(this.#slots.length, tiles.length);
    let frozen = false;
    let labelled = false;
    tiles.forEach((tile, slot) => {
      const state = (this.#slots[slot] ??= {
        everLive: false,
        frames: null,
        changedAt: now,
        interval: null,
        seen: 0,
      });
      if (!tile.live) {
        // A picture that went away is as bad as one that stands (hold); the label waits: the
        // status says the tile is connecting.
        if (state.everLive) frozen = true;
        state.frames = null;
        state.changedAt = now;
        state.seen = 0;
        return;
      }
      if (!state.everLive || tile.frames === null || tile.frames !== state.frames) {
        if (tile.frames !== state.frames) {
          if (state.frames !== null && tile.frames !== null) {
            this.learn(state, now);
            state.seen += 1;
          }
          state.changedAt = now;
        }
        state.frames = tile.frames;
        state.everLive = true;
        // A live tile without a readable counter cannot be checked; trust the tile status.
        if (tile.frames === null) state.changedAt = now;
        return;
      }
      const since = now - state.changedAt;
      if (since > this.threshold(state)) frozen = true;
      if (since > this.labelThreshold(state)) labelled = true;
    });
    this.#labelled = labelled;
    return frozen;
  }

  /**
   * Whether the stage shows "Bild steht" after the last reading: a live tile whose gap lasted
   * the label's threshold (hysteresis, cleared at the next frame). Never for a tile that is not
   * live (connecting) or has not presented a frame yet.
   */
  get labelled(): boolean {
    return this.#labelled;
  }

  /** What the watch knows about every tile at the last reading. */
  view(): SlotView[] {
    return this.#slots.map((state) => ({
      intervalMs: state.interval === null ? null : Math.round(state.interval),
      thresholdMs: Math.round(this.threshold(state)),
      sinceMs: this.#now - state.changedAt,
    }));
  }

  /** Forget everything (a new session, another camera layout). */
  reset(): void {
    this.#slots = [];
    this.#labelled = false;
  }

  private labelThreshold(state: SlotState): number {
    const floor = state.seen < LABEL_WARM_FRAMES ? LABEL_COLD_MS : LABEL_FLOOR_MS;
    return Math.max(floor, this.threshold(state));
  }

  private threshold(state: SlotState): number {
    return freezeThreshold(state.interval, this.floorMs, this.ceilingMs);
  }

  /** A new frame after a gap: a gap that did not count as frozen teaches the interval. */
  private learn(state: SlotState, now: number): void {
    const gap = now - state.changedAt;
    if (gap <= 0 || gap > this.threshold(state)) return;
    state.interval =
      state.interval === null ? gap : state.interval + (gap - state.interval) * INTERVAL_WEIGHT;
  }
}

/** The frame signals of one `<video>` (null: the browser has none). */
export interface VideoFrameSignals {
  /** Frames presented since the watch began (`requestVideoFrameCallback`). */
  presented: number | null;
  /** `getVideoPlaybackQuality().totalVideoFrames`. */
  decoded: number | null;
  /** `getVideoPlaybackQuality().droppedVideoFrames`. */
  dropped: number | null;
}

/** The parts of a `<video>` the frame clock uses (a fake in tests). */
export interface FrameCallbackVideo {
  srcObject: unknown;
  requestVideoFrameCallback?: (callback: () => void) => number;
  cancelVideoFrameCallback?: (handle: number) => void;
}

interface FrameClock {
  count: number;
  handle: number | null;
  source: unknown;
}

const clocks = new WeakMap<object, FrameClock>();

/**
 * Frames the browser presented in `video` since the first call (null: no
 * `requestVideoFrameCallback`). The first call starts a callback chain that counts every
 * presented frame; a new `srcObject` starts a fresh chain (the counter keeps rising).
 */
export function presentedFrames(video: FrameCallbackVideo): number | null {
  if (typeof video.requestVideoFrameCallback !== 'function') return null;
  let clock = clocks.get(video);
  if (!clock) {
    clock = { count: 0, handle: null, source: undefined };
    clocks.set(video, clock);
  }
  if (clock.handle === null || clock.source !== video.srcObject) {
    const current = clock;
    if (current.handle !== null) video.cancelVideoFrameCallback?.(current.handle);
    current.source = video.srcObject;
    const tick = (): void => {
      current.count += 1;
      current.handle = video.requestVideoFrameCallback?.(tick) ?? null;
    };
    current.handle = video.requestVideoFrameCallback(tick);
  }
  return clock.count;
}

/** The frame signals of `video` (presented frames, else decoded ones count). */
export function videoFrameSignals(video: HTMLVideoElement): VideoFrameSignals {
  const quality =
    typeof video.getVideoPlaybackQuality === 'function' ? video.getVideoPlaybackQuality() : null;
  return {
    presented: presentedFrames(video),
    decoded: quality ? quality.totalVideoFrames : null,
    dropped: quality ? quality.droppedVideoFrames : null,
  };
}

/**
 * The frame counter of a picture element (null: nothing readable, or no frame yet). A video
 * counts presented frames where the browser can (`requestVideoFrameCallback`), else decoded
 * frames; never its playback time.
 */
export function frameCounter(element: HTMLCanvasElement | HTMLVideoElement | null): number | null {
  if (!element) return null;
  if (element instanceof HTMLCanvasElement) {
    const frames = Number(element.dataset.frames);
    return Number.isFinite(frames) && element.dataset.frames !== undefined ? frames : null;
  }
  const signals = videoFrameSignals(element);
  const frames = signals.presented ?? signals.decoded;
  // No frame yet: the picture has not started, which the tile status and the no-data timer cover.
  return frames ? frames : null;
}
