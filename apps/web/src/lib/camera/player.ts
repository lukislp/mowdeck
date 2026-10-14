/**
 * Camera players: what the viewer needs from a video source. `fake` sessions draw the test
 * pattern on a canvas; `agora` sessions load the Agora Web SDK lazily (`agora-player.ts`, its own
 * chunk) and join the channel as audience.
 *
 * A player fills one or more tiles: a {@link CameraChoice} is one camera (its uid), the first
 * camera that publishes video (null), or several uids side by side (both front cameras, tile
 * order). A tile only ever shows the camera it was asked for.
 */
import type { DiagnosticsSnapshot } from './diagnostics';
import type { CameraOptions } from './options';
import type { CameraSession } from './session';
import {
  drawTestPattern,
  positionText,
  type PatternColors,
  type PatternContext,
} from './test-pattern';

/** One camera (uid), the first that publishes video (null), or uids side by side. */
export type CameraChoice = number | null | readonly number[];

/** The wanted uid of every tile (null: the first publisher). */
export function slotsOf(choice: CameraChoice): (number | null)[] {
  return typeof choice === 'object' && choice !== null ? [...choice] : [choice];
}

/** What a player reports back to the viewer. */
export interface PlayerEvents {
  /** Video frames arrive. */
  onLive(): void;
  /** Joined, but no camera publishes video right now. */
  onWaiting(): void;
  /** The token is about to expire: renew it. */
  onTokenExpiring(): void;
  /** The stream ended for good (expired token, decryption error, connection lost). */
  onFailed(kind: 'expired' | 'failed'): void;
  /**
   * The connection dropped (`disconnected`) or stayed `RECONNECTING` too long (`stuck`): the
   * viewer may join again with a fresh session. Without a handler the stream fails.
   */
  onConnectionLost?(kind: 'disconnected' | 'stuck'): void;
  /** The uids that publish video right now (the picker lists only these). */
  onPublishers?(uids: number[]): void;
  /** The uid whose picture the first tile shows (null: none). */
  onActive?(uid: number | null): void;
  /** The uids whose picture is attached to a tile right now, tile order. */
  onTracks?(uids: number[]): void;
}

export interface CameraPlayer {
  /** Join and play `choice` (one camera, the first publisher, or uids side by side). */
  start(session: CameraSession, choice: CameraChoice, options?: CameraOptions): Promise<void>;
  /** Apply stream fallback and data saver to a running connection (profile needs a rejoin). */
  applyOptions?(options: CameraOptions): Promise<void>;
  /** Receive statistics, connection state and event log (null: nothing to measure). */
  diagnostics?(): DiagnosticsSnapshot | null;
  /** Use the token of a fresh session of the same channel. */
  renew(session: CameraSession): Promise<void>;
  /** Show another camera, or other cameras side by side. */
  select(choice: CameraChoice): Promise<void>;
  /** Sound on or off (video plays muted by default). */
  setMuted(muted: boolean): void;
  /** Leave and release everything. Idempotent. */
  stop(): Promise<void>;
}

export interface FakeFrameSource {
  /** Mower and camera name under the clock, for the camera `uid` (null: none chosen). */
  label(uid: number | null): string;
  /** Draw this camera mirrored (the right camera of a stereo pair). */
  mirror?(uid: number | null): boolean;
  /** The mower's latest position. */
  position(): { lat: number; lon: number } | null;
  /** The pattern colours (from the design tokens). */
  colors(): PatternColors;
}

/** The canvas of a tile (a single canvas serves tile 0 only). */
export type CanvasTarget = HTMLCanvasElement | ((slot: number) => HTMLCanvasElement | null);

/** Interval of the test pattern (10 frames per second keeps it cheap). */
export const FAKE_FRAME_MS = 100;

/** The player of `fake` sessions: the test pattern on a canvas per tile. */
export class FakePlayer implements CameraPlayer {
  private timer: ReturnType<typeof setInterval> | null = null;
  private tick = 0;
  /** The uids the session lists (every simulated camera "publishes"). */
  private known: number[] = [];
  /** The uid every tile shows. */
  slots: (number | null)[] = [null];
  renewed = 0;

  constructor(
    private readonly canvas: CanvasTarget,
    private readonly source: FakeFrameSource,
    private readonly events: PlayerEvents,
    private readonly now: () => number = () => Date.now(),
  ) {}

  /** The first tile's camera. */
  get uid(): number | null {
    return this.slots[0] ?? null;
  }

  async start(session: CameraSession, choice: CameraChoice): Promise<void> {
    this.known = (session.cameras ?? [])
      .map((camera) => camera.uid)
      .filter((value): value is number => value !== null && value !== undefined);
    this.events.onPublishers?.(this.known);
    this.show(choice);
    this.timer ??= setInterval(() => this.draw(), FAKE_FRAME_MS);
    this.events.onLive();
  }

  async renew(_session: CameraSession): Promise<void> {
    this.renewed += 1;
  }

  async select(choice: CameraChoice): Promise<void> {
    this.show(choice);
  }

  setMuted(_muted: boolean): void {
    // The pattern has no sound.
  }

  async stop(): Promise<void> {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
  }

  /** A uid this simulated mower "publishes". */
  private delivers(uid: number | null): uid is number {
    return uid !== null && this.known.includes(uid);
  }

  private show(choice: CameraChoice): void {
    this.slots = slotsOf(choice).map((uid) => uid ?? this.known[0] ?? null);
    this.events.onActive?.(this.uid);
    // A camera the session does not list delivers nothing (its tile says so after a while).
    this.events.onTracks?.(this.slots.filter((uid) => this.delivers(uid)));
    this.draw();
  }

  private canvasOf(slot: number): HTMLCanvasElement | null {
    if (typeof this.canvas === 'function') return this.canvas(slot);
    return slot === 0 ? this.canvas : null;
  }

  /** Draw one frame on every tile now (also called by the interval). */
  draw(): void {
    this.tick += 1;
    this.slots.forEach((uid, slot) => {
      const canvas = this.canvasOf(slot);
      if (!canvas || (this.known.length && !this.delivers(uid))) return;
      const ctx = canvas.getContext('2d') as PatternContext | null;
      if (!ctx) return;
      canvas.dataset.frames = String(this.tick);
      drawTestPattern(
        ctx,
        canvas.width,
        canvas.height,
        {
          now: this.now(),
          label: this.source.label(uid),
          position: positionText(this.source.position()),
          tick: this.tick,
          mirror: this.source.mirror?.(uid) ?? false,
        },
        this.source.colors(),
      );
    });
  }
}
