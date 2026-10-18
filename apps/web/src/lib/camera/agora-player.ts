/**
 * The player of `agora` sessions (ADR 0032): the browser joins the Mammotion camera channel
 * directly with the Agora Web SDK (`agora-rtc-sdk-ng`) and shows the remote tracks in our own
 * `<video>` elements. This module is only reached through a dynamic `import()`, so the SDK
 * (about 1.6 MB) is its own chunk and loads when a camera opens.
 *
 * * Channel profile from the viewer's options (`options.ts`): `rtc` (default) or `live` as
 *   audience with a latency level (`setClientRole('audience', { level })`). The viewer only
 *   subscribes, it never publishes.
 * * Stability: `setStreamFallbackOption(uid, 2)` (low stream, then audio only, on a poor
 *   network) and, as an opt-in data saver, `setRemoteVideoStreamType(uid, 1)`, for every
 *   subscribed camera. A dropped connection (`DISCONNECTED`, or `RECONNECTING` for more than
 *   20 s) is reported to the viewer, which joins again with a fresh session.
 * * Events (publish, unpublish, subscribe) run one after the other in a queue, so a quick
 *   unpublish/publish of the same uid cannot interleave two subscriptions.
 * * Diagnostics: `getRemoteVideoStats()` per shown camera, `getRTCStats()` and an event log
 *   (`diagnostics.ts`).
 * * Encryption: when the session has one, `setEncryptionConfig(mode, key, salt)` before the
 *   join; the salt arrives as base64 and is decoded to the 32 bytes the SDK expects.
 * * Token: `token-privilege-will-expire` (30 s before) asks the viewer for a fresh session,
 *   whose token goes to `client.renewToken(token)`.
 * * No `track.play()`: the SDK's own player applies inline styles, which the CSP refuses. The
 *   raw `MediaStreamTrack`s go into our elements (`media.ts`); sound is the first element's
 *   `muted`. The SDK's autoplay prompt (a styled overlay) is switched off for the same reason.
 *   One inline style remains that the SDK sets itself: every video track probes its resolution
 *   with a 1 px `<video style="position: fixed; ...">`; the CSP allows exactly that hash
 *   (see `nginx/40-mowdeck-runtime-config.sh`).
 * * Cameras: the mower publishes every camera under its own uid (slot n as uid n + 1, when the
 *   adapter asked for all cameras). Every tile shows exactly the camera it was asked for: both
 *   front cameras side by side are two subscriptions, each in its own element. A chosen camera
 *   that does not publish shows no picture (the panel says so) instead of another camera's
 *   picture; only a tile without a choice (the single view's default) plays the first publisher.
 * * Privacy: the SDK's log upload to Agora is switched off; statistics the SDK sends as part
 *   of the call itself still go to Agora (see the privacy note in docs/dev/web-app.md).
 */
import AgoraRTC, {
  type EncryptionMode,
  type IAgoraRTCClient,
  type IAgoraRTCRemoteUser,
  type IRemoteAudioTrack,
  type IRemoteVideoTrack,
} from 'agora-rtc-sdk-ng';

import { buildSnapshot, EventLog, type DiagnosticsSnapshot } from './diagnostics';
import type { VideoTarget } from './factory';
import { attachTracks, detachTracks, videoPublishers } from './media';
import { DEFAULT_OPTIONS, RECONNECTING_LIMIT_MS, type CameraOptions } from './options';
import { slotsOf, type CameraChoice, type CameraPlayer, type PlayerEvents } from './player';
import { CameraError, decodeSalt, type CameraSession } from './session';

let configured = false;

function configureSdk(): void {
  if (configured) return;
  configured = true;
  AgoraRTC.setLogLevel(3); // warnings and errors only
  AgoraRTC.disableLogUpload();
  // Without a handler the SDK shows its own autoplay prompt (inline styles); we play ourselves.
  AgoraRTC.onAutoplayFailed = () => undefined;
}

/** Create the Agora player on our tiles' elements (the SDK injects nothing visible). */
export function createAgoraPlayer(
  video: VideoTarget | HTMLVideoElement,
  events: PlayerEvents,
): CameraPlayer {
  const target: VideoTarget = typeof video === 'function' ? video : (slot) => (slot ? null : video);
  return new AgoraPlayer(target, events);
}

/** One tile: the camera it asks for (null: the first publisher) and what it shows. */
interface Tile {
  wanted: number | null;
  shown: number | null;
  track: IRemoteVideoTrack | null;
}

const tile = (wanted: number | null): Tile => ({ wanted, shown: null, track: null });

class AgoraPlayer implements CameraPlayer {
  private client: IAgoraRTCClient | null = null;
  private tiles: Tile[] = [tile(null)];
  /** Subscribed video tracks by uid (one subscription per camera, whatever shows it). */
  private tracks = new Map<number, IRemoteVideoTrack>();
  private audioTrack: IRemoteAudioTrack | null = null;
  private muted = true;
  private options: CameraOptions = DEFAULT_OPTIONS;
  private log = new EventLog();
  private state = 'DISCONNECTED';
  private queue: Promise<void> = Promise.resolve();
  private stuckTimer: ReturnType<typeof setTimeout> | null = null;
  /** Whether a tile showed a picture at the last report (null: nothing reported yet). */
  private anyLive: boolean | null = null;
  private lastActive: number | null | undefined = undefined;
  private lastTracks = '';

