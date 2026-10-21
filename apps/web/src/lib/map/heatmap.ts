/**
 * Heat layers of the mower map: the layer and period choice, the time window of a period and
 * the request to Telemetry's heatmap endpoint
 * (`GET /api/v1/telemetry/mowers/{id}/heatmap?layer=&from=&to=&cell=`).
 *
 * The endpoint is newer than the committed OpenAPI documents, so the answer is typed locally
 * here (a structural copy of the contract) and fetched with the shared authenticated fetch;
 * once the generated types include it, `HeatmapResponse` can become an alias of the schema.
 * Pure apart from the injected fetch and storage, so it is unit tested (`heatmap.test.ts`).
 */
import type { HeatGap, HeatLayerData, HeatLegend } from '@mowdeck/ui';

import { ApiProblem, problemFromResponse } from '$lib/api/problem';

/** The layers of the switcher; `off` draws nothing. */
export type HeatLayer = 'off' | 'coverage' | 'rtk' | 'wifi' | 'mnet';
export const HEAT_LAYERS: readonly HeatLayer[] = ['off', 'coverage', 'rtk', 'wifi', 'mnet'];

/** The periods: since the last mowing job, the last 24 hours, 7 days or 30 days. */
export type HeatPeriod = 'job' | '24h' | '7d' | '30d';
export const HEAT_PERIODS: readonly HeatPeriod[] = ['job', '24h', '7d', '30d'];

/** Cell edges offered (metres); the server may answer with a coarser one. */
export const HEAT_CELL_SIZES = [1, 2, 5] as const;
export type HeatCellSize = (typeof HEAT_CELL_SIZES)[number];

/** The answer of the heatmap endpoint (local copy of the contract). */
export interface HeatmapResponse {
  mower_id: string;
  layer: Exclude<HeatLayer, 'off'>;
  cell_m: number;
  from: string;
  to: string;
  reference?: { origin_lat: number; origin_lon: number; rotation_deg: number } | null;
  cells: { x: number; y: number; value: number; n?: number | null }[];
  legend: HeatLegend;
  gaps?: HeatGap[] | null;
  samples: number;
  truncated: boolean;
}

/** A time window in ISO form; `fallback` when the period could not be resolved as asked. */
export interface HeatWindow {
  from: string;
  to: string;
  fallback?: boolean;
}

export function isHeatLayer(value: unknown): value is HeatLayer {
  return typeof value === 'string' && (HEAT_LAYERS as readonly string[]).includes(value);
}

export function isHeatPeriod(value: unknown): value is HeatPeriod {
  return typeof value === 'string' && (HEAT_PERIODS as readonly string[]).includes(value);
}

/**
 * Only a number is a cell size: `Number(value)` on an arbitrary stored value can throw (an
 * object whose `toString` is not callable) and would accept strings such as `'1'` or `' 2 '`.
 */
export function isHeatCellSize(value: unknown): value is HeatCellSize {
  return typeof value === 'number' && (HEAT_CELL_SIZES as readonly number[]).includes(value);
}

/** A command as the window needs it (Fleet `CommandView`). */
export interface CommandLike {
  kind: string;
  issued_at: string;
}

const HOUR = 3_600_000;

/**
 * The window of a period at `now`. `job` starts at the newest `start_mow` among `commands` and
 * ends at the first `return_to_dock` after it (or now); without a start in the list it falls back
 * to the last 24 hours and says so.
 */
export function periodWindow(
  period: HeatPeriod,
  now: Date,
  commands: readonly CommandLike[] = [],
): HeatWindow {
  const to = now.toISOString();
  const back = (hours: number) => new Date(now.getTime() - hours * HOUR).toISOString();
  switch (period) {
    case '24h':
      return { from: back(24), to };
    case '7d':
      return { from: back(24 * 7), to };
    case '30d':
      return { from: back(24 * 30), to };
    case 'job': {
      const sorted = [...commands]
        .filter((c) => !Number.isNaN(Date.parse(c.issued_at)))
        .sort((a, b) => Date.parse(a.issued_at) - Date.parse(b.issued_at));
      const start = [...sorted].reverse().find((c) => c.kind === 'start_mow');
      if (!start) return { from: back(24), to, fallback: true };
      const startAt = Date.parse(start.issued_at);
      const end = sorted.find(
        (c) => c.kind === 'return_to_dock' && Date.parse(c.issued_at) > startAt,
      );
      // A job the server started after the browser's "now" (clock skew) is open until its start,
      // so the window never ends before it begins.
      const open = new Date(Math.max(startAt, now.getTime())).toISOString();
      return { from: new Date(startAt).toISOString(), to: end ? end.issued_at : open };
    }
  }
}

