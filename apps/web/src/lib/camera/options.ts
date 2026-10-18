/**
 * Connection options of the live camera (ADR 0032), kept per browser in `localStorage`
 * (`mowdeck.cameraOptions`). The defaults favour a stable picture over minimal latency.
 *
 * * `profile`: the Agora channel profile. `rtc` (default) joins like a call participant that
 *   only subscribes; `live` joins as audience, which adds latency levels.
 * * `latency`: audience latency level, `live` only: 1 low latency, 2 ultra low latency.
 * * `fallback`: `setStreamFallbackOption(uid, 2)`: under a poor network Agora drops to the
 *   low-quality stream and, if that is still too much, to audio only.
 * * `dataSaver`: `setRemoteVideoStreamType(uid, 1)`: always the low-quality stream (works only
 *   when the publisher sends two streams).
 * * `autoRejoin`: rejoin with a fresh session after a lost connection.
 * * `transport` (ADR 0045): `auto` (default) lets Fleet choose, the camera relay where it can
 *   serve the mower and the direct vendor path otherwise; `relay` and `direct` force one path
 *   for debugging (`relay` shows the relay's errors and never falls back). The profile,
 *   latency, fallback and data saver options belong to the direct Agora path only.
 */
export type CameraProfile = 'rtc' | 'live';
export type LatencyLevel = 1 | 2;
/** How this browser wants the camera: Fleet's `CameraTransport`. */
export type CameraTransport = 'auto' | 'relay' | 'direct';

export interface CameraOptions {
  profile: CameraProfile;
  latency: LatencyLevel;
  fallback: boolean;
  dataSaver: boolean;
  autoRejoin: boolean;
  transport: CameraTransport;
}

export const OPTIONS_KEY = 'mowdeck.cameraOptions';
/**
 * Keys of removed debug features (the console statistics and the debug overlay's experiments,
 * ADR 0044): a browser that once set them still has them, so the viewer removes them.
 */
export const STALE_KEYS: readonly string[] = ['mowdeck.cameraDebug', 'mowdeck.cameraExperiments'];

export const DEFAULT_OPTIONS: CameraOptions = {
  profile: 'rtc',
  latency: 1,
  fallback: true,
  dataSaver: false,
  autoRejoin: true,
  transport: 'auto',
};

/** Backoff of the automatic rejoin after `DISCONNECTED`; the last delay repeats. */
export const REJOIN_DELAYS_MS: readonly number[] = [1_000, 2_000, 4_000, 8_000, 16_000];
/** Give up after this many rejoin attempts in a row (reset when the picture is live). */
export const MAX_REJOIN_ATTEMPTS = 6;
/** `RECONNECTING` for longer than this: join again with a fresh session (token). */
export const RECONNECTING_LIMIT_MS = 20_000;

/** Delay before rejoin attempt number `attempt` (0-based). */
export function rejoinDelayMs(attempt: number): number {
  const last = REJOIN_DELAYS_MS.length - 1;
  return REJOIN_DELAYS_MS[Math.min(Math.max(0, attempt), last)] ?? 1_000;
}

/** Valid options from untrusted JSON; anything else falls back to the default per field. */
export function parseOptions(raw: unknown): CameraOptions {
  const o: Record<string, unknown> =
    raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
  const flag = (value: unknown, fallback: boolean): boolean =>
    typeof value === 'boolean' ? value : fallback;
  return {
    profile: o.profile === 'live' || o.profile === 'rtc' ? o.profile : DEFAULT_OPTIONS.profile,
    latency: o.latency === 2 ? 2 : o.latency === 1 ? 1 : DEFAULT_OPTIONS.latency,
    fallback: flag(o.fallback, DEFAULT_OPTIONS.fallback),
    dataSaver: flag(o.dataSaver, DEFAULT_OPTIONS.dataSaver),
    autoRejoin: flag(o.autoRejoin, DEFAULT_OPTIONS.autoRejoin),
    transport:
      o.transport === 'relay' || o.transport === 'direct' || o.transport === 'auto'
        ? o.transport
        : DEFAULT_OPTIONS.transport,
  };
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

export function loadOptions(
  storage: Pick<Storage, 'getItem'> | null = safeStorage(),
): CameraOptions {
  try {
    const text = storage?.getItem(OPTIONS_KEY);
    return parseOptions(text ? JSON.parse(text) : null);
  } catch {
    return { ...DEFAULT_OPTIONS };
  }
}

export function saveOptions(
  options: CameraOptions,
  storage: Pick<Storage, 'setItem'> | null = safeStorage(),
): void {
  try {
    storage?.setItem(OPTIONS_KEY, JSON.stringify(options));
  } catch {
    // private mode: the options last until the page closes
  }
}

/** Removes the {@link STALE_KEYS}; a failing storage is left alone. */
export function clearStaleKeys(storage: Pick<Storage, 'removeItem'> | null = safeStorage()): void {
  for (const key of STALE_KEYS) {
    try {
      storage?.removeItem(key);
    } catch {
      // private mode or blocked storage: nothing to clean up
    }
  }
}
