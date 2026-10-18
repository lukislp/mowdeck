/**
 * The player of `relay` sessions (ADR 0045): MowDeck's camera relay serves every camera of the
 * mower over WebRTC with WHEP, through the same origin (`/media/whep/...`), so no vendor SDK and
 * no vendor cloud is involved in the browser.
 *
 * * One receive-only `RTCPeerConnection` (video only) per tile: both front cameras side by side
 *   are two connections. The offer is not trickled: the player waits until ICE gathering is
 *   complete (at most {@link GATHER_TIMEOUT_MS}), then POSTs it to the tile's `whep_url` with the
 *   lease's ticket as bearer token (`Content-Type: application/sdp`) and applies the answer. The
 *   `Location` of the answer names the WHEP session; it is deleted when the tile closes.
 *   MediaMTX answers it relative to its own root (`/r/<id>/<camera>/whep/<secret>`) while the
 *   gateway serves it below `/media/whep`: {@link resolveLocation} puts the prefix back.
 * * Starting and recovering (live incident 2026-10-18): a source that is still starting answers
 *   404 (nothing published on its path yet; the tile reports it as starting), the media plane
 *   that is away for a moment answers 5xx or not at all. The tile offers again with a backoff
 *   ({@link whepRetryDelayMs}: 0.5 s growing to 3 s) for as long as the lease lives, and gives
 *   up only after {@link TILE_GIVE_UP_MS} without a picture. Getting from a new lease to a
 *   playable H.264 path takes 6 to 8 s on a LUBA (Agora join, publisher, key frame,
 *   transcoder) and up to about 35 s after a 4G stall; every accepted offer keeps the relay's
 *   unconfirmed lease alive, so the lease never lapses while the tile is still trying.
 * * Live: a tile is live at its first presented frame (`requestVideoFrameCallback` through the
 *   freeze watch's frame counter, `freeze.ts`); the viewer's freeze detection and the drive
 *   view's hold read the same counter afterwards. The relay never invents frames, so a frozen
 *   source is a frozen picture here.
 * * Lost tile: ICE `failed`, `disconnected` for longer than {@link DISCONNECTED_LIMIT_MS}, or a
 *   connection the media server closed (its muxer restarted after a stall, MediaMTX restarted)
 *   reconnects that tile alone with a new WHEP session on the same lease (same backoff). Only a
 *   lease that is gone (heartbeat 404) or an offer the relay refuses (401/403, or another 4xx
 *   such as a browser without H.264) is a lost connection: the viewer joins again with a fresh
 *   session (new lease, its backoff). Without automatic rejoin any loss fails the stream.
 * * Gave up: a tile without a picture for {@link TILE_GIVE_UP_MS} stops trying (its tile says
 *   it delivers nothing); when no tile plays and every tile gave up the stream fails as
 *   `unavailable` ("camera not reachable", with a retry).
 * * Lease: a heartbeat every `heartbeat_seconds` (Fleet's renew endpoint) keeps the lease and
 *   brings a fresh ticket; a lease that is gone (404) is a lost connection. `stop()` deletes the
 *   WHEP sessions and detaches the lease (Fleet's DELETE, `keepalive` so it survives a page
 *   that is going away). A lease without confirmed media closes after 30 s on the relay, so
 *   every reconnect requests a new session.
 * * Cameras: only a camera known to deliver is a publisher the picker offers: one the relay
 *   reports `available: true`, one a tile plays (live), or one a tile was asked for by the
 *   viewer's choice (it must not look missing while it starts). A camera whose availability is
 *   not known yet (`null`, the first seconds of a session) or that the relay reports
 *   `available: false` (listed by the vendor, never published) is left out, so a camera the
 *   mower does not have never flashes up; every renewal and every live tile updates the list.
 * * Diagnostics: `getStats()` of every tile's receiver (bitrate, loss, frame rate, size,
 *   freezes, jitter buffer, round trip) as the panel's snapshot.
 *
 * The ticket and the WHEP session URLs are secrets: never logged.
 */
import { asProblem } from '$lib/api/problem';

