/**
 * Runtime configuration of the installation, read once from `/config.json`. nginx renders that
 * file at container start from the environment (`MOWDECK_WEB_MAP_TILES`,
 * `MOWDECK_WEB_LEGAL_ENABLED`, `MOWDECK_WEB_CAMERA_ENABLED`, ...), so one image serves every
 * installation without a rebuild. Missing, unreadable or invalid values fall back to the typed
 * default: the metric grid without any tile server, no links to legal pages, no third-party
 * video (the Agora domains stay out of the CSP), and the official repository as the source code
 * of a development build.
 */

import { DEFAULT_SOURCE, parseSource, type SourceInfo } from '#lib/legal/source.js';

export interface MapTilesConfig {
  /** URL template with `{z}`, `{x}`, `{y}` and optionally `{s}` and `{r}`. */
  url: string;
  /** Attribution text required by the tile provider (shown on the map). */
  attribution: string | null;
  /** Highest zoom level the server delivers tiles for. */
  maxZoom: number;
}

export interface RuntimeConfig {
  /** Optional raster tiles under the map; null keeps the local metric grid (the default). */
  mapTiles: MapTilesConfig | null;
  /** Show links to the legal pages (imprint, terms, privacy) on the sign-in page. */
  legalEnabled: boolean;
  /**
   * The live camera through the vendor cloud (Agora) is allowed: the CSP then lists the Agora
   * domains. The simulator's test pattern works without it.
   */
  cameraEnabled: boolean;
  /** The build and where its source code is (AGPL-3.0 section 13, `#lib/legal/source.js`). */
  source: SourceInfo;
  /**
   * A public demo installation (ADR 0065, `MOWDECK_WEB_DEMO_MODE`): the banner, "Demo" in the
   * title and "Try the demo" on the sign-in page. Absent on every other installation.
   */
  demo?: DemoConfig;
}

export interface DemoConfig {
  /** Hours between two resets of the demo (shown in the banner). */
  resetHours: number;
}

export const DEFAULT_CONFIG: RuntimeConfig = Object.freeze({
  mapTiles: null,
  legalEnabled: false,
  cameraEnabled: false,
  source: DEFAULT_SOURCE,
});

const OSM_ATTRIBUTION = '© OpenStreetMap contributors';

/** Validate an untrusted config object; anything invalid becomes the default. */
export function parseConfig(raw: unknown): RuntimeConfig {
  if (!raw || typeof raw !== 'object') return DEFAULT_CONFIG;
  const { mapTiles, legalEnabled, cameraEnabled, source, demo } = raw as {
    mapTiles?: unknown;
    legalEnabled?: unknown;
    cameraEnabled?: unknown;
    source?: unknown;
    demo?: unknown;
  };
  const legal = legalEnabled === true;
  const camera = cameraEnabled === true;
  const tiles = parseMapTiles(mapTiles);
  const build = parseSource(source);
  const showcase = parseDemo(demo);
  if (!tiles && !legal && !camera && build === DEFAULT_SOURCE && !showcase) return DEFAULT_CONFIG;
  return {
    mapTiles: tiles,
    legalEnabled: legal,
    cameraEnabled: camera,
    source: build,
    ...(showcase ? { demo: showcase } : {}),
  };
}

/** The demo block: an object with whole reset hours from 1 to 168 (anything else: 6). */
function parseDemo(demo: unknown): DemoConfig | null {
  if (!demo || typeof demo !== 'object') return null;
  const { resetHours } = demo as { resetHours?: unknown };
  const hours =
    typeof resetHours === 'number' && Number.isInteger(resetHours) && resetHours >= 1
      ? Math.min(resetHours, 168)
      : 6;
  return { resetHours: hours };
}

function parseMapTiles(tiles: unknown): MapTilesConfig | null {
  if (!tiles || typeof tiles !== 'object') return null;
  const { url, attribution, maxZoom } = tiles as Record<string, unknown>;
  if (typeof url !== 'string' || !/^https?:\/\/[^\s"'<>]+$/.test(url)) return null;
  if (!['{z}', '{x}', '{y}'].every((p) => url.includes(p))) return null;
  const text = typeof attribution === 'string' && attribution.trim() ? attribution.trim() : null;
  const zoom =
    typeof maxZoom === 'number' && Number.isInteger(maxZoom) && maxZoom >= 1 && maxZoom <= 22
      ? maxZoom
      : 19;
  return {
    url,
    attribution: text ?? (/openstreetmap\.org/i.test(url) ? OSM_ATTRIBUTION : null),
    maxZoom: zoom,
  };
}

let pending: Promise<RuntimeConfig> | null = null;

/** Fetch `/config.json` once per page load; never rejects. */
export function loadConfig(fetchFn: typeof fetch = (input, init) => fetch(input, init)) {
  pending ??= fetchFn('/config.json', { cache: 'no-cache', credentials: 'omit' })
    .then(async (response) => {
      if (!response.ok) return DEFAULT_CONFIG;
      const type = response.headers.get('content-type') ?? '';
      if (!type.includes('json')) return DEFAULT_CONFIG; // the SPA fallback in `vite dev`
      return parseConfig(await response.json());
    })
    .catch(() => DEFAULT_CONFIG);
  return pending;
}

/** Forget the cached config (tests). */
export function resetConfig(): void {
  pending = null;
}
