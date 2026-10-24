/**
 * Loading helpers of the schedules pages: the account's mowers and groups (names and targets)
 * and the rights per target. The UI asks `POST /me/permissions` only to decide what to render;
 * the Scheduler checks `can_edit_schedule` and `can_control` again on every call.
 */
import { fleetApi, schedulerApi, telemetryApi, unwrap } from '$lib/api/client';
import { permissions, type ObjectRef } from '$lib/live.svelte';

import type { CapsLike } from './mow-options';
import type { Preview, Run, Schedule } from './types';

export interface Target {
  id: string;
  name: string;
  /** Mowers: their groups; groups: their mowers (when Fleet lists them). */
  related: string[];
  /** The site (null: a cross-site group, which uses the default site). */
  siteId: string | null;
  /** Mowers: the custom names of their map areas (Fleet's `area_names`). */
  areaNames?: Record<string, string>;
}

export interface Site {
  id: string;
  name: string;
  timezone: string;
  isDefault: boolean;
}

export interface Targets {
  mowers: Target[];
  groups: Target[];
  sites: Site[];
}

/** Mowers, groups and sites the user can see (failed group or site lists stay empty). */
export async function loadTargets(): Promise<Targets> {
  const [mowers, groups, sites] = await Promise.all([
    unwrap(fleetApi.GET('/api/v1/fleet/mowers')),
    unwrap(fleetApi.GET('/api/v1/fleet/groups')).catch(() => ({ items: [] })),
    unwrap(
      fleetApi.GET('/api/v1/fleet/sites', { params: { query: { include_default: true } } }),
    ).catch(() => ({ items: [] })),
  ]);
  return {
    mowers: mowers.items.map((x) => ({
      id: x.id,
      name: x.name,
      related: x.group_ids,
      siteId: x.site_id,
      areaNames: x.area_names ?? {},
    })),
    groups: groups.items.map((g) => ({
      id: g.id,
      name: g.name,
      related: g.mower_ids ?? [],
      siteId: g.site_id,
    })),
    sites: sites.items.map((s) => ({
      id: s.id,
      name: s.name,
      timezone: s.timezone,
      isDefault: s.is_default,
    })),
  };
}

/**
 * The capabilities of every mower the user can see, by mower id: what a schedule may set for
 * it. A mower whose gateway never reported any is missing (it offers nothing); a failed request
 * gives an empty map.
 */
export async function loadCapabilities(): Promise<Record<string, CapsLike>> {
  try {
    const found = await unwrap(
      telemetryApi.GET('/api/v1/telemetry/accounts/me/mowers/capabilities'),
    );
    return Object.fromEntries(
      found.items.map((item) => [String(item.mower_id), item.capabilities as CapsLike]),
    );
  } catch {
    return {};
  }
}

/** The time zone a target follows without an override (its site, else the default site). */
export function siteTimeZone(targets: Targets, type: 'mower' | 'group', id: string): string | null {
  const target = (type === 'group' ? targets.groups : targets.mowers).find((t) => t.id === id);
  const site =
    targets.sites.find((s) => s.id === target?.siteId) ?? targets.sites.find((s) => s.isDefault);
  return site?.timezone ?? null;
}

export interface Rights {
  edit: boolean;
  control: boolean;
}

const NO_RIGHTS: Rights = { edit: false, control: false };

/** Edit (`can_edit_schedule` and `can_control`) and control rights per `type:id` target key. */
export async function targetRights(
  targets: { type: 'mower' | 'group'; id: string }[],
): Promise<Record<string, Rights>> {
  if (!targets.length) return {};
  const objects = targets.map((t) => `${t.type}:${t.id}` as ObjectRef);
  try {
    const answers = await permissions.checkObjects(objects, ['can_edit_schedule', 'can_control']);
    return Object.fromEntries(
      objects.map((object) => {
        const a = answers[object];
        return [
          object,
          { edit: !!a?.can_edit_schedule && !!a.can_control, control: !!a?.can_control },
        ];
      }),
    );
  } catch {
    return Object.fromEntries(objects.map((object) => [object, NO_RIGHTS]));
  }
}

export function rightsOf(rights: Record<string, Rights>, schedule: Schedule): Rights {
  return rights[`${schedule.target_type}:${schedule.target_id}`] ?? NO_RIGHTS;
}

export async function loadPreview(id: string, count = 5): Promise<Preview> {
  return unwrap(
    schedulerApi.GET('/api/v1/scheduler/schedules/{schedule_id}/preview', {
      params: { path: { schedule_id: id }, query: { count } },
    }),
  );
}

export async function loadLastRun(id: string): Promise<Run | null> {
  const list = await unwrap(
    schedulerApi.GET('/api/v1/scheduler/schedules/{schedule_id}/runs', {
      params: { path: { schedule_id: id }, query: { limit: 1 } },
    }),
  );
  return list.items[0] ?? null;
}

/** Schedules that start this mower: its own and those of its groups, the next one first. */
export function schedulesOfMower(
  schedules: readonly Schedule[],
  mowerId: string,
  groupIds: readonly string[],
): Schedule[] {
  return schedules
    .filter(
      (s) =>
        (s.target_type === 'mower' && s.target_id === mowerId) ||
        (s.target_type === 'group' && groupIds.includes(s.target_id)),
    )
    .filter((s) => s.enabled && s.next_run_at)
    .sort((a, b) => Date.parse(a.next_run_at ?? '') - Date.parse(b.next_run_at ?? ''));
}
