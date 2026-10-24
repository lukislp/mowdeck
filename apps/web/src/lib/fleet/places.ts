/**
 * Sites and groups in the web app: form state, client checks and request bodies for Fleet's
 * `/sites` and `/groups`, the mower list sections by site, and the fleet filters. Pure and unit
 * tested (`places.test.ts`); Fleet checks everything again.
 */
import { isValidTimeZone, parseNumber } from '#lib/schedules/form.js';

export interface SiteLike {
  id: string;
  name: string;
  timezone: string;
  is_default: boolean;
  address?: Record<string, unknown> | null;
  latitude?: number | null;
  longitude?: number | null;
}

export interface GroupLike {
  id: string;
  name: string;
  site_id: string | null;
  mower_ids?: string[] | null;
}

export interface MowerLike {
  id: string;
  name: string;
  site_id: string;
  group_ids: string[];
}

export interface SiteForm {
  name: string;
  timezone: string;
  street: string;
  postalCode: string;
  city: string;
  country: string;
  latitude: string;
  longitude: string;
}

export type SiteField = 'name' | 'timezone' | 'country' | 'latitude' | 'longitude';
export type SiteIssue =
  | { code: 'required' }
  | { code: 'timezone' }
  | { code: 'country' }
  | { code: 'range'; min: number; max: number }
  | { code: 'both' };

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** The form of a new site (time zone of the device) or of an existing one. */
export function siteForm(site: SiteLike | null, deviceZone: string): SiteForm {
  const address = site?.address ?? {};
  return {
    name: site?.name ?? '',
    timezone: site?.timezone ?? deviceZone,
    street: text(address.street),
    postalCode: text(address.postal_code),
    city: text(address.city),
    country: text(address.country),
    latitude: site?.latitude != null ? String(site.latitude) : '',
    longitude: site?.longitude != null ? String(site.longitude) : '',
  };
}

/** Client checks (Fleet's rules): name (except the default site), IANA zone, ISO country,
 * coordinates in range and both or none. */
export function validateSite(
  form: SiteForm,
  isDefault = false,
): Partial<Record<SiteField, SiteIssue>> {
  const issues: Partial<Record<SiteField, SiteIssue>> = {};
  if (!isDefault && !form.name.trim()) issues.name = { code: 'required' };
  if (!form.timezone.trim()) issues.timezone = { code: 'required' };
  else if (!isValidTimeZone(form.timezone.trim())) issues.timezone = { code: 'timezone' };
  if (form.country.trim() && !/^[A-Za-z]{2}$/.test(form.country.trim())) {
    issues.country = { code: 'country' };
  }
  const lat = form.latitude.trim() ? parseNumber(form.latitude) : null;
  const lon = form.longitude.trim() ? parseNumber(form.longitude) : null;
  if (form.latitude.trim() && (lat === null || lat < -90 || lat > 90)) {
    issues.latitude = { code: 'range', min: -90, max: 90 };
  }
  if (form.longitude.trim() && (lon === null || lon < -180 || lon > 180)) {
    issues.longitude = { code: 'range', min: -180, max: 180 };
  }
  if (!!form.latitude.trim() !== !!form.longitude.trim()) {
    issues[form.latitude.trim() ? 'longitude' : 'latitude'] = { code: 'both' };
  }
  return issues;
}

export interface SiteBody {
  name?: string;
  timezone: string;
  address: {
    street: string | null;
    postal_code: string | null;
    city: string | null;
    country: string | null;
  } | null;
  latitude: number | null;
  longitude: number | null;
}

/** The body of `POST /sites` or `PATCH /sites/{id}` (the default site keeps its name; an empty
 * address is sent as null, which clears it). */
