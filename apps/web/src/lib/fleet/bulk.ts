/**
 * Bulk commands of the fleet pages ("pause all", "all to dock"). Fleet has them per site and
 * per group (`POST /sites/{id}/commands`, `POST /groups/{id}/commands`) and checks every mower
 * on its own; the answer lists each mower the caller sees with its outcome (`accepted`,
 * `forbidden`, `not_published`). For "everything visible" the app sends one request per
 * visible site and merges the answers. Pure and unit tested (`bulk.test.ts`).
 */

export type BulkKind = 'pause_mow' | 'return_to_dock';
export const BULK_KINDS: readonly BulkKind[] = ['pause_mow', 'return_to_dock'];

export interface BulkTarget {
  type: 'site' | 'group';
  id: string;
}

export type BulkOutcome = 'accepted' | 'forbidden' | 'not_published';

/** One mower's line in the sheet: `pending` before the answer, `missing` when Fleet left it
 * out (it is not in the target, or the caller no longer sees it). */
export type BulkLineState = BulkOutcome | 'pending' | 'missing' | 'failed';

export interface BulkItem {
  mower_id: string;
  outcome: BulkOutcome;
  command_id?: string | null;
}

export interface BulkAnswer {
  items: BulkItem[];
}

/** The targets for a filter: the group, else the site, else every visible site. */
export function bulkTargets(
  filter: { siteId: string; groupId: string },
  siteIds: readonly string[],
): BulkTarget[] {
  if (filter.groupId) return [{ type: 'group', id: filter.groupId }];
  if (filter.siteId) return [{ type: 'site', id: filter.siteId }];
  return siteIds.map((id) => ({ type: 'site', id }));
}

/** The command body of a kind. */
export function bulkCommand(kind: BulkKind): { kind: BulkKind } {
  return { kind };
}

/** One outcome per mower over several answers; an `accepted` wins over other outcomes. */
export function mergeAnswers(answers: readonly BulkAnswer[]): Map<string, BulkItem> {
  const merged = new Map<string, BulkItem>();
  const rank: Record<BulkOutcome, number> = { accepted: 0, not_published: 1, forbidden: 2 };
  for (const answer of answers) {
    for (const item of answer.items) {
      const known = merged.get(item.mower_id);
      if (!known || rank[item.outcome] < rank[known.outcome]) merged.set(item.mower_id, item);
    }
  }
  return merged;
}

/** The line state of each affected mower after the answers (`failed` when a request failed). */
export function lineStates(
  mowerIds: readonly string[],
  merged: ReadonlyMap<string, BulkItem> | null,
  failed = false,
): Record<string, BulkLineState> {
  const states: Record<string, BulkLineState> = {};
  for (const id of mowerIds) {
    const item = merged?.get(id);
    states[id] = item ? item.outcome : merged === null ? 'pending' : failed ? 'failed' : 'missing';
  }
  return states;
}

/** Counts per line state, for the summary line. */
export function outcomeCounts(
  states: Record<string, BulkLineState>,
): Record<BulkLineState, number> {
  const counts: Record<BulkLineState, number> = {
    accepted: 0,
    forbidden: 0,
    not_published: 0,
    pending: 0,
    missing: 0,
    failed: 0,
  };
  for (const state of Object.values(states)) counts[state] += 1;
  return counts;
}
