/**
 * Picture freeze detection (ADR 0044). The drive view must never show a frozen picture as if it
 * were live, and it holds the mower while the picture stands. The camera stage reads a frame
 * counter of every shown tile a few times a second and feeds it to a {@link FreezeWatch}:
 *
 * - a tile that is live and whose counter has not moved for more than {@link FREEZE_MS} is
 *   frozen;
 * - a tile that was live during this watch and is no longer (reconnecting, the track went away)
 *   counts as frozen too: the picture is gone, the driver is blind;
 * - a tile that has never been live yet (still connecting) is not frozen: nothing is shown that
 *   could be mistaken for a live picture, and the status says it is connecting.
 *
 * Frame counters: the test pattern's canvas counts its frames (`data-frames`), a `<video>`
 * reports its decoded frames (`getVideoPlaybackQuality().totalVideoFrames`, else the playback
 * time). No SDK import, unit tested with plain values.
 */

/** No new frame for longer than this freezes the picture. */
export const FREEZE_MS = 1_000;
/** The stage reads the counters this often. */
export const FREEZE_POLL_MS = 200;

/** One shown tile at one reading. */
export interface TileReading {
  /** The tile shows a live picture (the viewer's tile status). */
  live: boolean;
  /** Frame counter of its picture element (null: none readable). */
  frames: number | null;
}

interface SlotState {
  everLive: boolean;
  frames: number | null;
  changedAt: number;
}

export class FreezeWatch {
  #slots: SlotState[] = [];

  constructor(private readonly freezeMs: number = FREEZE_MS) {}

  /** One reading of every shown tile (slot order); true while any of them is frozen. */
  observe(tiles: readonly TileReading[], now: number): boolean {
    this.#slots.length = Math.min(this.#slots.length, tiles.length);
    let frozen = false;
    tiles.forEach((tile, slot) => {
      const state = (this.#slots[slot] ??= { everLive: false, frames: null, changedAt: now });
      if (!tile.live) {
        // A picture that went away is as bad as one that stands.
        if (state.everLive) frozen = true;
        state.frames = null;
        state.changedAt = now;
        return;
      }
      if (!state.everLive || tile.frames === null || tile.frames !== state.frames) {
        if (tile.frames !== state.frames) state.changedAt = now;
        state.frames = tile.frames;
        state.everLive = true;
        // A live tile without a readable counter cannot be checked; trust the tile status.
        if (tile.frames === null) state.changedAt = now;
        return;
      }
      if (now - state.changedAt > this.freezeMs) frozen = true;
    });
    return frozen;
  }

  /** Forget everything (a new session, another camera layout). */
  reset(): void {
    this.#slots = [];
  }
}

/** The frame counter of a picture element (null: nothing readable). */
export function frameCounter(element: HTMLCanvasElement | HTMLVideoElement | null): number | null {
  if (!element) return null;
  if (element instanceof HTMLCanvasElement) {
    const frames = Number(element.dataset.frames);
    return Number.isFinite(frames) && element.dataset.frames !== undefined ? frames : null;
  }
  const quality = element.getVideoPlaybackQuality?.();
  if (quality && quality.totalVideoFrames > 0) return quality.totalVideoFrames;
  return element.readyState >= 2 ? element.currentTime : null;
}