/** The request path of a layer and window. */
export function heatmapUrl(
  mowerId: string,
  layer: Exclude<HeatLayer, 'off'>,
  window: HeatWindow,
  cell: number,
): string {
  const query = new URLSearchParams({
    layer,
    from: window.from,
    to: window.to,
    cell: String(cell),
  });
  return `/api/v1/telemetry/mowers/${encodeURIComponent(mowerId)}/heatmap?${query.toString()}`;
}

/** Load one layer; a non-2xx answer becomes an {@link ApiProblem} (404 `no-map-yet` too). */
export async function fetchHeatmap(
  fetcher: typeof fetch,
  mowerId: string,
  layer: Exclude<HeatLayer, 'off'>,
  window: HeatWindow,
  cell: number,
  signal?: AbortSignal,
): Promise<HeatmapResponse> {
  let response: Response;
  try {
    response = await fetcher(heatmapUrl(mowerId, layer, window, cell), {
      headers: { Accept: 'application/json' },
      signal,
    });
  } catch (error) {
    if (error instanceof ApiProblem) throw error;
    throw ApiProblem.network();
  }
  if (!response.ok) throw await problemFromResponse(response);
  return (await response.json()) as HeatmapResponse;
}

/** True for the problem Telemetry answers before the mower sent its first map. */
export function isNoMapYet(problem: ApiProblem): boolean {
  return problem.status === 404 && problem.slug === 'no-map-yet';
}

/** The `heat` prop of `MowerMap` from an answer. */
export function heatLayerData(response: HeatmapResponse | null): HeatLayerData | null {
  if (!response) return null;
  return {
    cells: response.cells,
    cellM: response.cell_m,
    legend: response.legend,
    gaps: response.layer === 'coverage' ? (response.gaps ?? []) : [],
  };
}

/** Missed spots, largest first, keeping their index in the answer. */
export function sortedGaps(response: HeatmapResponse | null): { index: number; gap: HeatGap }[] {
  return (response?.gaps ?? [])
    .map((gap, index) => ({ index, gap }))
    .sort((a, b) => b.gap.area_m2 - a.gap.area_m2);
}

// --- the choice per browser -------------------------------------------------------------------

export interface HeatPrefs {
  layer: HeatLayer;
  period: HeatPeriod;
  cell: HeatCellSize;
}

export const HEAT_PREFS_KEY = 'mowdeck.heatmap';
export const DEFAULT_HEAT_PREFS: HeatPrefs = { layer: 'off', period: '7d', cell: 1 };

type Store = Pick<Storage, 'getItem' | 'setItem'>;

function defaultStore(): Store | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/** The stored choice; anything missing or broken falls back to the default. */
export function loadHeatPrefs(store: Store | null = defaultStore()): HeatPrefs {
  try {
    const raw = store?.getItem(HEAT_PREFS_KEY);
    const parsed = raw ? (JSON.parse(raw) as Partial<Record<keyof HeatPrefs, unknown>>) : {};
    return {
      layer: isHeatLayer(parsed.layer) ? parsed.layer : DEFAULT_HEAT_PREFS.layer,
      period: isHeatPeriod(parsed.period) ? parsed.period : DEFAULT_HEAT_PREFS.period,
      cell: isHeatCellSize(parsed.cell) ? parsed.cell : DEFAULT_HEAT_PREFS.cell,
    };
  } catch {
    return { ...DEFAULT_HEAT_PREFS };
  }
}

/** Remember the choice (a per-viewer convenience: failures are ignored). */
export function saveHeatPrefs(prefs: HeatPrefs, store: Store | null = defaultStore()): void {
  try {
    store?.setItem(HEAT_PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // private mode or blocked storage
  }
}

/** The legend kind a layer answers with (for the legend before the first answer). */
export function legendKindOf(layer: HeatLayer): HeatLegend['kind'] {
  if (layer === 'rtk') return 'fix';
  if (layer === 'wifi' || layer === 'mnet') return 'dbm';
  return 'ratio';
}

/**
 * A stable key of the last job among `commands` ('' without one), so a page refetches the
 * `job` period only when the job itself changes, not on every command list reload.
 */
export function jobAnchor(commands: readonly CommandLike[]): string {
  const window = periodWindow('job', new Date(0), commands);
  return window.fallback ? '' : `${window.from}|${window.to}`;
}
