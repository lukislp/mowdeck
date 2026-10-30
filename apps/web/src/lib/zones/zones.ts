/**
 * Virtual zones (ADR 0070): the owner's own zones for a mower whose vendor shares no map. The
 * calls of Telemetry's zone routes and the pure helpers of the zone page and the mower page
 * (`zones.test.ts`). Whether zones are offered comes from the server (`availability`: the mower
 * reports positions and its vendor shares no map areas), never from the vendor's name.
 */
import type { LocalPoint, MapSnapshot } from '@mowdeck/ui';

import { telemetryApi, unwrap } from '#lib/api/client.js';
import type { components } from '#lib/generated/telemetry.js';
import { m } from '#lib/paraglide/messages.js';

type Schemas = components['schemas'];
export type ZonesOverview = Schemas['ZonesOut'];
export type VirtualZone = Schemas['ZoneOut'];
export type ZoneStats = Schemas['ZoneStatsOut'];
export type ZoneTotals = Schemas['ZoneTotalsOut'];
export type ZonePatch = Schemas['ZonePatch'];
export type DraftProblem = 'corners' | 'crossing' | 'small' | 'large' | 'many';

const path = (mowerId: string) => ({ mower_id: mowerId });

export function getZones(mowerId: string, signal?: AbortSignal): Promise<ZonesOverview> {
  return unwrap(
    telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/zones', {
      params: { path: path(mowerId) },
      signal,
    }),
  );
}

export function getZoneStats(mowerId: string, days: number): Promise<ZoneStats> {
  return unwrap(
    telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/zones/stats', {
      params: { path: path(mowerId), query: { days } },
    }),
  );
}

export function createZone(
  mowerId: string,
  body: { name: string; ring: number[][]; start_plan_ref?: string | null },
): Promise<VirtualZone> {
  return unwrap(
    telemetryApi.POST('/api/v1/telemetry/mowers/{mower_id}/zones', {
      params: { path: path(mowerId) },
      body,
    }),
  );
}

/** Change a zone; fields left out stay as they are. */
export function updateZone(
  mowerId: string,
  zoneId: string,
  changes: Partial<ZonePatch>,
): Promise<VirtualZone> {
  return unwrap(
    telemetryApi.PATCH('/api/v1/telemetry/mowers/{mower_id}/zones/{zone_id}', {
      params: { path: { mower_id: mowerId, zone_id: zoneId } },
      body: { unlink_start_plan: false, ...changes },
    }),
  );
}

export async function deleteZone(mowerId: string, zoneId: string): Promise<void> {
  await unwrap(
    telemetryApi.DELETE('/api/v1/telemetry/mowers/{mower_id}/zones/{zone_id}', {
      params: { path: { mower_id: mowerId, zone_id: zoneId } },
    }),
  );
}

/** Zones are worth showing: offered for the mower, or some exist already. */
export function zonesShown(
  overview: Pick<ZonesOverview, 'availability' | 'zones'> | null,
): boolean {
  return !!overview && (overview.availability === 'available' || overview.zones.length > 0);
}

/**
 * The map the zone page draws on: the mower's own snapshot (a vendor without areas still sends
 * its geographic reference), else an empty map anchored where the server says the garden is.
 */
export function zoneMap(
  snapshot: MapSnapshot | null | undefined,
  anchor: { lat: number; lon: number } | null | undefined,
): MapSnapshot | null {
  const ref = snapshot?.reference;
  if (ref && (ref.origin_lat !== 0 || ref.origin_lon !== 0)) return snapshot;
  if (!anchor) return null;
  return {
    reference: { origin_lat: anchor.lat, origin_lon: anchor.lon, rotation_deg: 0 },
    areas: [],
  };
}

/** "2 h 05 min", "12 min", "< 1 min", "0 min" (time in a zone). */
export function formatZoneTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0 min';
  if (seconds < 60) return '< 1 min';
  const minutes = Math.round(seconds / 60);
  const h = Math.floor(minutes / 60);
  const min = minutes % 60;
  if (h === 0) return `${min} min`;
  return `${h} h ${String(min).padStart(2, '0')} min`;
}

/** Bar widths 0..1 relative to the zone with the most time. */
export function timeShares(
  items: readonly Pick<ZoneTotals, 'zone_id' | 'seconds'>[],
): Map<string, number> {
  const most = Math.max(0, ...items.map((i) => i.seconds));
  return new Map(items.map((i) => [i.zone_id, most > 0 ? i.seconds / most : 0]));
}

/** Why a drawing cannot be saved, in words. */
export function draftProblemText(problem: DraftProblem): string {
  switch (problem) {
    case 'corners':
      return m.zones_draft_corners();
    case 'crossing':
      return m.zones_draft_crossing();
    case 'small':
      return m.zones_draft_small();
    case 'large':
      return m.zones_draft_large();
    case 'many':
      return m.zones_draft_many();
  }
}

/** Remove the last corner (undo while drawing). */
export function undoCorner(points: readonly LocalPoint[]): LocalPoint[] {
  return points.slice(0, -1);
}

/** The zone the mower is in, by name, or null outside every zone or before the first fix. */
export function currentZoneName(overview: ZonesOverview | null): string | null {
  const presence = overview?.presence;
  if (!presence?.zone_id) return null;
  return presence.zone_name ?? overview?.zones.find((z) => z.id === presence.zone_id)?.name ?? null;
}
