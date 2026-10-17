/**
 * What the camera debug overlay reads (`debug.ts`): the life of a tile's `<video>` element (its
 * media events, the state of the element and of its tracks, every frame signal) and the
 * receiver's WebRTC statistics (`inbound-rtp`: frames received, decoded and dropped, key
 * frames, PLI/FIR/NACK, the decoder and the codec). Together they tell where a picture stops:
 *
 * - frames received rise, decoded stand, PLIs rise: the browser cannot decode the stream (for
 *   H.264 typically a key frame without SPS/PPS, or a profile the decoder refuses);
 * - decoded rise, presented (`requestVideoFrameCallback`) stand: the element does not render
 *   (paused, hidden, a media event in the log says why);
 * - received stand: nothing arrives (sender, network, the Agora edge or the stream type).
 *
 * No SDK import, unit tested with fakes.
 */
import { videoFrameSignals, type VideoFrameSignals } from './freeze';

/** Media events of a `<video>` worth logging. */
export const VIDEO_EVENTS = [
  'loadstart',
  'loadedmetadata',
  'loadeddata',
  'canplay',
  'play',
  'playing',
  'pause',
  'waiting',
  'stalled',
  'suspend',
  'emptied',
  'ended',
  'error',
  'resize',
] as const;

/** Track events worth logging. */
export const TRACK_EVENTS = ['mute', 'unmute', 'ended'] as const;

export interface ProbeEvent {
  /** Milliseconds since the probe started. */
  at: number;
  name: string;
  detail?: string;
}

export interface TrackState {
  kind: string;
  id: string;
  readyState: string;
  muted: boolean;
  enabled: boolean;
}

export interface ElementState extends VideoFrameSignals {
  paused: boolean;
  readyState: number;
  networkState: number;
  width: number;
  height: number;
  currentTime: number;
  tracks: TrackState[];
}

/** The tracks of a `srcObject` (null: none or not a stream). */
function streamTracks(source: unknown): MediaStreamTrack[] {
  const stream = source as { getTracks?: () => MediaStreamTrack[] } | null;
  return typeof stream?.getTracks === 'function' ? stream.getTracks() : [];
}

/** Logs the media events of one `<video>` and of the tracks it plays (the last 16). */
export class VideoProbe {
  readonly events: ProbeEvent[] = [];
  private readonly started: number;
  private video: HTMLVideoElement | null = null;
  private readonly watched = new WeakSet<object>();
  private readonly onVideo = (event: Event): void => this.videoEvent(event);

  constructor(
    private readonly now: () => number = () => performance.now(),
    private readonly limit = 16,
  ) {
    this.started = now();
  }

  /** Watch `video` (null: stop watching). */
  attach(video: HTMLVideoElement | null): void {
    if (video === this.video) return;
    this.detach();
    this.video = video;
    if (!video) return;
    for (const name of VIDEO_EVENTS) video.addEventListener(name, this.onVideo);
    this.watchTracks();
  }

  detach(): void {
    const video = this.video;
    if (!video) return;
    for (const name of VIDEO_EVENTS) video.removeEventListener(name, this.onVideo);
    this.video = null;
  }

  /** The element now (null: nothing attached). */
  state(): ElementState | null {
    const video = this.video;
    if (!video) return null;
    this.watchTracks();
    return {
      paused: video.paused,
      readyState: video.readyState,
      networkState: video.networkState,
      width: video.videoWidth,
      height: video.videoHeight,
      currentTime: Math.round(video.currentTime * 10) / 10,
      ...videoFrameSignals(video),
      tracks: streamTracks(video.srcObject).map((t) => ({
        kind: t.kind,
        id: t.id,
        readyState: t.readyState,
        muted: t.muted,
        enabled: t.enabled,
      })),
    };
  }

  add(name: string, detail?: string): void {
    this.events.push({ at: Math.round(this.now() - this.started), name, detail });
    if (this.events.length > this.limit) this.events.shift();
  }

  private videoEvent(event: Event): void {
    const video = this.video;
    let detail: string | undefined;
    if (event.type === 'resize' && video) detail = `${video.videoWidth}x${video.videoHeight}`;
    if (event.type === 'error' && video?.error)
      detail = `${video.error.code} ${video.error.message}`;
    this.add(event.type, detail);
    if (event.type === 'loadstart' || event.type === 'emptied') this.watchTracks();
  }

  /** Listen to the tracks of the current stream (once per track). */
  private watchTracks(): void {
    for (const track of streamTracks(this.video?.srcObject)) {
      if (this.watched.has(track)) continue;
      this.watched.add(track);
      for (const name of TRACK_EVENTS) {
        track.addEventListener(name, () => this.add(`${track.kind}:${name}`));
      }
    }
  }
}

