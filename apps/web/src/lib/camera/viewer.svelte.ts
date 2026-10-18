/**
 * The live camera of one mower as a small state machine (ADR 0032): request a session from
 * Fleet, create the player for its provider, renew the token before it expires and release
 * everything on close. The page owns one viewer per open camera panel.
 *
 * Phases: `idle` → `requesting` → `connecting` → `live` (or `waiting` while no camera publishes)
 * → `closed`; any step can end in `error` with a {@link CameraErrorKind}.
 *
 * Cameras: `cameraUid` is the viewer's choice (null: the first camera that publishes video),
 * `publishing` the uids the player reports as publishing video, `activeUid` the one shown. A
 * chosen camera that does not publish is `missing`: the panel says so instead of switching.
 *
 * Layout (`layout.ts`): `single` or `both` (the stereo pair side by side, when the session has
 * one); kept per browser. `live` are the uids whose picture is on a tile; a wanted camera that
 * shows nothing within {@link NO_DATA_MS} lands in `stalled` ("no data from this camera").
 * `tiles` says for every view what it shows.
 *
 * Stability: `options` (per browser, `options.ts`); a lost connection (`DISCONNECTED`, or
 * `RECONNECTING` for more than 20 s) joins again with a fresh session, with a backoff, up to
 * {@link MAX_REJOIN_ATTEMPTS} times in a row. Statistics are read every 2 s while a player can
 * measure: `tracks` (resolution and frame rate per tile) always, the full `diagnostics` while
 * watched (the panel's Diagnose line).
 *
 * Transport (ADR 0045): every session request names the browser's `options.transport` (`auto`
 * by default). The first session of an `open` pins the path it took (`relay` or `direct`): a
 * rejoin, a refresh or a return to the page asks for exactly that path, so `auto` never
 * switches between the relay and the vendor path while a drive runs. A relay session renews
 * its lease in the player (heartbeat); the viewer never requests a new session for it before
 * its ticket expires. A hidden page (`suspend`) stops a relay session at once (the player
 * detaches its lease, the relay stops the source after its grace) and `resume` requests a new
 * one when the page is visible again; the direct path keeps playing as before.
 */
import { asProblem } from '$lib/api/problem';

import type { DiagnosticsSnapshot, TrackDiagnostics } from './diagnostics';
import {
  layoutChoice,
  loadLayout,
  NO_DATA_MS,
  saveLayout,
  tilesOf,
  wantedUids,
  type CameraLayout,
  type CameraTile,
} from './layout';
import {
  clearStaleKeys,
  loadOptions,
  MAX_REJOIN_ATTEMPTS,
  rejoinDelayMs,
  saveOptions,
  type CameraOptions,
} from './options';
import type { CameraTransport } from './options';
import type { CameraPlayer, PlayerEvents } from './player';
import {
  CameraError,
  cameraErrorOf,
  isRelay,
  renewDelayMs,
  sameChannel,
  stereoPair,
  type CameraErrorKind,
  type CameraInfo,
  type CameraSession,
  type StereoPair,
} from './session';

export type ViewerPhase =
  'idle' | 'requesting' | 'connecting' | 'live' | 'waiting' | 'error' | 'closed';

export interface ViewerDeps {
  /** `POST /mowers/{id}/camera/session` with `transport`; throws an `ApiProblem` when refused. */
  request(mowerId: string, transport: CameraTransport): Promise<CameraSession>;
  /** The player for the session's provider. */
  createPlayer(session: CameraSession, events: PlayerEvents): Promise<CameraPlayer>;
  /** The page runs in a secure context (HTTPS or localhost); WebRTC needs it. */
  secure(): boolean;
  /** The installation allows the vendor cloud video (`/config.json` `cameraEnabled`). */
  cameraEnabled(): Promise<boolean>;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  /** The viewer's options (default: from `localStorage`). */
  options?: CameraOptions;
  /** Stores changed options (default: `localStorage`). */
  saveOptions?: (options: CameraOptions) => void;
  /** The stored layout (default: from `localStorage`). */
  layout?: CameraLayout;
  /** Stores a changed layout (default: `localStorage`). */
  saveLayout?: (layout: CameraLayout) => void;
}

/** Statistics are read this often. */
export const DIAGNOSTICS_INTERVAL_MS = 2_000;

