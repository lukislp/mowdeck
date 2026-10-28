/**
 * The base map under a theft tracking track (ADR 0058): the layers Telemetry offers (street map
 * and aerial photo, only around the tracked positions), their tile routes, the loader of a share
 * link (the token in a POST body, never in a URL; the owner loads through the aerial photo's
 * authenticated `tileLoader`), the remembered choice
 * of this browser, and a link that opens the last position in the device's maps app.
 *
 * The tiles come from MowDeck's own routes, never from a third party, so the content security
 * policy stays as it is. Pure apart from the injected fetch and storage (`basemap.test.ts`).
 */
import type { TrackMapLayer } from '@mowdeck/ui';

import type { components } from '#lib/generated/telemetry.js';

export type MapLayer = components['schemas']['MapLayerOut'];
export type LayerId = MapLayer['id'];

export const PUBLIC_TILE = '/api/v1/telemetry/public/tracking/tile';
export const LAYER_KEY = 'mowdeck.tracking.layer';

type Load = TrackMapLayer['load'];

/** The owner's tile route of one layer (`{z}/{x}/{y}` filled in by the map). */
export function ownerTileUrl(mowerId: string, layer: LayerId, sessionId?: string | null): string {
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
  return `/api/v1/telemetry/mowers/${encodeURIComponent(mowerId)}/tracking/map/${layer}/{z}/{x}/{y}${query}`;
}

/** The pseudo template of a public tile; `publicTileLoader` turns it into a POST. */
export function publicTileUrl(layer: LayerId): string {
  return `${layer}/{z}/{x}/{y}`;
}

const PUBLIC_PATTERN = /^(map|aerial)\/(\d{1,2})\/(\d{1,9})\/(\d{1,9})$/;

/** Parse a filled public template; null for anything else. */
export function parsePublicTile(
  url: string,
): { layer: LayerId; z: number; x: number; y: number } | null {
  const match = PUBLIC_PATTERN.exec(url);
  if (!match) return null;
  return {
    layer: match[1] as LayerId,
    z: Number(match[2]),
    x: Number(match[3]),
    y: Number(match[4]),
  };
}

/** Tiles of a share link: a plain POST without cookies or referrer, token in the body. */
export function publicTileLoader(
  token: string,
  fetchFn: typeof fetch = (input, init) => fetch(input, init),
): Load {
  return async (url, signal) => {
    const tile = parsePublicTile(url);
    if (!tile) throw new Error('not a tile');
    const response = await fetchFn(PUBLIC_TILE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'image/*' },
      body: JSON.stringify({ token, ...tile }),
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
      cache: 'no-store',
      signal,
    });
    if (!response.ok) throw new Error(`tile ${response.status}`);
    const blob = await response.blob();
    if (!blob.type.startsWith('image/')) throw new Error('not an image');
    return blob;
  };
}

/** The `layers` prop of `TrackMap` from what the server offers. */
export function mapLayers(
  offered: readonly MapLayer[] | null | undefined,
  url: (layer: LayerId) => string,
  load: Load,
  label: (layer: LayerId) => string,
): TrackMapLayer[] {
  return (offered ?? []).map((layer) => ({
    id: layer.id,
    label: label(layer.id),
    url: url(layer.id),
    attribution: layer.attribution || null,
    minZoom: layer.min_zoom,
    maxZoom: layer.max_zoom,
    load,
  }));
}

type Store = Pick<Storage, 'getItem' | 'setItem'>;

function defaultStore(): Store | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/** The layer this browser showed last (null: none chosen yet, the map takes the first). */
export function loadLayer(store: Store | null = defaultStore()): LayerId | null {
  try {
    const value = store?.getItem(LAYER_KEY);
    return value === 'map' || value === 'aerial' ? value : null;
  } catch {
    return null;
  }
}

/** Remember the layer (a per-viewer convenience: failures are ignored). */
export function saveLayer(layer: string | null, store: Store | null = defaultStore()): void {
  if (layer !== 'map' && layer !== 'aerial') return;
  try {
    store?.setItem(LAYER_KEY, layer);
  } catch {
    // private mode or blocked storage
  }
}

/**
 * A link that opens the position in the device's maps app: Apple Maps on Apple devices, a
 * `geo:` URI on Android, OpenStreetMap elsewhere. Only followed when the user taps it.
 */
export function mapsLink(
  point: { lat: number; lon: number },
  userAgent: string = globalThis.navigator?.userAgent ?? '',
): string {
  const lat = point.lat.toFixed(6);
  const lon = point.lon.toFixed(6);
  if (/iPhone|iPad|iPod|Macintosh/.test(userAgent)) {
    return `https://maps.apple.com/?ll=${lat},${lon}&q=${lat},${lon}`;
  }
  if (/Android/.test(userAgent)) return `geo:${lat},${lon}?q=${lat},${lon}`;
  return `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=18/${lat}/${lon}`;
}
