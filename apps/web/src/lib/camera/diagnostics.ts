/**
 * Stability diagnostics of the live camera (ADR 0032): a snapshot of the receive statistics the
 * Agora client reports (`getRemoteVideoStats()[uid]`, `getRTCStats()`), the connection state and
 * a small log of connection and publish events since the join. With both front cameras on screen
 * every attached track has its own numbers (`tracks`; each tile shows its resolution and frame
 * rate); the top-level numbers are the first tile's. No SDK import, so it is unit tested with
 * plain objects.
 */

/** The fields of `RemoteVideoTrackStats` that are shown. */
export interface RemoteStatsLike {
  receiveBitrate?: number;
  receiveFrameRate?: number;
  renderFrameRate?: number;
  receiveResolutionWidth?: number;
  receiveResolutionHeight?: number;
  freezeRate?: number;
  totalFreezeTime?: number;
  /** Cumulative since the join: lost / (received + lost), a fraction 0 to 1. */
  packetLossRate?: number;
  /** Loss in the last 400 ms, a fraction 0 to 1 (the SDK divides the server's percent by 100). */
  currentPacketLossRate?: number;
  /** Capture to playout in ms; 0 in the rtc profile where the SDK has no capture timestamps. */
  end2EndDelay?: number;
  /** Send to playout in ms (inaccurate on Safari and Firefox). */
  receiveDelay?: number;
  /** Send to receive in ms. */
  transportDelay?: number;
}

/** The fields of `AgoraRTCStats` that are shown. */
export interface RtcStatsLike {
  RTT?: number;
}

export type DelaySource = 'end2end' | 'receive' | 'transport' | 'rtt';

export type EventKind = 'state' | 'published' | 'unpublished';

export interface LoggedEvent {
  /** Epoch milliseconds. */
  at: number;
  kind: EventKind;
  text: string;
}

/** The receive numbers of one attached video track (one tile). */
export interface TrackDiagnostics {
  uid: number;
  bitrateKbps: number | null;
  fps: number | null;
  width: number | null;
  height: number | null;
  freezePercent: number | null;
  lossPercent: number | null;
  delayMs: number | null;
  delaySource: DelaySource | null;
}

/** What the panel shows; numbers are null when the SDK reports none. */
export interface DiagnosticsSnapshot {
  state: string;
  uid: number | null;
  bitrateKbps: number | null;
  fps: number | null;
  width: number | null;
  height: number | null;
  /** Share of the last second the picture froze, 0 to 100. */
  freezePercent: number | null;
  freezeSeconds: number | null;
  /** Packet loss of the last 400 ms, percent. */
  lossPercent: number | null;
  /** Packet loss since the join, percent. */
  lossTotalPercent: number | null;
  /** The best delay the SDK reports, see `delaySource`. */
  delayMs: number | null;
  delaySource: DelaySource | null;
  rttMs: number | null;
  stateChanges: number;
  published: number;
  unpublished: number;
  /** The last three events, oldest first. */
  recent: LoggedEvent[];
  /** Every attached video track in tile order (one entry in the single view). */
  tracks: TrackDiagnostics[];
}

/** Connection and publish events since the join. */
export class EventLog {
  private events: LoggedEvent[] = [];
  private counts: Record<EventKind, number> = { state: 0, published: 0, unpublished: 0 };

  constructor(private readonly now: () => number = () => Date.now()) {}

  add(kind: EventKind, text: string): void {
    this.counts[kind] += 1;
    this.events.push({ at: this.now(), kind, text });
    if (this.events.length > 3) this.events.shift();
  }

  count(kind: EventKind): number {
    return this.counts[kind];
  }

  last(): LoggedEvent[] {
    return [...this.events];
  }
}

const finite = (value: number | undefined): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;

const round = (value: number | null, digits = 0): number | null => {
  if (value === null) return null;
  const f = 10 ** digits;
  return Math.round(value * f) / f;
};

/**
 * A loss rate as percent. The SDK reports fractions 0 to 1 (`lost / (received + lost)`); a value
 * above 1 can only be a percent already and is kept as is.
 */