export class CameraViewer {
  phase = $state<ViewerPhase>('idle');
  session = $state.raw<CameraSession | null>(null);
  error = $state<CameraErrorKind | null>(null);
  correlationId = $state<string | null>(null);
  /** The chosen camera (null: the first that publishes video). */
  cameraUid = $state<number | null>(null);
  /** The uids that publish video right now (null: not known yet). */
  publishing = $state.raw<number[] | null>(null);
  /** The uid whose picture is shown. */
  activeUid = $state<number | null>(null);
  muted = $state(true);
  renewals = $state(0);
  /** Connection options (persisted per browser). */
  options = $state<CameraOptions>(loadOptions());
  /** The latest statistics (null: not watched or nothing to measure). */
  diagnostics = $state.raw<DiagnosticsSnapshot | null>(null);
  /** Automatic rejoins since the camera opened. */
  rejoins = $state(0);
  /** One camera or both front cameras side by side (persisted per browser). */
  layout = $state<CameraLayout>(loadLayout());
  /** The uids whose picture is on a tile right now. */
  live = $state.raw<number[]>([]);
  /** Wanted uids that delivered nothing within {@link NO_DATA_MS}. */
  stalled = $state.raw<number[]>([]);
  /** Receive numbers per shown camera (resolution and frame rate on every tile). */
  tracks = $state.raw<TrackDiagnostics[]>([]);
  /** The path the current `open` took (pinned for its rejoins; null: none yet). */
  transport = $state<'relay' | 'direct' | null>(null);
  /** A relay session is paused while the page is hidden. */
  suspended = $state(false);

  private player: CameraPlayer | null = null;
  private mowerId: string | null = null;
  private timer: unknown = null;
  private generation = 0;
  private attempts = 0;
  private rejoinTimer: unknown = null;
  private rejoining = false;
  private diagTimer: unknown = null;
  private diagWatched = false;
  private noDataTimer: unknown = null;
  private readonly now: () => number;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (handle: unknown) => void;

  constructor(private readonly deps: ViewerDeps) {
    clearStaleKeys();
    if (deps.options) this.options = deps.options;
    if (deps.layout) this.layout = deps.layout;
    this.now = deps.now ?? (() => Date.now());
    this.setTimer = deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer =
      deps.clearTimer ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  }

  /** The cameras of the current session (empty before one arrived). */
  get cameras(): CameraInfo[] {
    return this.session?.cameras ?? [];
  }

  /** The session's two front cameras (null: no pair, single views only). */
  get pair(): StereoPair | null {
    return stereoPair(this.cameras);
  }

  /** What every view shows, left to right. */
  get tiles(): CameraTile[] {
    return tilesOf({
      layout: this.layout,
      pair: this.pair,
      cameras: this.cameras,
      cameraUid: this.cameraUid,
      activeUid: this.activeUid,
      publishing: this.publishing,
      live: this.live,
      stalled: this.stalled,
    });
  }

  /** Both front cameras are on screen side by side. */
  get both(): boolean {
    return this.tiles.length > 1;
  }

  /** A chosen camera publishes no video while others do. */
  get missing(): boolean {
    const uid = this.cameraUid;
    const publishing = this.publishing;
    return uid !== null && !!publishing?.length && !publishing.includes(uid);
  }

  /** Open the camera of `mowerId` (closes a previous one). */
  async open(mowerId: string): Promise<void> {
    const generation = ++this.generation;
    await this.release();
    if (generation !== this.generation) return;
    this.mowerId = mowerId;
    this.error = null;
    this.correlationId = null;
    this.attempts = 0;
    this.rejoins = 0;
    this.transport = null;
    this.suspended = false;
    this.phase = 'requesting';
    let session: CameraSession;
    try {
      session = await this.deps.request(mowerId, this.wantedTransport());
    } catch (error) {
      if (generation === this.generation) this.fail(error);
      return;
    }
    if (generation !== this.generation) return;
    await this.play(session, generation);
  }

  /** Request a fresh session: renew the token on the same channel, else rejoin. */
  async refresh(): Promise<void> {
    const mowerId = this.mowerId;
    const current = this.session;
    if (!mowerId || !current || this.phase === 'closed' || this.phase === 'error') return;
    const generation = this.generation;
    let next: CameraSession;
    try {
      next = await this.deps.request(mowerId, this.wantedTransport());
    } catch (error) {
      if (generation === this.generation) this.fail(error);
      return;
    }
    if (generation !== this.generation) return;
    if (this.player && sameChannel(current, next)) {
      try {
        await this.player.renew(next);
        this.session = next;
        this.renewals += 1;
        this.schedule(next);
        return;
      } catch {
        // fall through to a rejoin
      }
    }
    await this.release(false);
    await this.play(next, generation);
  }

  /** The transport of the next request: the pinned path, else the browser's option. */
  private wantedTransport(): CameraTransport {
    return this.transport ?? this.options.transport;
  }