  constructor(
    private readonly video: VideoTarget,
    private readonly events: PlayerEvents,
  ) {}

  async start(
    session: CameraSession,
    choice: CameraChoice,
    options: CameraOptions = DEFAULT_OPTIONS,
  ): Promise<void> {
    configureSdk();
    if (!AgoraRTC.checkSystemRequirements()) throw new CameraError('browser');
    this.tiles = slotsOf(choice).map(tile);
    this.options = options;
    this.log = new EventLog();
    const client = AgoraRTC.createClient({ mode: options.profile, codec: 'vp8' });
    this.client = client;
    if (session.encryption) {
      const { mode, key, salt } = session.encryption;
      client.setEncryptionConfig(mode as EncryptionMode, key, salt ? decodeSalt(salt) : undefined);
    }
    client.on('user-published', (user, mediaType) => {
      this.log.add('published', `${user.uid} ${mediaType}`);
      this.enqueue(() => this.onPublished(user, mediaType));
    });
    client.on('user-unpublished', (user, mediaType) => {
      this.log.add('unpublished', `${user.uid} ${mediaType}`);
      this.enqueue(() => this.onUnpublished(user, mediaType));
    });
    client.on('user-left', (user) => this.enqueue(() => this.onUnpublished(user, 'video')));
    client.on('token-privilege-will-expire', () => this.events.onTokenExpiring());
    client.on('token-privilege-did-expire', () => this.events.onFailed('expired'));
    client.on('crypt-error', () => this.events.onFailed('failed'));
    client.on('connection-state-change', (state, previous, reason) => {
      this.state = state;
      this.log.add('state', `${previous} > ${state}${reason ? ` (${reason})` : ''}`);
      this.watchConnection(state, reason);
    });
    if (options.profile === 'live') {
      await client.setClientRole('audience', { level: options.latency });
    }
    await client.join(session.app_id, session.channel, session.token || null, session.uid);
    this.events.onWaiting();
    this.anyLive = false;
    this.report();
    // Users that published before the join arrive as user-published events, too.
  }

  async renew(session: CameraSession): Promise<void> {
    await this.client?.renewToken(session.token);
  }

  async applyOptions(options: CameraOptions): Promise<void> {
    this.options = options;
    for (const uid of this.tracks.keys()) await this.applyStreamOptions(uid);
  }

  diagnostics(): DiagnosticsSnapshot | null {
    const client = this.client;
    if (!client) return null;
    const shown = this.shownUids();
    const uid = this.tiles[0]?.shown ?? null;
    let stats: Record<string, Parameters<typeof buildSnapshot>[0]['remote']> = {};
    let rtc;
    try {
      stats = client.getRemoteVideoStats();
      rtc = client.getRTCStats();
    } catch {
      // statistics exist only while connected
    }
    return buildSnapshot({
      state: this.state,
      uid,
      remote: uid === null ? undefined : stats[String(uid)],
      rtc,
      log: this.log,
      tracks: shown.map((u) => ({ uid: u, remote: stats[String(u)] })),
    });
  }

  async select(choice: CameraChoice): Promise<void> {
    const wanted = slotsOf(choice);
    for (let slot = wanted.length; slot < this.tiles.length; slot += 1) {
      const element = this.video(slot);
      if (element) detachTracks(element);
    }
    this.tiles = wanted.map((w, slot) => ({ ...(this.tiles[slot] ?? tile(w)), wanted: w }));
    await new Promise<void>((resolve) =>
      this.enqueue(async () => {
        try {
          await this.assign();
        } finally {
          resolve();
        }
      }),
    );
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    const first = this.video(0);
    if (first) first.muted = muted;
  }

  async stop(): Promise<void> {
    const client = this.client;
    this.client = null;
    if (this.stuckTimer !== null) clearTimeout(this.stuckTimer);
    this.stuckTimer = null;
    this.audioTrack = null;
    this.tracks.clear();
    const slots = Math.max(this.tiles.length, 2);
    this.tiles = this.tiles.map((t) => tile(t.wanted));
    for (let slot = 0; slot < slots; slot += 1) {
      const element = this.video(slot);
      if (element) detachTracks(element);
    }
    if (client) {
      client.removeAllListeners();
      try {
        await client.leave();
      } catch {
        // already gone
      }
    }
  }

  /** Report a dropped connection; a `RECONNECTING` that lasts too long gets a timer. */
  private watchConnection(state: string, reason?: string): void {
    if (this.stuckTimer !== null) clearTimeout(this.stuckTimer);
    this.stuckTimer = null;
    if (state === 'RECONNECTING') {
      this.stuckTimer = setTimeout(() => {
        this.stuckTimer = null;
        if (this.options.autoRejoin) this.events.onConnectionLost?.('stuck');
      }, RECONNECTING_LIMIT_MS);
    } else if (state === 'DISCONNECTED' && reason !== 'LEAVE') {
      if (this.options.autoRejoin && this.events.onConnectionLost) {
        this.events.onConnectionLost('disconnected');
      } else {
        this.events.onFailed('failed');
      }
    }
  }