export function lossToPercent(rate: number | null): number | null {
  if (rate === null || rate < 0) return null;
  return round(rate > 1 ? rate : rate * 100, 1);
}

/**
 * The most meaningful delay: end-to-end when the SDK has it (0 means "not measured", as in the
 * rtc profile), then receive and transport delay, finally the round trip to the edge server.
 */
export function pickDelay(
  remote: RemoteStatsLike | undefined,
  rtc: RtcStatsLike | undefined,
): { ms: number | null; source: DelaySource | null } {
  const candidates: [DelaySource, number | null][] = [
    ['end2end', finite(remote?.end2EndDelay)],
    ['receive', finite(remote?.receiveDelay)],
    ['transport', finite(remote?.transportDelay)],
    ['rtt', finite(rtc?.RTT)],
  ];
  for (const [source, value] of candidates) {
    if (value !== null && value > 0) return { ms: round(value), source };
  }
  return { ms: null, source: null };
}

/** The numbers of one track (rates arrive as fractions 0 to 1). */
export function trackSnapshot(
  uid: number,
  remote: RemoteStatsLike | undefined,
  rtc: RtcStatsLike | undefined,
): TrackDiagnostics {
  const bitrate = finite(remote?.receiveBitrate);
  const delay = pickDelay(remote, rtc);
  const freeze = finite(remote?.freezeRate);
  return {
    uid,
    bitrateKbps: round(bitrate === null ? null : bitrate / 1000),
    fps: round(finite(remote?.receiveFrameRate) ?? finite(remote?.renderFrameRate)),
    width: finite(remote?.receiveResolutionWidth),
    height: finite(remote?.receiveResolutionHeight),
    freezePercent: round(freeze === null ? null : freeze * 100, 1),
    lossPercent: lossToPercent(finite(remote?.currentPacketLossRate)),
    delayMs: delay.ms,
    delaySource: delay.source,
  };
}

/** Build the snapshot from the SDK's numbers (rates arrive as fractions 0 to 1). */
export function buildSnapshot(input: {
  state: string;
  uid: number | null;
  remote: RemoteStatsLike | undefined;
  rtc: RtcStatsLike | undefined;
  log: EventLog;
  /** Every attached track with its statistics (default: the first one alone). */
  tracks?: { uid: number; remote: RemoteStatsLike | undefined }[];
}): DiagnosticsSnapshot {
  const { remote, rtc, log } = input;
  const first = input.uid === null ? null : trackSnapshot(input.uid, remote, rtc);
  const fallback = trackSnapshot(-1, remote, rtc);
  const main = first ?? fallback;
  const tracks =
    input.tracks?.map((t) => trackSnapshot(t.uid, t.remote, rtc)) ?? (first ? [first] : []);
  return {
    state: input.state,
    uid: input.uid,
    bitrateKbps: main.bitrateKbps,
    fps: main.fps,
    width: main.width,
    height: main.height,
    freezePercent: main.freezePercent,
    freezeSeconds: round(finite(remote?.totalFreezeTime), 1),
    lossPercent: main.lossPercent,
    lossTotalPercent: lossToPercent(finite(remote?.packetLossRate)),
    delayMs: main.delayMs,
    delaySource: main.delaySource,
    rttMs: round(finite(rtc?.RTT)),
    stateChanges: log.count('state'),
    published: log.count('published'),
    unpublished: log.count('unpublished'),
    recent: log.last(),
    tracks,
  };
}

/** `1280x720 · 15 fps` for a tile caption; null while nothing is measured. */
export function trackSummary(track: TrackDiagnostics | undefined): string | null {
  if (!track) return null;
  const size = track.width && track.height ? `${track.width}x${track.height}` : null;
  const fps = track.fps === null ? null : `${track.fps} fps`;
  if (!size && !fps) return null;
  return `${size ?? '-'} · ${fps ?? '-'}`;
}

/** A number for the readout, `-` when unknown. */
export function shown(value: number | null, unit = ''): string {
  return value === null ? '-' : `${value}${unit}`;
}