  /**
   * The page went hidden: a relay session (or one still being requested) ends at once; the
   * player detaches its lease. The direct path is left as it is.
   */
  async suspend(): Promise<void> {
    if (this.suspended || this.phase === 'closed' || this.phase === 'error' || !this.mowerId) {
      return;
    }
    const pending = this.session === null && this.wantedTransport() !== 'direct';
    if (!isRelay(this.session) && !pending) return;
    this.suspended = true;
    this.generation += 1;
    await this.release(false);
    this.phase = 'connecting';
  }

  /** The page is visible again: a suspended relay session is requested anew. */
  async resume(): Promise<void> {
    const mowerId = this.mowerId;
    if (!this.suspended || !mowerId) return;
    this.suspended = false;
    const generation = ++this.generation;
    this.phase = 'requesting';
    let session: CameraSession;
    try {
      session = await this.deps.request(mowerId, this.wantedTransport());
    } catch (error) {
      if (generation === this.generation) this.fail(error);
      return;
    }
    if (generation !== this.generation) return;
    await this.play(session, generation);
  }

  /** Show one camera of the session (the single layout). */
  async select(uid: number | null): Promise<void> {
    this.cameraUid = uid;
    this.setLayout('single');
    await this.showChoice();
  }

  /** Show both front cameras side by side (only with a stereo pair). */
  async showBoth(): Promise<void> {
    if (!this.pair) return;
    this.setLayout('both');
    await this.showChoice();
  }

  private setLayout(layout: CameraLayout): void {
    if (this.layout === layout) return;
    this.layout = layout;
    (this.deps.saveLayout ?? saveLayout)(layout);
  }

  private choice(): number | null | number[] {
    return layoutChoice(this.layout, this.pair, this.cameraUid);
  }

  private async showChoice(): Promise<void> {
    const choice = this.choice();
    this.armNoData(choice);
    await this.player?.select(choice);
  }

  /** Wanted cameras that show nothing within {@link NO_DATA_MS} are reported as stalled. */
  private armNoData(choice: number | null | readonly number[]): void {
    if (this.noDataTimer !== null) this.clearTimer(this.noDataTimer);
    this.noDataTimer = null;
    this.stalled = [];
    const wanted = wantedUids(choice);
    if (!wanted.length) return;
    const generation = this.generation;
    this.noDataTimer = this.setTimer(() => {
      this.noDataTimer = null;
      if (generation !== this.generation) return;
      this.stalled = wanted.filter((uid) => !this.live.includes(uid));
    }, NO_DATA_MS);
  }

  /** Sound on or off; the camera always starts muted. */
  toggleMute(): void {
    this.muted = !this.muted;
    this.player?.setMuted(this.muted);
  }

  /**
   * Change connection options (stored per browser). Fallback and data saver apply at once; the
   * channel profile and latency level need a new join.
   */
  async setOptions(patch: Partial<CameraOptions>): Promise<void> {
    const previous = this.options;
    const next = { ...previous, ...patch };
    this.options = next;
    (this.deps.saveOptions ?? saveOptions)(next);
    const rejoin =
      next.transport !== previous.transport ||
      next.profile !== previous.profile ||
      (next.profile === 'live' && next.latency !== previous.latency);
    const mowerId = this.mowerId;
    if (rejoin && mowerId && this.phase !== 'idle' && this.phase !== 'closed') {
      await this.open(mowerId);
    } else {
      await this.player?.applyOptions?.(next);
    }
  }

  /** Poll statistics every 2 s while the Diagnose line is open. */
  watchDiagnostics(watched: boolean): void {
    this.diagWatched = watched;
    if (!watched) this.diagnostics = null;
    this.pollDiagnostics();
  }

  /** Leave the channel and stop renewing. */
  async close(): Promise<void> {
    this.generation += 1;
    this.suspended = false;
    await this.release();
    this.phase = 'closed';
  }

