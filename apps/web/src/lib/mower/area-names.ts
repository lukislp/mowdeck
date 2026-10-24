/** Custom area names in Fleet (`PUT /mowers/{id}/area-names/{area_id}`); the mower keeps its own. */
import { fleetApi, unwrap } from '#lib/api/client.js';

import { isPlaceholderName, type AreaNames } from './map-labels';

/** Longest custom name (Fleet answers 422 above it; `AREA_NAME_MAX_LENGTH` in the contracts). */
export const AREA_NAME_MAX = 60;

/** What saving the rename form means. */
export type RenameIntent =
  | { kind: 'none' } // nothing changes: close the form
  | { kind: 'set'; name: string }
  | { kind: 'reset' } // drop the custom name
  | { kind: 'too_long' };

/**
 * Turn what the user typed into a change. A blank field, or the mower's own name, drops the
 * custom name (the area follows the mower again); the same name as now changes nothing.
 */
export function renameIntent(
  typed: string,
  area: { id: string; name?: string | null },
  overrides: AreaNames,
): RenameIntent {
  const cleaned = typed.split(/\s+/).filter(Boolean).join(' ');
  const current = overrides?.[area.id]?.trim() || null;
  if (cleaned.length > AREA_NAME_MAX) return { kind: 'too_long' };
  const own = isPlaceholderName(area.name) ? null : (area.name?.trim() ?? null);
  if (!cleaned || cleaned === own) return current ? { kind: 'reset' } : { kind: 'none' };
  if (cleaned === current) return { kind: 'none' };
  return { kind: 'set', name: cleaned };
}

/** The value the form starts with: the custom name, else the mower's own name, else empty. */
export function initialRenameValue(
  area: { id: string; name?: string | null },
  overrides: AreaNames,
): string {
  const current = overrides?.[area.id]?.trim();
  if (current) return current;
  return isPlaceholderName(area.name) ? '' : (area.name?.trim() ?? '');
}

/** Store or drop (`name: null`) a custom name; answers all custom names of the mower. */
export async function saveAreaName(
  mowerId: string,
  areaId: string,
  name: string | null,
): Promise<Record<string, string>> {
  const result = await unwrap(
    fleetApi.PUT('/api/v1/fleet/mowers/{mower_id}/area-names/{area_id}', {
      params: { path: { mower_id: mowerId, area_id: areaId } },
      body: { name },
    }),
  );
  return result.names;
}
