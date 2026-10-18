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
 *   gateway serves it below `/media/whep`: {@link resolveLocation} puts the prefix back. A
 *   source that is still starting answers 404 (nothing published on its path yet): the same
 *   offer goes out again every {@link WHEP_RETRY_MS} for up to 20 s.
 * * Live: a tile is live at its first presented frame (`requestVideoFrameCallback` through the
 *   freeze watch's frame counter, `freeze.ts`); the viewer's freeze detection and the drive
 *   view's hold read the same counter afterwards. The relay never invents frames, so a frozen
 *   source is a frozen picture here.
 * * Lost: ICE `failed`, or `disconnected` for longer than {@link DISCONNECTED_LIMIT_MS}, is
 *   reported as a lost connection; the viewer joins again with a fresh session (new lease, its
 *   backoff). A WHEP POST that is refused fails the start the same way.
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
/**
 * A WHEP offer answered 404 (the source is starting and has not published yet) is sent again
 * after {@link WHEP_RETRY_MS}, at most this many times (20 s, well inside the relay's 30 s for
 * a lease without media).
 */
export const WHEP_START_ATTEMPTS = 40;
export const WHEP_RETRY_MS = 500;
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
      next.map((tile, slot) => (tile.pc || this.stopped ? undefined : this.connect(tile, slot))),
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

  /** Open the WHEP session of one tile; a refused or failed offer is a lost connection. */
  private async connect(tile: Tile, slot: number): Promise<void> {
    const url = tile.url;
    if (!url) return; // a camera the relay does not serve: the tile says so after a while
    const pc = this.createPeer();
    tile.pc = pc;
    tile.state = 'connecting';
    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.addEventListener('track', (event) => {
      if (tile.pc !== pc) return;
      const track = (event as RTCTrackEvent).track;
      void this.show(tile, slot, track);
    });
    const onState = (): void => this.watchConnection(tile, pc);
    pc.addEventListener('iceconnectionstatechange', onState);
    pc.addEventListener('connectionstatechange', onState);
    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await this.gathered(pc);
      if (tile.pc !== pc) return;
      const sdp = pc.localDescription?.sdp ?? offer.sdp ?? '';
      let response: Response;
      // A source that is still starting has no stream on its path yet (404): offer again.
      for (let attempt = 0; ; attempt += 1) {
        response = await this.fetcher(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/sdp', Authorization: `Bearer ${this.ticket}` },
          body: sdp,
        });
        this.log.add('state', `tile ${slot} whep ${response.status}`);
        if (response.status !== 404 || attempt + 1 >= WHEP_START_ATTEMPTS) break;
        await this.delay(WHEP_RETRY_MS);
        if (tile.pc !== pc || this.stopped) return;
      }
      if (response.status !== 201) throw new Error(`WHEP answered ${response.status}`);
      const location = response.headers.get('Location');
      if (location) tile.location = resolveLocation(location, url, this.origin());
      const answer = await response.text();
      if (tile.pc !== pc) {
        await this.deleteSession(tile.location);
        return;
      }
      await pc.setRemoteDescription({ type: 'answer', sdp: answer });
    } catch {
      if (tile.pc !== pc || this.stopped) return;
      tile.state = 'failed';
      this.log.add('state', `tile ${slot} offer failed`);
      this.lost();
    }
  }

  private delay(ms: number): Promise<void> {
    return new Promise((resolve) => this.setTimer(resolve, ms));
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
      if (frames !== null && frames > 0) {
        tile.live = true;
        this.lostReported = false;
        this.announce();
        return;
      }
      tile.firstFrameTimer = this.setTimer(check, FIRST_FRAME_POLL_MS);
    };
    check();
  }

  /** ICE `failed` at once, `disconnected` after {@link DISCONNECTED_LIMIT_MS}: connection lost. */
  private watchConnection(tile: Tile, pc: PeerConnection): void {
    if (tile.pc !== pc || this.stopped) return;
    const ice = pc.iceConnectionState;
    const state = pc.connectionState === 'failed' || ice === 'failed' ? 'failed' : ice;
    if (state === tile.state) return;
    this.log.add('state', `${tile.state} > ${state}`);
    tile.state = state;
    if (tile.lostTimer !== null) this.clearTimer(tile.lostTimer);
    tile.lostTimer = null;
    if (state === 'failed') {
      this.lost();
    } else if (state === 'disconnected') {
      tile.lostTimer = this.setTimer(() => {
        tile.lostTimer = null;
        if (tile.pc === pc && tile.state === 'disconnected') this.lost();
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
    tile.firstFrameTimer = null;
    tile.lostTimer = null;
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
