/** The translated texts of `MowerMap` (the library takes every text as a prop). */
import type { MowerMapLabels } from '@mowdeck/ui';

import { formatNumber } from '#lib/i18n.js';
import { m } from '#lib/paraglide/messages.js';

/** A vendor name that is only a long number (a hash such as "4525245168006937683"). */
const HASH_NAME = /^\d{8,}$/;

/** Custom area names from Fleet (`MowerOut.area_names`), by area id. */
export type AreaNames = Readonly<Record<string, string>> | null | undefined;

/** What `areaLabel` needs besides the area: the custom names and the area's map position. */
export interface AreaLabelContext {
  /** Custom names from Fleet; an entry wins over everything else. */
  overrides?: AreaNames;
  /** 0-based position of the area in the map's area order (`MowerMap.areas`). */
  index?: number | null;
}

/** True for a name that says nothing: missing, blank or only a long number (a vendor hash). */
export function isPlaceholderName(name: string | null | undefined): boolean {
  const cleaned = name?.trim() ?? '';
  return !cleaned || HASH_NAME.test(cleaned);
}

/**
 * The display name of a map area, the same rule as `mowdeck_contracts.v1.area_display_name`:
 * the custom name from Fleet, else the mower's own name (unless empty or only a long number),
 * else "Area N" / "Bereich N" with N the 1-based position in the map's area order. Without an
 * index (legacy callers) the fallback is "Area <id>", shortened to the last four digits for a
 * hash. The mower's stored name stays as it is.
 */
export function areaLabel(
  area: { id: string; name?: string | null },
  context: AreaLabelContext = {},
): string {
  const custom = context.overrides?.[area.id]?.trim();
  if (custom) return custom;
  const name = area.name?.trim();
  if (name && !isPlaceholderName(name)) return name;
  if (context.index != null && context.index >= 0) return m.area_numbered({ n: context.index + 1 });
  const raw = name || area.id;
  return m.area_unnamed({ id: HASH_NAME.test(raw) ? raw.slice(-4) : raw });
}

/**
 * An `areaName` function for one map (the map components call it with an area only): it knows
 * every area's position in `areas` and the custom names, so unnamed areas read "Area 1",
 * "Area 2" in map order. The numbers are stable for one map snapshot.
 */
export function areaNamer(
  areas: readonly { id: string }[] | null | undefined,
  overrides?: AreaNames,
): (area: { id: string; name?: string | null }) => string {
  const index = new Map((areas ?? []).map((a, i) => [a.id, i] as const));
  return (area) => areaLabel(area, { overrides, index: index.get(area.id) ?? null });
}

/**
 * An `areaName` function across several mowers (the fleet map): each area is named against
 * the map and the custom names of the mower it belongs to.
 */
export function fleetAreaNamer(
  mowers: readonly {
    map?: { areas?: readonly { id: string }[] | null } | null;
    area_names?: AreaNames;
  }[],
): (area: { id: string; name?: string | null }) => string {
  const namers = new Map<string, (area: { id: string; name?: string | null }) => string>();
  for (const mower of mowers) {
    const namer = areaNamer(mower.map?.areas, mower.area_names);
    for (const area of mower.map?.areas ?? []) if (!namers.has(area.id)) namers.set(area.id, namer);
  }
  return (area) => (namers.get(area.id) ?? areaLabel)(area);
}

export function mapLabels(): MowerMapLabels {
  return {
    region: m.map_region(),
    zoomIn: m.map_zoom_in(),
    zoomOut: m.map_zoom_out(),
    fit: m.map_fit(),
    follow: m.map_follow(),
    layers: m.map_layers(),
    route: m.map_layer_route(),
    zones: m.map_layer_zones(),
    rtkTint: m.map_layer_rtk(),
    expand: m.map_expand(),
    collapse: m.map_collapse(),
    north: m.map_north(),
    dock: m.map_dock(),
    mower: m.map_mower(),
    rtkFixed: m.map_rtk_fixed(),
    rtkFloat: m.map_rtk_float(),
    rtkNone: m.map_rtk_none(),
    rtkLayer: m.map_layer_rtk_cells(),
    rtkLegend: m.map_rtk_legend(),
    rtkUnknown: m.map_rtk_unknown(),
    rtkCellSize: m.map_rtk_cell_size(),
    rtkCell: (metres) => `${formatNumber(metres, metres % 1 ? 1 : 0)} m`,
    rtkAggregate: m.map_rtk_aggregate(),
    rtkWorst: m.map_rtk_worst(),
    rtkMajority: m.map_rtk_majority(),
    selectArea: (name) => m.map_select_area({ name }),
    renameArea: (name) => m.area_rename_label({ name }),
    path: m.map_path(),
    pathDone: m.map_path_done(),
    pathAhead: m.map_path_ahead(),
    pathPreview: m.map_path_preview(),
    spot: m.map_spot(),
    selectZone: (name) => m.zones_select({ name }),
  };
}
