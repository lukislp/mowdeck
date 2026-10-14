/**
 * The time range of the RTK signal map on the detail page (today, 7 days, 30 days) and the
 * paged loading of its positions: Telemetry returns at most `MOWDECK_TELEMETRY_QUERY_MAX_ROWS`
 * (5000) of the newest positions per request (`truncated` when more exist), so older pages are
 * fetched with `to` set to the oldest point so far, up to a cap. Pure apart from the injected
 * fetcher, so it is unit tested (`rtk-range.test.ts`).
 */
import type { RoutePoint } from '@mowdeck/ui';

export type RtkRange = 'today' | '7d' | '30d';
export const RTK_RANGES: readonly RtkRange[] = ['today', '7d', '30d'];

/** Points per request (Telemetry's default row cap) and pages at most (20 000 points). */
export const RTK_PAGE = 5000;
export const RTK_MAX_PAGES = 4;

export function isRtkRange(value: unknown): value is RtkRange {
  return typeof value === 'string' && (RTK_RANGES as readonly string[]).includes(value);
}

/** Start of the range at `now` (`today` is local midnight). */
export function rangeStart(range: RtkRange, now: Date): Date {
  switch (range) {
    case 'today': {
      const start = new Date(now);
      start.setHours(0, 0, 0, 0);
      return start;
    }
    case '7d':
      return new Date(now.getTime() - 7 * 86_400_000);
    case '30d':
      return new Date(now.getTime() - 30 * 86_400_000);
  }
}

/** One request: the newest `limit` positions in `[from, to)`, oldest first. */
export type PositionPage = (query: {
  from: string;
  to: string;
  limit: number;
}) => Promise<{ positions: RoutePoint[]; truncated: boolean }>;

export interface LoadedPositions {
  points: RoutePoint[];
  /** More positions exist in the range than were loaded (the oldest are missing). */
  truncated: boolean;
  pages: number;
}

/** Load the range newest first, page by page, until it is complete or the cap is reached. */
export async function loadRange(
  fetchPage: PositionPage,
  from: Date,
  to: Date,
  options: { limit?: number; maxPages?: number } = {},
): Promise<LoadedPositions> {
  const limit = options.limit ?? RTK_PAGE;
  const maxPages = options.maxPages ?? RTK_MAX_PAGES;
  const pages: RoutePoint[][] = [];
  let end = to.toISOString();
  let truncated = false;
  for (let i = 0; i < maxPages; i += 1) {
    const page = await fetchPage({ from: from.toISOString(), to: end, limit });
    pages.unshift(page.positions);
    truncated = page.truncated;
    const oldest = page.positions[0];
    if (!page.truncated || !oldest || oldest.observed_at === end) break;
    end = oldest.observed_at;
  }
  return { points: pages.flat(), truncated, pages: pages.length };
}
