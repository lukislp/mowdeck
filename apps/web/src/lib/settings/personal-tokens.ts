/**
 * Personal access tokens (ADR 0064): a user's own API tokens for scripts, managed under
 * `/api/v1/identity/me/api-tokens`. The scopes are a curated list, the lifetime is always limited
 * (1 to 365 days), and creating one needs a recent step-up (the API client asks for it).
 */
import type { components } from '#lib/generated/identity.js';
import { m } from '#lib/paraglide/messages.js';

export type PersonalToken = components['schemas']['PersonalTokenOut'];
export type PersonalTokenBody = components['schemas']['PersonalTokenIn'];

export const VIEW_SCOPE = 'mower.view';

/** The scopes a personal token can hold, in display order (Identity's `PERSONAL_SCOPES`). */
export const SCOPES = ['mower.view', 'mower.control', 'schedule.edit'] as const;
export type Scope = (typeof SCOPES)[number];

/** Lifetimes offered in the form, in days; the server accepts any value from 1 to 365. */
export const LIFETIMES = [30, 90, 180, 365] as const;
export const DEFAULT_LIFETIME = 90;
export const MAX_LIFETIME = 365;

/** Where the guide and the reference live (the docs folder of the repository). */
export const DOCS_BASE = 'https://github.com/lukislp/mowdeck/blob/main/docs';
export const SCRIPTING_GUIDE_URL = `${DOCS_BASE}/guides/scripting.md`;
export const API_REFERENCE_URL = `${DOCS_BASE}/reference/api/index.md`;

const DAY_MS = 86_400_000;

/** The selection after switching `scope` on or off: control and schedules need the view scope,
 * which then stays on; switching the view scope off is refused while another scope needs it. */
export function toggleScope(selected: readonly Scope[], scope: Scope, on: boolean): Scope[] {
  const next = new Set<Scope>(selected);
  if (on) {
    next.add(scope);
    next.add(VIEW_SCOPE);
  } else if (!(scope === VIEW_SCOPE && selected.some((key) => key !== VIEW_SCOPE))) {
    next.delete(scope);
  }
  return SCOPES.filter((key) => next.has(key));
}

/** True when the view scope cannot be switched off (another scope needs it). */
export function viewLocked(selected: readonly Scope[]): boolean {
  return selected.some((key) => key !== VIEW_SCOPE);
}

/** `POST /me/api-tokens`: the whole account as level (the server's default). */
export function personalTokenBody(
  name: string,
  scopes: readonly Scope[],
  days: number,
): PersonalTokenBody {
  return { name: name.trim(), scopes: [...scopes], expires_in_days: days };
}

/** Whole days until `expiresAt` (0 on the last day, negative once expired). */
export function daysLeft(expiresAt: string, now: number): number {
  return Math.floor((Date.parse(expiresAt) - now) / DAY_MS);
}

/** Share of the lifetime still left, 0 to 1 (the bar under each token). */
export function lifeLeft(token: Pick<PersonalToken, 'created_at' | 'expires_at'>, now: number) {
  const start = Date.parse(token.created_at);
  const end = Date.parse(token.expires_at);
  if (!(end > start)) return 0;
  return Math.min(1, Math.max(0, (end - now) / (end - start)));
}

/** Active: not revoked and not expired. */
export function isActive(token: PersonalToken, now: number): boolean {
  return !token.revoked_at && Date.parse(token.expires_at) > now;
}

/** An active token runs out within 14 days. */
export function expiresSoon(token: PersonalToken, now: number): boolean {
  return isActive(token, now) && daysLeft(token.expires_at, now) < 14;
}

/** The API base of this installation (the web app and the API share the origin). */
export function apiBase(origin: string): string {
  return `${origin.replace(/\/+$/, '')}/api/v1`;
}

/** The first two steps of every script, with the secret read from the environment. */
export function quickStart(origin: string): string {
  const base = apiBase(origin);
  return [
    `export MOWDECK_URL=${base}`,
    'ACCESS=$(curl -s -X POST "$MOWDECK_URL/identity/token" \\',
    "  -H 'Content-Type: application/json' \\",
    '  -d "{\\"grant_type\\":\\"api_token\\",\\"token\\":\\"$MOWDECK_TOKEN\\"}" | jq -r .access_token)',
    'curl -s "$MOWDECK_URL/fleet/mowers" -H "Authorization: Bearer $ACCESS"',
  ].join('\n');
}

interface Words {
  name: () => string;
  hint: () => string;
}

const WORDS: Record<Scope, Words> = {
  'mower.view': { name: m.pat_scope_view, hint: m.pat_scope_view_hint },
  'mower.control': { name: m.pat_scope_control, hint: m.pat_scope_control_hint },
  'schedule.edit': { name: m.pat_scope_schedules, hint: m.pat_scope_schedules_hint },
};

/** The translated name of a scope; the key itself for one the app does not know. */
export function scopeName(key: string): string {
  return (WORDS as Record<string, Words | undefined>)[key]?.name() ?? key;
}

/** What a scope allows, in one line. */
export function scopeHint(key: Scope): string {
  return WORDS[key].hint();
}
