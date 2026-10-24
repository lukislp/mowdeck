/**
 * Permissions of a new API token: the presets, the custom selection from Identity's permission
 * catalogue (`GET /accounts/{id}/permissions`) and the request body of `POST /api-tokens`.
 *
 * The web app grants every token on the whole account. Account keys a module declares with
 * `implied_by` (e.g. `telemetry.view` follows `mower.view`) come with the token at exchange time,
 * so the body carries only the keys the owner chose; the implied ones are shown as included.
 */
import type { components } from '$lib/generated/identity';
import { m } from '$lib/paraglide/messages';

export type CatalogueEntry = components['schemas']['PermissionOut'];
export type TokenBody = components['schemas']['ApiTokenIn'];

export const VIEW_KEY = 'mower.view';

export const PRESETS = {
  view: [VIEW_KEY],
  basic: [VIEW_KEY, 'mower.control'],
  full: [VIEW_KEY, 'mower.control', 'mower.drive', 'fleet.manage', 'schedule.edit'],
} as const satisfies Record<string, readonly string[]>;

export type Preset = keyof typeof PRESETS;
export type Choice = Preset | 'custom';

/** The safer Home Assistant preset; the full one is a deliberate choice. */
export const DEFAULT_CHOICE: Choice = 'basic';

/** Rights that move the mower or change devices and firmware: flagged wherever they appear. */
export const POWERFUL: ReadonlySet<string> = new Set(['mower.drive', 'fleet.manage']);

/** Model keys first in this order (what a person reads top-down), then the rest by key. */
const ORDER = [
  'mower.view',
  'mower.control',
  'schedule.edit',
  'mower.drive',
  'fleet.manage',
  'fleet.bulk_command',
];

function byDisplay(a: string, b: string): number {
  const rank = (key: string): number => {
    const index = ORDER.indexOf(key);
    return index === -1 ? ORDER.length : index;
  };
  return rank(a) - rank(b) || a.localeCompare(b);
}

/** Keys in display order (the list's chips). */
export function displayOrder(keys: Iterable<string>): string[] {
  return [...keys].sort(byDisplay);
}

/** Catalogue entries an API token may hold on the whole account, in display order. */
export function tokenGrantable(catalogue: readonly CatalogueEntry[]): CatalogueEntry[] {
  return catalogue
    .filter((entry) => entry.api_tokens && entry.levels.includes('account'))
    .sort((a, b) => byDisplay(a.key, b.key));
}

/** Mower and schedule rights are meaningless without seeing the mower (as for roles). */
export function needsView(key: string): boolean {
  return key !== VIEW_KEY && (key.startsWith('mower.') || key.startsWith('schedule.'));
}

/** Keys implied by `selected` on the whole account, each with the keys that imply it. */
export function impliedBy(
  selected: Iterable<string>,
  catalogue: readonly CatalogueEntry[],
): Map<string, string[]> {
  const held = new Set(selected);
  const implied = new Map<string, string[]>();
  for (const entry of catalogue) {
    if (entry.in_model || entry.implied_by.length === 0) continue;
    if (entry.implied_by.every((key) => held.has(key))) implied.set(entry.key, entry.implied_by);
  }
  return implied;
}

/** The selection after switching `key` on or off; `mower.view` stays while a right needs it. */
export function toggle(selected: readonly string[], key: string, on: boolean): string[] {
  const next = new Set(selected);
  if (on) {
    next.add(key);
    if (needsView(key)) next.add(VIEW_KEY);
  } else if (!(key === VIEW_KEY && selected.some(needsView))) {
    next.delete(key);
  }
  return [...next].sort();
}

/** The keys to send: the selection without what the rest of it implies anyway, sorted. */
export function requestPermissions(
  selected: Iterable<string>,
  catalogue: readonly CatalogueEntry[],
): string[] {
  const keys = new Set(selected);
  const implied = impliedBy(keys, catalogue);
  return [...keys].filter((key) => !implied.has(key)).sort();
}

/** The keys of a choice; a custom choice uses the selection. */
export function choiceKeys(choice: Choice, custom: readonly string[]): string[] {
  return choice === 'custom' ? [...custom] : [...PRESETS[choice]];
}

/** `POST /accounts/{id}/api-tokens`: one grant on the whole account. */
export function tokenBody(name: string, accountId: string, permissions: string[]): TokenBody {
  return {
    name: name.trim(),
    grants: [{ level: { type: 'account', id: accountId }, permissions }],
  };
}

interface Words {
  short: () => string;
  description: () => string;
}

const WORDS: Record<string, Words> = {
  'mower.view': { short: m.perm_mower_view_short, description: m.perm_mower_view },
  'mower.control': { short: m.perm_mower_control_short, description: m.perm_mower_control },
  'mower.drive': { short: m.perm_mower_drive_short, description: m.perm_mower_drive },
  'schedule.edit': { short: m.perm_schedule_edit_short, description: m.perm_schedule_edit },
  'fleet.manage': { short: m.perm_fleet_manage_short, description: m.perm_fleet_manage },
  'fleet.bulk_command': {
    short: m.perm_fleet_bulk_command_short,
    description: m.perm_fleet_bulk_command,
  },
  'telemetry.view': { short: m.perm_telemetry_view_short, description: m.perm_telemetry_view },
  'realtime.view': { short: m.perm_realtime_view_short, description: m.perm_realtime_view },
  'scheduler.view': { short: m.perm_scheduler_view_short, description: m.perm_scheduler_view },
  'planner.view': { short: m.perm_planner_view_short, description: m.perm_planner_view },
  'notifications.view': {
    short: m.perm_notifications_view_short,
    description: m.perm_notifications_view,
  },
  'notifications.manage': {
    short: m.perm_notifications_manage_short,
    description: m.perm_notifications_manage,
  },
  'device_gateway.view': {
    short: m.perm_device_gateway_view_short,
    description: m.perm_device_gateway_view,
  },
};

/** A short name for chips; the key itself when the app does not know it. */
export function permissionShort(key: string): string {
  return WORDS[key]?.short() ?? key;
}

/** The translated description, else the catalogue's (English) one, else the key. */
export function permissionDescription(key: string, entry?: CatalogueEntry): string {
  return WORDS[key]?.description() ?? entry?.description ?? key;
}

/** What a powerful right does, in one line (undefined for the others). */
export function powerfulWarning(key: string): string | undefined {
  if (key === 'mower.drive') return m.perm_warning_drive();
  if (key === 'fleet.manage') return m.perm_warning_fleet_manage();
  return undefined;
}