import {
  buildSnapshot,
  EventLog,
  summarizeInbound,
  type DiagnosticsSnapshot,
  type RemoteStatsLike,
} from './diagnostics';
import { frameCounter } from './freeze';
import { attachTracks, detachTracks } from './media';
import { DEFAULT_OPTIONS, type CameraOptions } from './options';
import { slotsOf, type CameraChoice, type CameraPlayer, type PlayerEvents } from './player';
import { CameraError, type CameraSession } from './session';

/** Wait at most this long for ICE gathering before the offer goes out as it is. */
export const GATHER_TIMEOUT_MS = 1_500;
/** ICE `disconnected` for longer than this is a lost connection. */
export const DISCONNECTED_LIMIT_MS = 5_000;
/** First delay before a WHEP offer goes out again (404 starting, 5xx or no answer). */
export const WHEP_RETRY_MS = 500;
/** The retry delay grows by this factor per attempt ... */
export const WHEP_RETRY_FACTOR = 1.5;
/** ... up to this delay. */
export const WHEP_RETRY_MAX_MS = 3_000;
/**
 * A tile that has shown no picture for this long while it keeps trying gives up (a camera that
 * does not come back; a starting LUBA camera takes 6 to 8 s, after a 4G stall up to about 35 s).
 */
export const TILE_GIVE_UP_MS = 90_000;

/** Delay before retry number `retry` (0-based) of a tile's offer. */
export function whepRetryDelayMs(retry: number): number {
  const delay = WHEP_RETRY_MS * WHEP_RETRY_FACTOR ** Math.max(0, retry);
  return Math.round(Math.min(WHEP_RETRY_MAX_MS, delay));
}
/** How often a tile is checked for its first presented frame. */
export const FIRST_FRAME_POLL_MS = 100;
/** Heartbeat interval when the session names none. */
export const DEFAULT_HEARTBEAT_SECONDS = 15;

/** Fleet's lease endpoints for this mower (`.../camera/session/{lease_id}`). */
export interface RelayLeaseApi {
  /** `POST .../{lease_id}/renew`: the same lease with a fresh ticket (rejects with a problem). */
  renew(leaseId: string): Promise<CameraSession>;
  /** `DELETE .../{lease_id}` (idempotent, sent with `keepalive`). */
  detach(leaseId: string): Promise<void>;
}

/** The parts of `RTCPeerConnection` the player uses (a fake in tests). */
export type PeerConnection = Pick<
  RTCPeerConnection,
  | 'addTransceiver'
  | 'createOffer'
  | 'setLocalDescription'
  | 'setRemoteDescription'
  | 'getStats'
  | 'getReceivers'
  | 'close'
  | 'addEventListener'
  | 'iceGatheringState'
  | 'iceConnectionState'
  | 'connectionState'
  | 'localDescription'
>;

export interface WhepDeps {
  lease: RelayLeaseApi;
  /** The WHEP requests (the ticket is their bearer token, never the user's token). */
  fetch?: typeof fetch;
  createPeer?: () => PeerConnection;
  /** The page's origin (relative WHEP URLs). */
  origin?: () => string;
  /** The frame counter of a tile's element (default: `freeze.ts`). */
  frames?: (video: HTMLVideoElement) => number | null;
  /** Wraps the received track for the element (jsdom has no `MediaStream`). */
  stream?: (tracks: MediaStreamTrack[]) => MediaStream;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  gatherTimeoutMs?: number;
}

/** A tile's video element. */
export type WhepVideoTarget = (slot: number) => HTMLVideoElement | null;

/**
 * The URL of the WHEP session from the answer's `Location`, against the endpoint it was POSTed
 * to. An absolute URL stays; a root-relative one that repeats the end of the endpoint's path
 * (MediaMTX behind a path prefix: endpoint `/media/whep/r/a/cam/whep`, Location
 * `/r/a/cam/whep/s`) gets the endpoint's prefix back (`/media/whep/r/a/cam/whep/s`); anything
 * else resolves like a link.
 */
export function resolveLocation(location: string, endpoint: string, origin: string): string {
  const base = new URL(endpoint, origin);
  if (/^[a-z][a-z0-9+.-]*:/i.test(location)) return new URL(location).toString();
  if (location.startsWith('/') && !location.startsWith('//')) {
    const path = base.pathname;
    for (let at = 0; at < path.length; at += 1) {
      if (path[at] !== '/') continue;
      const tail = path.slice(at);
      if (location === tail || location.startsWith(tail.endsWith('/') ? tail : `${tail}/`)) {
        return new URL(path.slice(0, at) + location, base).toString();
      }
    }
  }
  return new URL(location, base).toString();
}

