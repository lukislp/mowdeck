/**
 * The limit rows of the plan section: each limit of the plan with the current use where the app
 * knows it (the lists it loads), and whether it is reached. `null` limit means unlimited.
 */
import { LIMIT_KEYS, type LimitKey, type PlanLimits } from './types';

export interface LimitRow {
  key: LimitKey;
  limit: number | null;
  used: number | null;
  /** Use divided by the limit, 0..1 (null: unlimited or unknown use). */
  share: number | null;
  reached: boolean;
}

/** Limits without a count (a retention, not a number of things). */
const NOT_COUNTED: ReadonlySet<LimitKey> = new Set(['history_days']);

export function limitRows(
  limits: PlanLimits,
  usage: Partial<Record<LimitKey, number | null>>,
): LimitRow[] {
  return LIMIT_KEYS.map((key) => {
    const limit = limits[key] ?? null;
    const used = NOT_COUNTED.has(key) ? null : (usage[key] ?? null);
    const share =
      limit === null || used === null ? null : limit === 0 ? 1 : Math.min(1, used / limit);
    return { key, limit, used, share, reached: limit !== null && used !== null && used >= limit };
  });
}

/** Is the limit counted (shows a use)? */
export function isCounted(key: LimitKey): boolean {
  return !NOT_COUNTED.has(key);
}

/** In `grace` or `expired` only viewing and the emergency stop remain. */
export function isRestricted(status: string | null | undefined): boolean {
  return status === 'grace' || status === 'expired';
}

/** The end date that belongs to the status (trial or grace), if any. */
export function statusUntil(plan: {
  status: string;
  trial_until?: string | null;
  grace_until?: string | null;
}): string | null {
  if (plan.status === 'trial') return plan.trial_until ?? null;
  if (plan.status === 'grace') return plan.grace_until ?? null;
  return null;
}
