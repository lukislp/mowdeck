/**
 * The error code lexicon of a mower's vendor, browsable (ADR 0067). Telemetry picks the lexicon
 * by the mower's connection and serves a filtered page of it; the app holds no code table and no
 * vendor logic. The codes a mower has reported come from its health counts and its current
 * state; their lexicon spelling (a vendor may report `-1201` for the entry `1201`) comes from the
 * server's explanations, so the matching stays provider neutral.
 */
import { telemetryApi, unwrap } from '#lib/api/client.js';
import type { components } from '#lib/generated/telemetry.js';

import { explainMowerCodes, type ErrorExplanation, type ErrorSeverity } from './error-explain.js';

type Schemas = components['schemas'];
export type LexiconPage = Schemas['ErrorLexiconSearchReply'];
export type ErrorCategory = Schemas['MowerErrorCategory'];

/** Entries per page. */
export const PAGE_SIZE = 50;
/** The order of the severity filter, mildest first. */
export const SEVERITIES: readonly ErrorSeverity[] = ['info', 'warning', 'error', 'critical'];

export interface LexiconFilters {
  query: string;
  /** A `MowerErrorCategory`, '' for all (a native select's value). */
  category: string;
  severity: ErrorSeverity | '';
  /** Only the codes this mower has reported. */
  onlySeen: boolean;
}

export const NO_FILTERS: LexiconFilters = {
  query: '',
  category: '',
  severity: '',
  onlySeen: false,
};

/** A page of the mower's lexicon. */
export function browseLexicon(
  mowerId: string,
  filters: LexiconFilters,
  seen: readonly string[],
  locale: string,
  offset = 0,
): Promise<LexiconPage> {
  return unwrap(
    telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/error-lexicon', {
      params: {
        path: { mower_id: mowerId },
        query: lexiconQuery(filters, seen, locale, offset),
      },
    }),
  );
}

/** The query of one page; with `onlySeen` the server filters by the seen lexicon codes. */
export function lexiconQuery(
  filters: LexiconFilters,
  seen: readonly string[],
  locale: string,
  offset = 0,
): {
  q?: string;
  category?: ErrorCategory;
  severity?: ErrorSeverity;
  code?: string[];
  offset: number;
  limit: number;
  locale: string;
} {
  const query = filters.query.trim().slice(0, 100);
  return {
    ...(query ? { q: query } : {}),
    ...(filters.category ? { category: filters.category as ErrorCategory } : {}),
    ...(filters.severity ? { severity: filters.severity } : {}),
    ...(filters.onlySeen ? { code: seen.slice(0, 200) } : {}),
    offset,
    limit: PAGE_SIZE,
    locale,
  };
}

/** The codes a mower reported (health counts, `unknown` is no code), most frequent first. */
export function reportedCodes(
  counts: Record<string, number> | null | undefined,
  current: string | null | undefined,
): string[] {
  const rows = Object.entries(counts ?? {})
    .filter(([code, count]) => code !== 'unknown' && code !== '' && count > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const codes = rows.map(([code]) => code);
  if (current && !codes.includes(current)) codes.unshift(current);
  return codes;
}

/** What this mower has had, keyed by the lexicon's spelling of each code. */
export interface MowerMarks {
  /** Lexicon code to the number of times the mower reported it. */
  seen: Record<string, number>;
  /** The lexicon code of the current error, null without one. */
  current: string | null;
}

export const NO_MARKS: MowerMarks = { seen: {}, current: null };

/**
 * Join the reported codes with their explanations (in the same order) to marks keyed by the
 * lexicon's code. Codes the lexicon does not know are left out: they never appear in the list.
 */
export function marksOf(
  reported: readonly string[],
  explained: readonly ErrorExplanation[],
  counts: Record<string, number> | null | undefined,
  current: string | null | undefined,
): MowerMarks {
  const seen: Record<string, number> = {};
  let currentCode: string | null = null;
  reported.forEach((raw, i) => {
    const explanation = explained[i];
    if (!explanation?.known) return;
    const count = counts?.[raw] ?? 0;
    seen[explanation.code] = (seen[explanation.code] ?? 0) + count;
    if (raw === current) currentCode = explanation.code;
  });
  return { seen, current: currentCode };
}

/** Explain the reported codes in chunks of 20 (the server's limit) and build the marks. */
export async function loadMarks(
  mowerId: string,
  counts: Record<string, number> | null | undefined,
  current: string | null | undefined,
  locale: string,
): Promise<MowerMarks> {
  const reported = reportedCodes(counts, current).slice(0, 200);
  const explained: ErrorExplanation[] = [];
  for (let i = 0; i < reported.length; i += 20) {
    explained.push(...(await explainMowerCodes(mowerId, reported.slice(i, i + 20), locale)));
  }
  return marksOf(reported, explained, counts, current);
}

/** The seen lexicon codes, current error first, for the "only this mower" filter. */
export function seenList(marks: MowerMarks): string[] {
  const codes = Object.keys(marks.seen);
  if (marks.current && !codes.includes(marks.current)) codes.unshift(marks.current);
  return codes;
}

/** Append a further page, dropping entries already shown (a lexicon may change meanwhile). */
export function appendPage(
  shown: readonly ErrorExplanation[],
  next: LexiconPage,
): ErrorExplanation[] {
  const have = new Set(shown.map((e) => e.code));
  return [...shown, ...(next.entries ?? []).filter((e) => !have.has(e.code))];
}

/** The filters differ from none (the "reset" button shows then). */
export function isFiltered(filters: LexiconFilters): boolean {
  return (
    filters.query.trim() !== '' ||
    filters.category !== '' ||
    filters.severity !== '' ||
    filters.onlySeen
  );
}
