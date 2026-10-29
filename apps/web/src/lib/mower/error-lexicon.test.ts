import { describe, expect, it } from 'vitest';

import type { ErrorExplanation } from './error-explain.js';
import {
  NO_FILTERS,
  PAGE_SIZE,
  appendPage,
  isFiltered,
  lexiconQuery,
  marksOf,
  reportedCodes,
  seenList,
  type LexiconPage,
} from './error-lexicon.js';

function explained(code: string, known = true): ErrorExplanation {
  return {
    code,
    vendor: 'acme',
    vendor_name: 'Acme',
    known,
    locale: 'en',
    title: `Title ${code}`,
    description: '',
    cause: null,
    steps: [],
    severity: 'warning',
    category: 'other',
    user_action: true,
    links: [],
    verified: true,
  };
}

describe('error lexicon helpers', () => {
  it('builds the page query from the filters', () => {
    expect(lexiconQuery(NO_FILTERS, ['1'], 'de')).toEqual({
      offset: 0,
      limit: PAGE_SIZE,
      locale: 'de',
    });
    const filtered = lexiconQuery(
      { query: '  lifted  ', category: 'lift_tilt', severity: 'critical', onlySeen: true },
      ['1201', '2801'],
      'en',
      50,
    );
    expect(filtered).toEqual({
      q: 'lifted',
      category: 'lift_tilt',
      severity: 'critical',
      code: ['1201', '2801'],
      offset: 50,
      limit: PAGE_SIZE,
      locale: 'en',
    });
    expect(lexiconQuery({ ...NO_FILTERS, query: 'x'.repeat(150) }, [], 'en').q).toHaveLength(100);
  });

  it('lists the reported codes, most frequent first, the current error included', () => {
    expect(reportedCodes({ a: 1, b: 5, unknown: 9, c: 0 }, null)).toEqual(['b', 'a']);
    expect(reportedCodes({ a: 1 }, 'z')).toEqual(['z', 'a']);
    expect(reportedCodes(null, 'a')).toEqual(['a']);
    expect(reportedCodes(undefined, undefined)).toEqual([]);
  });

  it('keys the marks by the lexicon spelling, merging spellings of one code', () => {
    // a vendor reports `-1201` and `1201` for the same entry; the server spells it `1201`
    const reported = ['-1201', '1201', 'E9'];
    const marks = marksOf(
      reported,
      [explained('1201'), explained('1201'), explained('E9', false)],
      { '-1201': 2, '1201': 3, E9: 1 },
      '-1201',
    );
    expect(marks).toEqual({ seen: { '1201': 5 }, current: '1201' });
    expect(seenList(marks)).toEqual(['1201']);
    expect(seenList({ seen: { a: 1 }, current: 'b' })).toEqual(['b', 'a']);
  });

  it('appends a page without repeating entries and tells when filters are set', () => {
    const next = { entries: [explained('2'), explained('3')] } as LexiconPage;
    expect(appendPage([explained('1'), explained('2')], next).map((e) => e.code)).toEqual([
      '1',
      '2',
      '3',
    ]);
    expect(isFiltered(NO_FILTERS)).toBe(false);
    expect(isFiltered({ ...NO_FILTERS, query: ' ' })).toBe(false);
    expect(isFiltered({ ...NO_FILTERS, severity: 'info' })).toBe(true);
    expect(isFiltered({ ...NO_FILTERS, onlySeen: true })).toBe(true);
  });
});