  private async play(session: CameraSession, generation: number): Promise<void> {
    if (session.provider === 'agora') {
      if (!this.deps.secure()) return this.fail(new CameraError('insecure'));
      if (!(await this.deps.cameraEnabled())) return this.fail(new CameraError('disabled'));
      if (generation !== this.generation) return;
    }
    this.session = session;
    this.transport ??= session.provider === 'relay' ? 'relay' : 'direct';
    this.phase = 'connecting';
    const events: PlayerEvents = {
      onLive: () => {
        if (generation !== this.generation) return;
        this.phase = 'live';
        this.attempts = 0;
      },
      onWaiting: () => {
        if (generation === this.generation) this.phase = 'waiting';
      },
      onTokenExpiring: () => {
        if (generation === this.generation) void this.refresh();
      },
      onFailed: (kind) => {
        if (generation === this.generation) void this.release().then(() => this.setError(kind));
      },
      onConnectionLost: (kind) => {
        if (generation === this.generation) this.connectionLost(kind, generation);
      },
      onPublishers: (uids) => {
        if (generation === this.generation) this.publishing = uids;
      },
      onActive: (uid) => {
        if (generation === this.generation) this.activeUid = uid;
      },
      onRenewed: (next) => {
        if (generation !== this.generation) return;
        this.session = next;
        this.renewals += 1;
      },
      onTracks: (uids) => {
        if (generation !== this.generation) return;
        this.live = uids;
        if (this.stalled.some((uid) => uids.includes(uid))) {
          this.stalled = this.stalled.filter((uid) => !uids.includes(uid));
        }
      },
    };
    try {
      const player = await this.deps.createPlayer(session, events);
      if (generation !== this.generation) {
        await player.stop();
        return;
      }
      this.player = player;
      player.setMuted(this.muted);
      const choice = this.choice();
      this.armNoData(choice);
      await player.start(session, choice, this.options);
    } catch (error) {
      if (generation !== this.generation) return;
      await this.release(false);
      this.fail(error);
      return;
    }
    if (generation === this.generation) {
      this.schedule(session);
      this.pollDiagnostics();
    }
  }

  /**
   * One reading now and the next one in 2 s (a single timer chain), while the player can
   * measure: the per-tile numbers always, the full snapshot while watched.
   */
  private pollDiagnostics(): void {
    if (this.diagTimer !== null) this.clearTimer(this.diagTimer);
    this.diagTimer = null;
    if (!this.player?.diagnostics) return;
    const snapshot = this.player.diagnostics() ?? null;
    this.tracks = snapshot?.tracks ?? [];
    if (this.diagWatched) this.diagnostics = snapshot;
    this.diagTimer = this.setTimer(() => this.pollDiagnostics(), DIAGNOSTICS_INTERVAL_MS);
  }

  /**
   * The connection dropped or stayed `RECONNECTING` too long: join again with a fresh session
   * (a new token), `disconnected` after the backoff, `stuck` at once.
   */
  private connectionLost(kind: 'disconnected' | 'stuck', generation: number): void {
    if (this.rejoinTimer !== null || this.rejoining) return;
    if (this.attempts >= MAX_REJOIN_ATTEMPTS) {
      void this.release().then(() => this.setError('failed'));
      return;
    }
    const delay = kind === 'stuck' ? 0 : rejoinDelayMs(this.attempts);
    this.attempts += 1;
    this.phase = 'connecting';
    this.rejoinTimer = this.setTimer(() => {
      this.rejoinTimer = null;
      void this.rejoin(generation);
    }, delay);
  }

  private async rejoin(generation: number): Promise<void> {
    const mowerId = this.mowerId;
    if (!mowerId || generation !== this.generation) return;
    let session: CameraSession;
    this.rejoining = true;
    try {
      session = await this.deps.request(mowerId, this.wantedTransport());
    } catch (error) {
      this.rejoining = false;
      if (generation !== this.generation) return;
      // The network may be down: try again with the next delay, give up after the last attempt.
      if (this.attempts >= MAX_REJOIN_ATTEMPTS) return this.fail(error);
      this.connectionLost('disconnected', generation);
      return;
    }
    try {
      if (generation !== this.generation) return;
      this.rejoins += 1;
      await this.release(false);
      if (generation !== this.generation) return;
      await this.play(session, generation);
    } finally {
      this.rejoining = false;
    }
  }

  private schedule(session: CameraSession): void {
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
    // A relay lease is renewed by its player's heartbeat (a fresh ticket every 15 s).
    if (isRelay(session)) return;
    this.timer = this.setTimer(
      () => {
        this.timer = null;
        void this.refresh();
      },
      renewDelayMs(session, this.now()),
    );
  }

  private async release(clearSession = true): Promise<void> {
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
    if (this.rejoinTimer !== null) this.clearTimer(this.rejoinTimer);
    this.rejoinTimer = null;
    if (this.diagTimer !== null) this.clearTimer(this.diagTimer);
    this.diagTimer = null;
    if (this.noDataTimer !== null) this.clearTimer(this.noDataTimer);
    this.noDataTimer = null;
    const player = this.player;
    this.player = null;
    if (clearSession) this.session = null;
    this.publishing = null;
    this.activeUid = null;
    this.live = [];
    this.stalled = [];
    this.tracks = [];
    if (player) await player.stop().catch(() => undefined);
  }

  private fail(error: unknown): void {
    if (error instanceof CameraError) return this.setError(error.kind);
    const problem = asProblem(error);
    this.correlationId = problem.correlationId;
    this.setError(cameraErrorOf(problem));
  }

  private setError(kind: CameraErrorKind): void {
    this.error = kind;
    this.phase = 'error';
  }
}
