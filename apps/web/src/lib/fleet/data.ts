/**
 * Loading and labels shared by the fleet pages: sites (the default site included), groups and
 * mowers the user can see, and the translated words of bulk outcomes and assignment levels.
 * Group members come from the mowers' `group_ids` (Fleet's group list carries no members).
 */
import { fleetApi, unwrap } from '$lib/api/client';
import type { components } from '$lib/generated/fleet';
import { m } from '$lib/paraglide/messages';
import type { Glyph, Tone } from '$lib/schedules/labels';

import type { BulkKind, BulkLineState } from './bulk';

export type Site = components['schemas']['SiteOut'];
export type Group = components['schemas']['GroupOut'];
export type Mower = components['schemas']['MowerOut'];

export interface Places {
  sites: Site[];
  groups: Group[];
  mowers: Mower[];
}

/** Fleet's name of the default site unless the installation configured another one. */
const FLEET_DEFAULT_SITE_NAME = 'Default site';

/** Sites with the default site's built-in English name translated (the name cannot change). */
export function displaySites<T extends { name: string; is_default: boolean }>(sites: T[]): T[] {
  return sites.map((site) =>
    site.is_default && site.name === FLEET_DEFAULT_SITE_NAME
      ? { ...site, name: m.sites_default_name() }
      : site,
  );
}

/** Sites, groups and mowers in one go (a failed site or group list stays empty). */
export async function loadPlaces(): Promise<Places> {
  const [mowers, groups, sites] = await Promise.all([
    unwrap(fleetApi.GET('/api/v1/fleet/mowers')),
    unwrap(fleetApi.GET('/api/v1/fleet/groups')).catch(() => ({ items: [] as Group[] })),
    unwrap(
      fleetApi.GET('/api/v1/fleet/sites', { params: { query: { include_default: true } } }),
    ).catch(() => ({ items: [] as Site[] })),
  ]);
  return { mowers: mowers.items, groups: groups.items, sites: displaySites(sites.items) };
}

/** The word, tone and glyph of a bulk line. */
export function bulkLine(state: BulkLineState): { label: string; tone: Tone; glyph: Glyph } {
  switch (state) {
    case 'accepted':
      return { label: m.bulk_outcome_accepted(), tone: 'mowing', glyph: 'check' };
    case 'forbidden':
      return { label: m.bulk_outcome_forbidden(), tone: 'error', glyph: 'alert' };
    case 'not_published':
      return { label: m.bulk_outcome_not_published(), tone: 'paused', glyph: 'clock' };
    case 'pending':
      return { label: m.bulk_outcome_pending(), tone: 'offline', glyph: 'dash' };
    case 'missing':
      return { label: m.bulk_outcome_missing(), tone: 'unknown', glyph: 'question' };
    case 'failed':
      return { label: m.bulk_outcome_failed(), tone: 'error', glyph: 'alert' };
  }
}

export function bulkKindLabel(kind: BulkKind): string {
  return kind === 'pause_mow' ? m.bulk_pause_all() : m.bulk_dock_all();
}

export type LevelType = 'account' | 'site' | 'group' | 'mower';
export const LEVEL_TYPES: readonly LevelType[] = ['account', 'site', 'group', 'mower'];

export function levelLabel(type: LevelType): string {
  switch (type) {
    case 'account':
      return m.level_account();
    case 'site':
      return m.level_site();
    case 'group':
      return m.level_group();
    case 'mower':
      return m.level_mower();
  }
}