export function siteBody(form: SiteForm, isDefault = false): SiteBody {
  const value = (v: string) => v.trim() || null;
  const address = {
    street: value(form.street),
    postal_code: value(form.postalCode),
    city: value(form.city),
    country: value(form.country)?.toUpperCase() ?? null,
  };
  const anyAddress = Object.values(address).some((v) => v !== null);
  return {
    ...(isDefault ? {} : { name: form.name.trim() }),
    timezone: form.timezone.trim(),
    address: anyAddress ? address : null,
    latitude: form.latitude.trim() ? parseNumber(form.latitude) : null,
    longitude: form.longitude.trim() ? parseNumber(form.longitude) : null,
  };
}

/** A one-line address for lists ("Street 1, 12345 City, DE"), or null without one. */
export function addressLine(address: Record<string, unknown> | null | undefined): string | null {
  if (!address) return null;
  const place = [text(address.postal_code), text(address.city)].filter(Boolean).join(' ');
  const parts = [text(address.street), place, text(address.country)].filter(Boolean);
  return parts.length ? parts.join(', ') : null;
}

/** Sites in display order: the default site first, then by name. */
export function sortSites<T extends SiteLike>(sites: readonly T[]): T[] {
  return [...sites].sort(
    (a, b) => Number(b.is_default) - Number(a.is_default) || a.name.localeCompare(b.name),
  );
}

export interface GroupForm {
  name: string;
  /** Empty: a cross-site group. */
  siteId: string;
}

export function groupForm(group: GroupLike | null): GroupForm {
  return { name: group?.name ?? '', siteId: group?.site_id ?? '' };
}

export function groupBody(form: GroupForm): { name: string; site_id: string | null } {
  return { name: form.name.trim(), site_id: form.siteId || null };
}

/** The group ids of a mower after adding or removing one group. */
export function toggleGroup(
  groupIds: readonly string[],
  groupId: string,
  member: boolean,
): string[] {
  const rest = groupIds.filter((id) => id !== groupId);
  return member ? [...rest, groupId] : rest;
}

export interface SiteSection<T> {
  /** The site id, or `''` for mowers of sites the user cannot see. */
  id: string;
  site: SiteLike | null;
  items: T[];
}

/** Mowers grouped by site in site order; mowers of unknown sites go to a last section. */
export function sectionsBySite<T extends { site_id: string }>(
  items: readonly T[],
  sites: readonly SiteLike[],
): SiteSection<T>[] {
  const known = new Map(sortSites(sites).map((site) => [site.id, site]));
  const sections = new Map<string, SiteSection<T>>();
  for (const site of known.values()) sections.set(site.id, { id: site.id, site, items: [] });
  const other: SiteSection<T> = { id: '', site: null, items: [] };
  for (const item of items) (sections.get(item.site_id) ?? other).items.push(item);
  return [...sections.values(), other].filter((section) => section.items.length > 0);
}

/** The fleet filter: a site, a group, or everything visible. */
export interface FleetFilter {
  siteId: string;
  groupId: string;
}

export function filterMowers<T extends MowerLike>(mowers: readonly T[], filter: FleetFilter): T[] {
  return mowers.filter(
    (m) =>
      (!filter.siteId || m.site_id === filter.siteId) &&
      (!filter.groupId || m.group_ids.includes(filter.groupId)),
  );
}

/** Quick filters of the mower list. */
export type QuickFilter = 'all' | 'active' | 'attention' | 'docked';
export const QUICK_FILTERS: readonly QuickFilter[] = ['all', 'active', 'attention', 'docked'];

/** Whether a mower in `activity` (null: no telemetry) passes the quick filter. */
export function passesQuick(
  filter: QuickFilter,
  activity: string | null | undefined,
  stale: boolean,
): boolean {
  switch (filter) {
    case 'all':
      return true;
    case 'active':
      return activity === 'mowing' || activity === 'returning' || activity === 'paused';
    case 'attention':
      return activity === 'error' || activity === 'offline' || stale || !activity;
    case 'docked':
      return activity === 'docked' || activity === 'charging' || activity === 'idle';
  }
}