/** The `inbound-rtp` video numbers of one receiver (null fields: the browser has none). */
export interface InboundVideo {
  codec: string | null;
  decoder: string | null;
  powerEfficient: boolean | null;
  packetsReceived: number | null;
  packetsLost: number | null;
  framesReceived: number | null;
  framesDecoded: number | null;
  keyFramesDecoded: number | null;
  framesDropped: number | null;
  framesPerSecond: number | null;
  width: number | null;
  height: number | null;
  pliCount: number | null;
  firCount: number | null;
  nackCount: number | null;
  freezeCount: number | null;
  freezeSeconds: number | null;
  /** Mean jitter buffer delay in ms. */
  jitterMs: number | null;
}

type StatsEntry = Record<string, unknown>;

const num = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;
const str = (value: unknown): string | null => (typeof value === 'string' ? value : null);

/** The video `inbound-rtp` entry of a stats report, with its codec (null: none). */
export function summarizeInbound(report: Iterable<StatsEntry>): InboundVideo | null {
  const entries = [...report];
  const inbound = entries.find(
    (e) => e.type === 'inbound-rtp' && (e.kind === 'video' || e.mediaType === 'video'),
  );
  if (!inbound) return null;
  const codec = entries.find((e) => e.type === 'codec' && e.id === inbound.codecId);
  const mime = str(codec?.mimeType);
  const fmtp = str(codec?.sdpFmtpLine);
  const delay = num(inbound.jitterBufferDelay);
  const emitted = num(inbound.jitterBufferEmittedCount);
  const efficient = inbound.powerEfficientDecoder;
  return {
    codec: mime ? (fmtp ? `${mime} ${fmtp}` : mime) : null,
    decoder: str(inbound.decoderImplementation),
    powerEfficient: typeof efficient === 'boolean' ? efficient : null,
    packetsReceived: num(inbound.packetsReceived),
    packetsLost: num(inbound.packetsLost),
    framesReceived: num(inbound.framesReceived),
    framesDecoded: num(inbound.framesDecoded),
    keyFramesDecoded: num(inbound.keyFramesDecoded),
    framesDropped: num(inbound.framesDropped),
    framesPerSecond: num(inbound.framesPerSecond),
    width: num(inbound.frameWidth),
    height: num(inbound.frameHeight),
    pliCount: num(inbound.pliCount),
    firCount: num(inbound.firCount),
    nackCount: num(inbound.nackCount),
    freezeCount: num(inbound.freezeCount),
    freezeSeconds: num(inbound.totalFreezesDuration),
    jitterMs: delay !== null && emitted ? Math.round((delay / emitted) * 1000) : null,
  };
}

/** Read the stats of `receiver` (null: none, or the browser refused). */
export async function receiverStats(
  receiver: { getStats(): Promise<RTCStatsReport> } | null | undefined,
): Promise<InboundVideo | null> {
  if (!receiver) return null;
  try {
    const report = await receiver.getStats();
    return summarizeInbound(report.values() as Iterable<StatsEntry>);
  } catch {
    return null;
  }
}

/** The debug readings of one shown camera. */
export interface TrackDebug {
  slot: number;
  uid: number;
  /** The Agora client's numbers (`getRemoteVideoStats()[uid]`), as reported. */
  agora: Record<string, number | string | null>;
  /** The receiver's `inbound-rtp` numbers. */
  inbound: InboundVideo | null;
  /** An encoded transform sits on the receiver (frames pass through script; null: unknown). */
  transform: boolean | null;
  /** The relay path (WHEP): the HTTP status of the offer and the ICE state of the tile. */
  whep?: { status: number | null; ice: string };
}

/** The relay rows of the overlay (ADR 0045): the lease, never its ticket. */
export interface RelayDebug {
  transport: 'relay';
  /** The first 8 characters of the lease id. */
  lease: string;
  heartbeatSeconds: number | null;
  /** Successful and failed heartbeats since the start. */
  beats: number;
  beatFailures: number;
  lastBeatAgoMs: number | null;
  /** Seconds until the current ticket expires (negative: expired). */
  ticketExpiresInS: number | null;
}

/** What a player reports to the debug overlay. */
export interface PlayerDebugReport {
  state: string;
  /** The codec the client was created with. */
  codec: string;
  /** The active experiments (`describeExperiments`). */
  experiments: string;
  tracks: TrackDebug[];
  /** Set for the relay player. */
  relay?: RelayDebug;
}

/** The Agora fields the overlay shows (everything else in the stats is left out). */
export const AGORA_DEBUG_FIELDS = [
  'codecType',
  'receiveResolutionWidth',
  'receiveResolutionHeight',
  'receiveFrameRate',
  'decodeFrameRate',
  'renderFrameRate',
  'receiveBitrate',
  'receivePackets',
  'receivePacketsLost',
  'freezeRate',
  'totalFreezeTime',
  'totalDuration',
] as const;