/** One tile: the camera it asks for and its connection. */
interface Tile {
  wanted: number | null;
  /** The camera uid it plays (null: none of the session's cameras). */
  uid: number | null;
  cameraId: string | null;
  /** The camera's WHEP endpoint (null: the relay does not serve it). */
  url: string | null;
  pc: PeerConnection | null;
  /** The WHEP session to delete (secret, never shown). */
  location: string | null;
  live: boolean;
  /** The last offer was answered 404: the camera's source has nothing published yet. */
  starting: boolean;
  /** Since when the tile tries without a picture (null: playing, or not trying). */
  since: number | null;
  /** Offers retried since the tile last played (the backoff). */
  retries: number;
  retryTimer: unknown;
  /** No picture for {@link TILE_GIVE_UP_MS}: the tile stopped trying. */
  gaveUp: boolean;
  /** The element's frame count when the tile lost its connection (live again only above). */
  framesAtLoss: number;
  firstFrameTimer: unknown;
  lostTimer: unknown;
  state: string;
  /** Previous stats reading (bitrate and loss deltas). */
  last: { at: number; bytes: number; received: number; lost: number } | null;
  remote: RemoteStatsLike | undefined;
  rttMs: number | null;
}

const emptyTile = (wanted: number | null): Tile => ({
  wanted,
  uid: null,
  cameraId: null,
  url: null,
  pc: null,
  location: null,
  live: false,
  starting: false,
  since: null,
  retries: 0,
  retryTimer: null,
  gaveUp: false,
  framesAtLoss: 0,
  firstFrameTimer: null,
  lostTimer: null,
  state: 'new',
  last: null,
  remote: undefined,
  rttMs: null,
});

/** Create the WHEP player on our tiles' elements. */
export function createWhepPlayer(
  video: WhepVideoTarget,
  events: PlayerEvents,
  deps: WhepDeps,
): CameraPlayer {
  return new WhepPlayer(video, events, deps);
}

export class WhepPlayer implements CameraPlayer {
  private session: CameraSession | null = null;
  private ticket = '';
  private tiles: Tile[] = [emptyTile(null)];
  private options: CameraOptions = DEFAULT_OPTIONS;
  private log = new EventLog();
  private stopped = false;
  private muted = true;
  private heartbeat: unknown = null;
  private anyLive = false;
  private lastTracks = '';
  private lastActive: number | null | undefined = undefined;
  private lostReported = false;
  private lastPublishers: string | null = null;
  private lastStarting = '';
  private readonly fetcher: typeof fetch;
  private readonly createPeer: () => PeerConnection;
  private readonly origin: () => string;
  private readonly frames: (video: HTMLVideoElement) => number | null;
  private readonly now: () => number;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (handle: unknown) => void;

  constructor(
    private readonly video: WhepVideoTarget,
    private readonly events: PlayerEvents,
    private readonly deps: WhepDeps,
  ) {
    this.fetcher = deps.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.createPeer =
      deps.createPeer ?? (() => new RTCPeerConnection({ bundlePolicy: 'max-bundle' }));
    this.origin = deps.origin ?? (() => globalThis.location?.origin ?? 'http://localhost');
    this.frames = deps.frames ?? ((element) => frameCounter(element));
    this.now = deps.now ?? (() => Date.now());
    this.setTimer = deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer =
      deps.clearTimer ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  }

  async start(
    session: CameraSession,
    choice: CameraChoice,
    options: CameraOptions = DEFAULT_OPTIONS,
  ): Promise<void> {
    const relay = session.relay;
    if (session.provider !== 'relay' || !relay)
      throw new CameraError('failed', 'not a relay session');
    if (!this.deps.createPeer && typeof RTCPeerConnection === 'undefined') {
      throw new CameraError('browser', 'no WebRTC in this browser');
    }
    this.session = session;
    this.ticket = relay.ticket;
    this.options = options;
    this.log = new EventLog(this.now);
    this.stopped = false;
    this.lastPublishers = null;
    this.lastStarting = '';
    this.tiles = slotsOf(choice).map((wanted) => this.bind(emptyTile(wanted)));
    this.scheduleHeartbeat();
    this.announce();
    await Promise.all(this.tiles.map((tile, slot) => this.connect(tile, slot)));
  }

