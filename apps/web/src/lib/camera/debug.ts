/**
 * Camera debugging on a real device (ADR 0032, ADR 0044 amendment 2026-10-17). A phone has no
 * developer tools at hand, so the switches come in through the address bar and stay per browser
 * (`localStorage`):
 *
 * * `?camdebug=1` turns the debug overlay on (`mowdeck.cameraDebug = "1"`, the flag that also
 *   sends the statistics to `console.debug`), `?camdebug=0` turns it and every experiment off.
 * * Experiments (only while debugging), to find out on the device why a stream stops:
 *   `?camcodec=h264|vp8` (the codec the Agora client is created with; default `vp8`),
 *   `?camclone=1` (the tile plays a clone of the remote track instead of the track itself),
 *   `?camfakevideo=1` (the simulator's test pattern plays through the tile's `<video>` as a
 *   `MediaStream`, `canvas.captureStream()`, like a real camera; the e2e tests use it) and
 *   `?camparam=NAME=value` (one `AgoraRTC.setParameter(NAME, value)` before the client is
 *   created; repeat for more, `?camparam=` clears them). `true`/`false` and numbers are passed
 *   as such, anything else as text.
 *
 * No SDK import, unit tested with a fake storage.
 */
import { DEBUG_KEY } from './options';

export const EXPERIMENTS_KEY = 'mowdeck.cameraExperiments';

export type ExperimentCodec = 'vp8' | 'h264';
export type ParamValue = boolean | number | string;

export interface CameraExperiments {
  /** The codec of `AgoraRTC.createClient` (null: the default, `vp8`). */
  codec: ExperimentCodec | null;
  /** Play a clone of the remote track. */
  clone: boolean;
  /** The test pattern plays through the tile's `<video>` (where `captureStream` exists). */
  fakeVideo: boolean;
  /** `AgoraRTC.setParameter(name, value)` before the client is created. */
  params: Record<string, ParamValue>;
}

export const NO_EXPERIMENTS: CameraExperiments = {
  codec: null,
  clone: false,
  fakeVideo: false,
  params: {},
};

type Store = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function safeStorage(): Store | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

const PARAM_NAME = /^[A-Z][A-Z0-9_]{0,63}$/;

function paramValue(text: string): ParamValue {
  if (text === 'true') return true;
  if (text === 'false') return false;
  const n = Number(text);
  return text.trim() !== '' && Number.isFinite(n) ? n : text.slice(0, 200);
}

/** Valid experiments from untrusted JSON; anything else falls back to none. */
export function parseExperiments(raw: unknown): CameraExperiments {
  const o: Record<string, unknown> =
    raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
  const params: Record<string, ParamValue> = {};
  if (o.params && typeof o.params === 'object') {
    for (const [name, value] of Object.entries(o.params as Record<string, unknown>)) {
      if (!PARAM_NAME.test(name)) continue;
      if (typeof value === 'boolean' || typeof value === 'number' || typeof value === 'string') {
        params[name] = value;
      }
    }
  }
  return {
    codec: o.codec === 'h264' || o.codec === 'vp8' ? o.codec : null,
    clone: o.clone === true,
    fakeVideo: o.fakeVideo === true,
    params,
  };
}

/** The stored experiments; none unless debugging is on. */
export function loadExperiments(storage: Store | null = safeStorage()): CameraExperiments {
  try {
    if (storage?.getItem(DEBUG_KEY) !== '1') return { ...NO_EXPERIMENTS, params: {} };
    const text = storage.getItem(EXPERIMENTS_KEY);
    return parseExperiments(text ? JSON.parse(text) : null);
  } catch {
    return { ...NO_EXPERIMENTS, params: {} };
  }
}

/**
 * Apply the `cam*` switches of a query string (`location.search`) to the storage. Returns
 * whether anything changed.
 */
export function applyDebugQuery(search: string, storage: Store | null = safeStorage()): boolean {
  if (!storage) return false;
  const query = new URLSearchParams(search);
  const switches = ['camdebug', 'camcodec', 'camclone', 'camfakevideo', 'camparam'];
  if (!switches.some((name) => query.has(name))) return false;
  try {
    const debug = query.get('camdebug');
    if (debug === '0') {
      storage.removeItem(DEBUG_KEY);
      storage.removeItem(EXPERIMENTS_KEY);
      return true;
    }
    if (debug === '1') storage.setItem(DEBUG_KEY, '1');
    const text = storage.getItem(EXPERIMENTS_KEY);
    const next = parseExperiments(text ? JSON.parse(text) : null);
    const codec = query.get('camcodec');
    if (codec !== null) next.codec = codec === 'h264' || codec === 'vp8' ? codec : null;
    const clone = query.get('camclone');
    if (clone !== null) next.clone = clone === '1' || clone === 'true';
    const fakeVideo = query.get('camfakevideo');
    if (fakeVideo !== null) next.fakeVideo = fakeVideo === '1' || fakeVideo === 'true';
    const params = query.getAll('camparam');
    if (params.length) {
      if (params.some((p) => p === '')) next.params = {};
      for (const p of params) {
        const at = p.indexOf('=');
        const name = at < 0 ? p : p.slice(0, at);
        if (PARAM_NAME.test(name)) next.params[name] = at < 0 ? true : paramValue(p.slice(at + 1));
      }
    }
    storage.setItem(EXPERIMENTS_KEY, JSON.stringify(next));
    return true;
  } catch {
    return false;
  }
}

/** One line for the overlay (`codec=h264 clone NAME=value`, `none`). */
export function describeExperiments(e: CameraExperiments): string {
  const parts: string[] = [];
  if (e.codec) parts.push(`codec=${e.codec}`);
  if (e.clone) parts.push('clone');
  if (e.fakeVideo) parts.push('fakevideo');
  for (const [name, value] of Object.entries(e.params)) parts.push(`${name}=${String(value)}`);
  return parts.length ? parts.join(' ') : 'none';
}
