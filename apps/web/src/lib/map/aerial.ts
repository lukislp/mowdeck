/**
 * The aerial photo under the mower map: what Telemetry offers for a mower
 * (`GET /api/v1/telemetry/mowers/{id}/aerial`), the authenticated tile loader and the remembered
 * choice (shown or not, opacity) of this browser.
 *
 * The tiles come from MowDeck's own route, never from a third party, so the content security
 * policy stays as it is; they need the bearer token, so they are fetched here and handed to the
 * map as blobs. Pure apart from the injected fetch and storage (`aerial.test.ts`).
 */
import type { MapAerial } from '@mowdeck/ui';

import { ApiProblem, problemFromResponse } from '#lib/api/problem.js';

/** The answer of the aerial endpoint (a structural copy of Telemetry's `AerialOut`). */
export interface AerialInfo {
  mower_id: string;
  enabled: boolean;
  tile_url: string | null;
  attribution: string | null;
  min_zoom: number | null;
  max_zoom: number | null;
  bounds: { south: number; west: number; north: number; east: number } | null;
}

export function aerialInfoUrl(mowerId: string): string {
  return `/api/v1/telemetry/mowers/${encodeURIComponent(mowerId)}/aerial`;
}

/** Ask whether the installation offers an aerial photo for this mower's map. */
export async function fetchAerialInfo(
  fetcher: typeof fetch,
  mowerId: string,
  signal?: AbortSignal,
): Promise<AerialInfo> {
  let response: Response;
  try {
    response = await fetcher(aerialInfoUrl(mowerId), {
      headers: { Accept: 'application/json' },
      signal,
    });
  } catch (error) {
    if (error instanceof ApiProblem) throw error;
    throw ApiProblem.network();
  }
  if (!response.ok) throw await problemFromResponse(response);
  return (await response.json()) as AerialInfo;
}

/** Load one tile as a blob; any non-2xx answer or non-image becomes an error. */
export function tileLoader(fetcher: typeof fetch): MapAerial['load'] {
  return async (url, signal) => {
    const response = await fetcher(url, { headers: { Accept: 'image/*' }, signal });
    if (!response.ok) throw await problemFromResponse(response);
    const blob = await response.blob();
    if (!blob.type.startsWith('image/')) throw new Error('not an image');
    return blob;
  };
}

/** The `aerial` prop of `MowerMap`, or null while the photo is off or not offered. */
export function mapAerial(
  info: AerialInfo | null,
  prefs: AerialPrefs,
  load: MapAerial['load'],
): MapAerial | null {
  if (!prefs.shown || !info?.enabled || !info.tile_url) return null;
  const b = info.bounds;
  return {
    url: info.tile_url,
    opacity: prefs.opacity,
    attribution: info.attribution,
    minZoom: info.min_zoom ?? undefined,
    maxZoom: info.max_zoom ?? undefined,
    bounds: b ? [b.south, b.west, b.north, b.east] : null,
    load,
  };
}

export interface AerialPrefs {
  shown: boolean;
  /** 0.2 to 1. */
  opacity: number;
}

export const AERIAL_PREFS_KEY = 'mowdeck.aerial';
export const DEFAULT_AERIAL_PREFS: AerialPrefs = { shown: false, opacity: 0.85 };
export const AERIAL_OPACITY_MIN = 0.2;

type Store = Pick<Storage, 'getItem' | 'setItem'>;

function defaultStore(): Store | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/** Clamp to the slider's range; anything that is not a number is the default. */
export function clampOpacity(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return DEFAULT_AERIAL_PREFS.opacity;
  return Math.min(1, Math.max(AERIAL_OPACITY_MIN, Math.round(value * 100) / 100));
}

/** The stored choice; anything missing or broken falls back to the default. */
export function loadAerialPrefs(store: Store | null = defaultStore()): AerialPrefs {
  try {
    const raw = store?.getItem(AERIAL_PREFS_KEY);
    const parsed = raw ? (JSON.parse(raw) as Partial<Record<keyof AerialPrefs, unknown>>) : {};
    return { shown: parsed.shown === true, opacity: clampOpacity(parsed.opacity) };
  } catch {
    return { ...DEFAULT_AERIAL_PREFS };
  }
}

/** Remember the choice (a per-viewer convenience: failures are ignored). */
export function saveAerialPrefs(prefs: AerialPrefs, store: Store | null = defaultStore()): void {
  try {
    store?.setItem(AERIAL_PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // private mode or blocked storage
  }
}