  async renew(session: CameraSession): Promise<void> {
    if (session.relay && session.relay.lease_id === this.session?.relay?.lease_id) {
      this.adopt(session);
      return;
    }
    throw new CameraError('failed', 'another lease needs a new player');
  }

  async select(choice: CameraChoice): Promise<void> {
    const wanted = slotsOf(choice);
    const previous = this.tiles;
    const next = wanted.map((uid, slot) => {
      const current = previous[slot];
      const fresh = this.bind(emptyTile(uid));
      // The same camera in the same tile (e.g. "first camera" becoming its uid): keep playing.
      if (current && current.cameraId !== null && current.cameraId === fresh.cameraId) {
        current.wanted = uid;
        return current;
      }
      return fresh;
    });
    const dropped = previous.filter((tile) => !next.includes(tile));
    for (const [slot, tile] of previous.entries()) {
      if (next[slot] !== tile) {
        const element = this.video(slot);
        if (element) detachTracks(element);
      }
    }
    this.tiles = next;
    await Promise.all(dropped.map((tile) => this.closeTile(tile)));
    this.announce();
    await Promise.all(
      next.map((tile, slot) =>
        tile.pc || tile.retryTimer !== null || this.stopped ? undefined : this.connect(tile, slot),
      ),
    );
  }

  setMuted(muted: boolean): void {
    // The relay serves video only; the first element keeps the asked state for later sources.
    this.muted = muted;
    const first = this.video(0);
    if (first) first.muted = muted;
  }

  async applyOptions(options: CameraOptions): Promise<void> {
    this.options = options;
  }

  async stop(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    if (this.heartbeat !== null) this.clearTimer(this.heartbeat);
    this.heartbeat = null;
    const tiles = this.tiles;
    this.tiles = tiles.map((tile) => emptyTile(tile.wanted));
    for (let slot = 0; slot < Math.max(tiles.length, 2); slot += 1) {
      const element = this.video(slot);
      if (element) detachTracks(element);
    }
    const leaseId = this.session?.relay?.lease_id;
    await Promise.all([
      ...tiles.map((tile) => this.closeTile(tile)),
      leaseId ? this.deps.lease.detach(leaseId).catch(() => undefined) : undefined,
    ]);
  }

  diagnostics(): DiagnosticsSnapshot | null {
    if (!this.session || this.stopped) return null;
    void this.readStats();
    const first = this.tiles[0];
    const shown = this.tiles.filter((tile) => tile.uid !== null && tile.live);
    return buildSnapshot({
      state: first?.state ?? 'new',
      uid: first?.uid ?? null,
      remote: first?.remote,
      rtc: first?.rttMs !== null && first?.rttMs !== undefined ? { RTT: first.rttMs } : undefined,
      log: this.log,
      tracks: shown.map((tile) => ({ uid: tile.uid as number, remote: tile.remote })),
    });
  }

  // --- Cameras --------------------------------------------------------------------------------

  /**
   * The uids of the cameras known to deliver: reported `available: true` by the relay, played
   * by a live tile, or asked for by a tile of the viewer's choice. Unknown (`null`) and
   * `available: false` cameras are left out (the vendor lists cameras the mower may not have).
   */
  private servedUids(): number[] {
    const endpoints = this.session?.relay?.tiles ?? [];
    const shown = new Set(
      this.tiles.filter((tile) => tile.live || tile.wanted !== null).map((tile) => tile.uid),
    );
    return (this.session?.cameras ?? [])
      .filter((camera) => {
        const tile = endpoints.find((endpoint) => endpoint.camera_id === camera.id);
        if (!tile || tile.available === false) return false;
        return tile.available === true || shown.has(camera.uid ?? null);
      })
      .map((camera) => camera.uid)
      .filter((uid): uid is number => uid !== null && uid !== undefined);
  }

  /** Report the publishers when they changed. */
  private announcePublishers(): void {
    const uids = this.servedUids();
    const key = uids.join(',');
    if (key === this.lastPublishers) return;
    this.lastPublishers = key;
    this.events.onPublishers?.(uids);
  }