  /** Run `task` after the earlier ones (a failure is swallowed: the next event retries). */
  private enqueue(task: () => Promise<void> | void): void {
    this.queue = this.queue.then(task).catch(() => undefined);
  }

  /** The user a tile shows: its chosen camera, or (without a choice) the first publisher. */
  private pick(
    wanted: number | null,
    users: IAgoraRTCRemoteUser[],
    without?: IAgoraRTCRemoteUser,
  ): IAgoraRTCRemoteUser | undefined {
    const publishing = users.filter((u) => u.hasVideo && u !== without);
    if (wanted === null) return publishing[0];
    return publishing.find((u) => Number(u.uid) === wanted);
  }

  private shownUids(): number[] {
    return this.tiles.map((t) => t.shown).filter((uid): uid is number => uid !== null);
  }

  /** Tell the viewer which uids publish video (`without`: a user that is just leaving). */
  private report(without?: IAgoraRTCRemoteUser): void {
    if (!this.client) return;
    this.events.onPublishers?.(videoPublishers(this.client.remoteUsers, without?.uid));
  }

  private async onPublished(user: IAgoraRTCRemoteUser, mediaType: string): Promise<void> {
    const client = this.client;
    if (!client) return;
    if (mediaType === 'audio') {
      this.audioTrack = await client.subscribe(user, 'audio');
      if (this.tiles[0]?.track) await this.render(0);
      return;
    }
    if (mediaType !== 'video') return;
    this.report();
    // The same uid publishing again after an unpublish arrives without a track: subscribe again.
    await this.assign();
  }

  private async onUnpublished(user: IAgoraRTCRemoteUser, mediaType: string): Promise<void> {
    if (mediaType !== 'video') return;
    this.report(user);
    this.tracks.delete(Number(user.uid));
    await this.assign(user);
  }

  /**
   * Give every tile its camera: subscribe what is newly shown, attach it to the tile's element,
   * empty tiles whose camera is gone, unsubscribe what no tile shows any more, then report.
   */
  private async assign(without?: IAgoraRTCRemoteUser): Promise<void> {
    const client = this.client;
    if (!client) return;
    for (const [slot, current] of this.tiles.entries()) {
      const user = this.pick(current.wanted, client.remoteUsers, without);
      const uid = user ? Number(user.uid) : null;
      if (user && uid !== null && !this.tracks.has(uid)) {
        const track = await client.subscribe(user, 'video');
        if (this.client !== client) return;
        this.tracks.set(uid, track);
        await this.applyStreamOptions(uid);
      }
      const track = uid === null ? null : (this.tracks.get(uid) ?? null);
      if (current.shown !== uid || current.track !== track) {
        current.shown = uid;
        current.track = track;
        await this.render(slot);
      }
    }
    const shown = new Set(this.shownUids());
    for (const uid of [...this.tracks.keys()]) {
      if (shown.has(uid)) continue;
      this.tracks.delete(uid);
      await client.unsubscribe(uid, 'video').catch(() => undefined);
    }
    this.announce();
  }

  /** Report the first tile's camera, the shown cameras and live or waiting (on changes). */
  private announce(): void {
    const first = this.tiles[0]?.shown ?? null;
    if (first !== this.lastActive) {
      this.lastActive = first;
      this.events.onActive?.(first);
    }
    const shown = this.shownUids();
    const key = shown.join(',');
    if (key !== this.lastTracks) {
      this.lastTracks = key;
      this.events.onTracks?.(shown);
    }
    const any = shown.length > 0;
    if (any !== this.anyLive) {
      this.anyLive = any;
      if (any) this.events.onLive();
      else this.events.onWaiting();
    }
  }

  /** Stream fallback and data saver for a subscribed camera (best effort). */
  private async applyStreamOptions(uid: number | string): Promise<void> {
    const client = this.client;
    if (!client) return;
    // 2: the low stream on a poor network, audio only if that is still too much; 0: off.
    await client.setStreamFallbackOption(uid, this.options.fallback ? 2 : 0).catch(() => undefined);
    // 1: always the low-quality stream (a publisher without dual stream ignores or refuses it).
    await client
      .setRemoteVideoStreamType(uid, this.options.dataSaver ? 1 : 0)
      .catch(() => undefined);
  }

  /** A tile's element shows its track (the first tile also the sound, muted unless asked). */
  private async render(slot: number): Promise<void> {
    const element = this.video(slot);
    if (!element) return;
    const current = this.tiles[slot];
    if (!current?.track) {
      detachTracks(element);
      return;
    }
    const audio = slot === 0 ? this.audioTrack : null;
    await attachTracks(element, [current.track, audio], slot === 0 ? this.muted : true);
  }
}