/** The overlay's subset of an Agora `RemoteVideoTrackStats` (null: none). */
export function agoraDebugFields(
  stats: Record<string, unknown> | null | undefined,
): Record<string, number | string | null> {
  const out: Record<string, number | string | null> = {};
  for (const field of AGORA_DEBUG_FIELDS) {
    const value = stats?.[field];
    out[field] =
      typeof value === 'number' && Number.isFinite(value)
        ? Math.round(value * 100) / 100
        : typeof value === 'string'
          ? value
          : null;
  }
  return out;
}

/** One tile as the overlay sees it. */
export interface TileDebug {
  slot: number;
  status: string;
  uid: number | null;
  element: ElementState | null;
  events: ProbeEvent[];
  freeze: { intervalMs: number | null; thresholdMs: number; sinceMs: number } | null;
}

/** Everything the overlay shows (also what "copy" puts on the clipboard, as JSON). */
export interface DebugInput {
  at: string;
  userAgent: string;
  phase: string;
  frozen: boolean;
  tiles: TileDebug[];
  report: PlayerDebugReport | null;
  /** The transport option of this browser and the path the session took. */
  transport?: { requested: string; used: string | null };
}

const show = (value: unknown): string =>
  value === null || value === undefined || value === '' ? '-' : String(value);

/** The overlay's text, a few short lines per tile. */
export function debugLines(input: DebugInput): string[] {
  const lines = [`${input.at} phase=${input.phase} frozen=${input.frozen}`, input.userAgent];
  if (input.transport) {
    lines.push(`transport ${input.transport.requested} > ${show(input.transport.used)}`);
  }
  const report = input.report;
  const relay = report?.relay;
  if (report && relay) {
    lines.push(
      `relay state=${report.state} codec=${report.codec} lease=${relay.lease}`,
      `relay heartbeat=${show(relay.heartbeatSeconds)}s ok=${relay.beats} failed=${relay.beatFailures} last=${show(relay.lastBeatAgoMs)}ms ticket=${show(relay.ticketExpiresInS)}s`,
    );
  } else if (report) {
    lines.push(`agora state=${report.state} codec=${report.codec} exp=${report.experiments}`);
  }
  for (const tile of input.tiles) {
    lines.push(`tile ${tile.slot} uid=${show(tile.uid)} ${tile.status}`);
    const el = tile.element;
    if (el) {
      lines.push(
        ` el paused=${el.paused} rs=${el.readyState} ns=${el.networkState} ${el.width}x${el.height} t=${el.currentTime}`,
        ` frames presented=${show(el.presented)} decoded=${show(el.decoded)} dropped=${show(el.dropped)}`,
      );
      for (const t of el.tracks) {
        lines.push(` track ${t.kind} ${t.readyState} muted=${t.muted} enabled=${t.enabled}`);
      }
    }
    if (tile.freeze) {
      const f = tile.freeze;
      lines.push(
        ` freeze since=${f.sinceMs}ms limit=${f.thresholdMs}ms interval=${show(f.intervalMs)}`,
      );
    }
    const track = report?.tracks.find((t) => t.slot === tile.slot);
    if (track?.whep) {
      lines.push(` whep status=${show(track.whep.status)} ice=${track.whep.ice}`);
    }
    if (track) {
      const a = track.agora;
      if (!track.whep)
        lines.push(
          ` agora ${show(a.codecType)} ${show(a.receiveResolutionWidth)}x${show(a.receiveResolutionHeight)} fps rx=${show(a.receiveFrameRate)} dec=${show(a.decodeFrameRate)} render=${show(a.renderFrameRate)}`,
          ` agora pkts=${show(a.receivePackets)} lost=${show(a.receivePacketsLost)} kbps=${show(typeof a.receiveBitrate === 'number' ? Math.round(a.receiveBitrate / 1000) : null)} freeze=${show(a.freezeRate)} (${show(a.totalFreezeTime)} s)`,
        );
      const r = track.inbound;
      if (r) {
        lines.push(
          ` rtp ${show(r.codec)}`,
          ` rtp frames rx=${show(r.framesReceived)} dec=${show(r.framesDecoded)} key=${show(r.keyFramesDecoded)} drop=${show(r.framesDropped)} fps=${show(r.framesPerSecond)} ${show(r.width)}x${show(r.height)}`,
          ` rtp pli=${show(r.pliCount)} fir=${show(r.firCount)} nack=${show(r.nackCount)} lost=${show(r.packetsLost)} freezes=${show(r.freezeCount)} jitter=${show(r.jitterMs)}ms`,
          ` rtp decoder=${show(r.decoder)} hw=${show(r.powerEfficient)} transform=${show(track.transform)}`,
        );
      } else {
        lines.push(` rtp - transform=${show(track.transform)}`);
      }
    }
    if (tile.events.length) {
      lines.push(
        ` events ${tile.events.map((e) => `${e.at}:${e.name}${e.detail ? `(${e.detail})` : ''}`).join(' ')}`,
      );
    }
  }
  return lines;
}