  /** The camera a tile plays: its uid's, or (without a choice) the first one served. */
  private cameraFor(
    wanted: number | null,
  ): { uid: number | null; cameraId: string; url: string } | null {
    const session = this.session;
    const endpoints = session?.relay?.tiles ?? [];
    if (!session || !endpoints.length) return null;
    const cameras = session.cameras ?? [];
    if (wanted === null) {
      // The first camera known to deliver, else the first not known silent (the relay lists
      // the tiles front first), else the first.
      const endpoint =
        endpoints.find((e) => e.available === true) ??
        endpoints.find((e) => e.available !== false) ??
        endpoints[0]!;
      const camera = cameras.find((c) => c.id === endpoint.camera_id);
      return { uid: camera?.uid ?? null, cameraId: endpoint.camera_id, url: endpoint.whep_url };
    }
    const camera = cameras.find((c) => c.uid === wanted);
    const endpoint = camera ? endpoints.find((e) => e.camera_id === camera.id) : undefined;
    return endpoint ? { uid: wanted, cameraId: endpoint.camera_id, url: endpoint.whep_url } : null;
  }

  // --- WHEP -----------------------------------------------------------------------------------

  /** The tile's camera and endpoint from the session. */
  private bind(tile: Tile): Tile {
    const target = this.cameraFor(tile.wanted);
    tile.uid = target?.uid ?? null;
    tile.cameraId = target?.cameraId ?? null;
    tile.url = target?.url ?? null;
    return tile;
  }

