/**
 * Sites of the fleet: the list the pickers use and the one way a site is created (the sites
 * page and the inline "new site" of the linked-account flow).
 */
import { fleetApi, unwrap } from '$lib/api/client';
import type { components } from '$lib/generated/fleet';

export type Site = components['schemas']['SiteOut'];
export type SiteIn = components['schemas']['SiteIn'];

/** The browser's IANA time zone, `UTC` when it cannot be determined. */
export function deviceTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

/** The body of a new site: trimmed name, and the device's time zone unless one is given. */
export function newSiteBody(body: SiteIn): SiteIn {
  return {
    ...body,
    name: body.name.trim(),
    timezone: body.timezone?.trim() || deviceTimeZone(),
  };
}

/** Every site the caller sees, the default one included. */
export async function listSites(): Promise<Site[]> {
  const list = await unwrap(
    fleetApi.GET('/api/v1/fleet/sites', { params: { query: { include_default: true } } }),
  );
  return list.items;
}

export function createSite(body: SiteIn): Promise<Site> {
  return unwrap(fleetApi.POST('/api/v1/fleet/sites', { body: newSiteBody(body) }));
}