  /**
   * Open the WHEP session of one tile. Resolves after the first answer; a source that is still
   * starting (404) or a media plane that is away (5xx, no answer) gets the same offer again
   * later, a refused offer is a lost connection.
   */
  private async connect(tile: Tile, slot: number): Promise<void> {
    const url = tile.url;
    if (!url || this.stopped) return; // a camera the relay does not serve: the tile says so
    tile.since ??= this.now();
    tile.gaveUp = false;
    const pc = this.createPeer();
    tile.pc = pc;
    tile.state = 'connecting';
    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.addEventListener('track', (event) => {
      if (tile.pc !== pc) return;
      const track = (event as RTCTrackEvent).track;
      void this.show(tile, slot, track);
    });
    const onState = (): void => this.watchConnection(tile, slot, pc);
    pc.addEventListener('iceconnectionstatechange', onState);
    pc.addEventListener('connectionstatechange', onState);
    let sdp: string;
    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await this.gathered(pc);
      sdp = pc.localDescription?.sdp ?? offer.sdp ?? '';
    } catch {
      if (tile.pc !== pc || this.stopped) return;
      this.tileLost(tile, slot, 'offer failed');
      return;
    }
    if (tile.pc !== pc) return;
    await this.offer(tile, slot, pc, url, sdp);
  }

  /** POST the tile's offer once; schedules the next one when the answer says "later". */
  private async offer(
    tile: Tile,
    slot: number,
    pc: PeerConnection,
    url: string,
    sdp: string,
  ): Promise<void> {
    let response: Response | null;
    try {
      response = await this.fetcher(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp', Authorization: `Bearer ${this.ticket}` },
        body: sdp,
      });
    } catch {
      response = null; // offline for a moment, or the gateway is restarting
    }
    if (tile.pc !== pc || this.stopped) return;
    const status = response?.status ?? 0;
    this.log.add('state', `tile ${slot} whep ${status}`);
    if (response && status === 201) {
      const location = response.headers.get('Location');
      const resolved = location ? resolveLocation(location, url, this.origin()) : null;
      let answer: string;
      try {
        answer = await response.text();
      } catch {
        await this.deleteSession(resolved);
        if (tile.pc === pc && !this.stopped) this.retryOffer(tile, slot, pc, url, sdp);
        return;
      }
      if (tile.pc !== pc || this.stopped) {
        await this.deleteSession(resolved);
        return;
      }
      tile.location = resolved;
      this.setStarting(tile, false);
      try {
        await pc.setRemoteDescription({ type: 'answer', sdp: answer });
      } catch {
        if (tile.pc === pc && !this.stopped) this.tileLost(tile, slot, 'answer refused');
      }
      return;
    }
    if (status >= 400 && status < 500 && status !== 404 && status !== 408 && status !== 429) {
      // Refused: the ticket or its lease (401/403), or the offer itself. A new session.
      tile.state = 'failed';
      this.log.add('state', `tile ${slot} offer refused`);
      this.lost();
      return;
    }
    // 404: nothing published yet (the source starts, or its muxer restarts after a stall);
    // 5xx, 408, 429 or no answer: the media plane is away for a moment. Offer again later.
    this.setStarting(tile, status === 404 && !this.knownSilent(tile));
    this.retryOffer(tile, slot, pc, url, sdp);
  }

  private retryOffer(tile: Tile, slot: number, pc: PeerConnection, url: string, sdp: string): void {
    this.retryLater(tile, slot, () => {
      if (tile.pc === pc) void this.offer(tile, slot, pc, url, sdp);
    });
  }

  /** Run `again` after the tile's next backoff delay, or give up after {@link TILE_GIVE_UP_MS}. */
  private retryLater(tile: Tile, slot: number, again: () => void): void {
    if (tile.retryTimer !== null) this.clearTimer(tile.retryTimer);
    tile.retryTimer = null;
    if (tile.since !== null && this.now() - tile.since >= TILE_GIVE_UP_MS) {
      void this.giveUp(tile, slot);
      return;
    }
    const delay = whepRetryDelayMs(tile.retries);
    tile.retries += 1;
    tile.retryTimer = this.setTimer(() => {
      tile.retryTimer = null;
      if (this.stopped || !this.tiles.includes(tile)) return;
      again();
    }, delay);
  }

  /** The relay reports the tile's camera as one that never publishes. */
  private knownSilent(tile: Tile): boolean {
    const endpoint = this.session?.relay?.tiles.find((e) => e.camera_id === tile.cameraId);
    return endpoint?.available === false;
  }

  private setStarting(tile: Tile, starting: boolean): void {
    if (tile.starting === starting) return;
    tile.starting = starting;
    this.announce();
  }

  /**
   * A tile lost its connection (or could not build an offer): reconnect it alone on the same
   * lease after its backoff. Without automatic rejoin the stream fails instead.
   */
  private tileLost(tile: Tile, slot: number, reason: string): void {
    if (this.stopped || !this.tiles.includes(tile)) return;
    this.log.add('state', `tile ${slot} ${reason}`);
    if (!this.options.autoRejoin) {
      tile.state = 'failed';
      this.lost();
      return;
    }
    const wasLive = tile.live;
    const element = this.video(slot);
    tile.framesAtLoss = (element ? this.frames(element) : null) ?? 0;
    void this.closeTile(tile);
    tile.state = 'reconnecting';
    if (wasLive || tile.since === null) {
      tile.since = this.now();
      tile.retries = 0;
    }
    this.announce();
    this.retryLater(tile, slot, () => {
      if (tile.pc === null) void this.connect(tile, slot);
    });
  }

  /** No picture for {@link TILE_GIVE_UP_MS}: stop trying; no tile left: the camera is unreachable. */
  private async giveUp(tile: Tile, slot: number): Promise<void> {
    this.log.add('state', `tile ${slot} gave up`);
    await this.closeTile(tile);
    tile.gaveUp = true;
    tile.starting = false;
    tile.since = null;
    tile.state = 'failed';
    this.announce();
    if (this.stopped) return;
    const trying = this.tiles.some((t) => t.live || (t.url !== null && !t.gaveUp));
    if (!trying && !this.lostReported) {
      this.lostReported = true;
      this.events.onFailed('unavailable');
    }
  }

  /** ICE gathering complete, or the timeout (the candidates so far go out). */
  private gathered(pc: PeerConnection): Promise<void> {
    if (pc.iceGatheringState === 'complete') return Promise.resolve();
    return new Promise<void>((resolve) => {
      let done = false;
      const finish = (): void => {
        if (done) return;
        done = true;
        this.clearTimer(timer);
        resolve();
      };
      const timer = this.setTimer(finish, this.deps.gatherTimeoutMs ?? GATHER_TIMEOUT_MS);
      pc.addEventListener('icegatheringstatechange', () => {
        if (pc.iceGatheringState === 'complete') finish();
      });
    });
  }

  /** The received track goes into the tile's element; the tile is live at its first frame. */
  private async show(tile: Tile, slot: number, track: MediaStreamTrack): Promise<void> {
    const element = this.video(slot);
    if (!element) return;
    await attachTracks(
      element,
      [{ getMediaStreamTrack: () => track }],
      slot === 0 ? this.muted : true,
      this.deps.stream,
    );
    this.waitForFrame(tile, element);
  }

  private waitForFrame(tile: Tile, element: HTMLVideoElement): void {
    if (tile.firstFrameTimer !== null) this.clearTimer(tile.firstFrameTimer);
    const check = (): void => {
      tile.firstFrameTimer = null;
      if (this.stopped || !this.tiles.includes(tile) || tile.live) return;
      const frames = this.frames(element);
      // A reconnected tile is live at the first frame of its new connection, not at the count
      // its element kept from the old one.
      if (frames !== null && frames > tile.framesAtLoss) {
        tile.live = true;
        tile.starting = false;
        tile.since = null;
        tile.retries = 0;
        this.lostReported = false;
        this.announce();
        return;
      }
      tile.firstFrameTimer = this.setTimer(check, FIRST_FRAME_POLL_MS);
    };
    check();
  }

  /**
   * ICE `failed` or a connection the other side closed at once, `disconnected` after
   * {@link DISCONNECTED_LIMIT_MS}: the tile reconnects (a connection the player closed itself
   * is no longer the tile's and never counts).
   */
  private watchConnection(tile: Tile, slot: number, pc: PeerConnection): void {
    if (tile.pc !== pc || this.stopped) return;
    const ice = pc.iceConnectionState;
    const state =
      pc.connectionState === 'failed' || ice === 'failed'
        ? 'failed'
        : pc.connectionState === 'closed' || ice === 'closed'
          ? 'closed'
          : ice;
    if (state === tile.state) return;
    this.log.add('state', `${tile.state} > ${state}`);
    tile.state = state;
    if (tile.lostTimer !== null) this.clearTimer(tile.lostTimer);
    tile.lostTimer = null;
    if (state === 'failed' || state === 'closed') {
      this.tileLost(tile, slot, `connection ${state}`);
    } else if (state === 'disconnected') {
      tile.lostTimer = this.setTimer(() => {
        tile.lostTimer = null;
        if (tile.pc === pc && tile.state === 'disconnected') {
          this.tileLost(tile, slot, 'connection disconnected');
        }
      }, DISCONNECTED_LIMIT_MS);
    }
  }

  /** Report a lost connection once (the viewer joins again with a fresh session). */
  private lost(): void {
    if (this.stopped || this.lostReported) return;
    this.lostReported = true;
    if (this.options.autoRejoin && this.events.onConnectionLost) {
      this.events.onConnectionLost('disconnected');
    } else {
      this.events.onFailed('failed');
    }
  }

  private async closeTile(tile: Tile): Promise<void> {
    if (tile.firstFrameTimer !== null) this.clearTimer(tile.firstFrameTimer);
    if (tile.lostTimer !== null) this.clearTimer(tile.lostTimer);
    if (tile.retryTimer !== null) this.clearTimer(tile.retryTimer);
    tile.firstFrameTimer = null;
    tile.lostTimer = null;
    tile.retryTimer = null;
    const pc = tile.pc;
    const location = tile.location;
    tile.pc = null;
    tile.location = null;
    tile.live = false;
    try {
      pc?.close();
    } catch {
      // already closed
    }
    await this.deleteSession(location);
  }

  /** DELETE the WHEP session (best effort; the relay notices a gone reader anyway). */
  private async deleteSession(location: string | null): Promise<void> {
    if (!location) return;
    try {
      await this.fetcher(location, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${this.ticket}` },
        keepalive: true,
      });
    } catch {
      // the page is going away or the network is down
    }
  }

  /** Tell the viewer the first tile's camera, the live cameras and live or not (on changes). */
  private announce(): void {
    this.announcePublishers();
    const starting = this.tiles
      .filter((tile) => tile.starting && !tile.live && tile.uid !== null)
      .map((tile) => tile.uid as number);
    const startingKey = starting.join(',');
    if (startingKey !== this.lastStarting) {
      this.lastStarting = startingKey;
      this.events.onStarting?.(starting);
    }
    const first = this.tiles[0]?.uid ?? null;
    if (first !== this.lastActive) {
      this.lastActive = first;
      this.events.onActive?.(first);
    }
    const live = this.tiles
      .filter((tile) => tile.live && tile.uid !== null)
      .map((tile) => tile.uid as number);
    const key = live.join(',');
    if (key !== this.lastTracks) {
      this.lastTracks = key;
      this.events.onTracks?.(live);
    }
    const any = this.tiles.some((tile) => tile.live);
    if (any && !this.anyLive) this.events.onLive();
    this.anyLive = any;
  }

  // --- Lease ----------------------------------------------------------------------------------

  private scheduleHeartbeat(): void {
    if (this.heartbeat !== null) this.clearTimer(this.heartbeat);
    const seconds = this.session?.relay?.heartbeat_seconds ?? DEFAULT_HEARTBEAT_SECONDS;
    this.heartbeat = this.setTimer(
      () => {
        this.heartbeat = null;
        void this.beat();
      },
      Math.max(1, seconds) * 1000,
    );
  }

  /** One heartbeat: a fresh ticket; a lease that is gone is a lost connection. */
  private async beat(): Promise<void> {
    const leaseId = this.session?.relay?.lease_id;
    if (!leaseId || this.stopped) return;
    try {
      const next = await this.deps.lease.renew(leaseId);
      if (this.stopped) return;
      this.adopt(next);
    } catch (error) {
      if (this.stopped) return;
      if (asProblem(error).status === 404) {
        this.log.add('state', 'lease gone');
        this.lost();
        return;
      }
    }
    this.scheduleHeartbeat();
  }

  /** A renewed session of the same lease: its ticket for the next WHEP requests. */
  private adopt(next: CameraSession): void {
    if (!next.relay || next.relay.lease_id !== this.session?.relay?.lease_id) return;
    this.session = next;
    this.ticket = next.relay.ticket;
    this.events.onRenewed?.(next);
    this.announcePublishers(); // the relay now knows which cameras deliver
  }

  // --- Statistics -----------------------------------------------------------------------------

  /** Read every tile's receiver statistics (bitrate and loss from the previous reading). */
  private async readStats(): Promise<void> {
    await Promise.all(
      this.tiles.map(async (tile) => {
        const pc = tile.pc;
        if (!pc) return;
        let entries: Record<string, unknown>[];
        try {
          const report = await pc.getStats();
          entries = [...(report.values() as Iterable<Record<string, unknown>>)];
        } catch {
          return;
        }
        const inbound = summarizeInbound(entries);
        const pair = entries.find(
          (e) =>
            e.type === 'candidate-pair' &&
            (e.nominated === true || e.selected === true) &&
            e.state === 'succeeded',
        );
        const rtt =
          typeof pair?.currentRoundTripTime === 'number' ? pair.currentRoundTripTime : null;
        tile.rttMs = rtt === null ? null : Math.round(rtt * 1000);
        const raw = entries.find(
          (e) => e.type === 'inbound-rtp' && (e.kind === 'video' || e.mediaType === 'video'),
        );
        const at = this.now();
        const bytes = typeof raw?.bytesReceived === 'number' ? raw.bytesReceived : 0;
        const received = inbound?.packetsReceived ?? 0;
        const lost = inbound?.packetsLost ?? 0;
        const last = tile.last;
        let bitrate: number | undefined;
        let loss: number | undefined;
        if (last && at > last.at) {
          bitrate = ((bytes - last.bytes) * 8 * 1000) / (at - last.at);
          const dReceived = received - last.received;
          const dLost = lost - last.lost;
          loss = dReceived + dLost > 0 ? Math.max(0, dLost) / (dReceived + dLost) : 0;
        }
        tile.last = { at, bytes, received, lost };
        const total = received + lost;
        tile.remote = {
          receiveBitrate: bitrate,
          receiveFrameRate: inbound?.framesPerSecond ?? undefined,
          receiveResolutionWidth: inbound?.width ?? undefined,
          receiveResolutionHeight: inbound?.height ?? undefined,
          totalFreezeTime: inbound?.freezeSeconds ?? undefined,
          currentPacketLossRate: loss,
          packetLossRate: total > 0 ? lost / total : undefined,
          receiveDelay: inbound?.jitterMs ?? undefined,
        };
      }),
    );
  }
}
