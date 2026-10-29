import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ErrorExplanation } from '#lib/mower/error-explain.js';
import type { LexiconFilters, LexiconPage } from '#lib/mower/error-lexicon.js';
import { setLocale } from '#lib/paraglide/runtime.js';

const calls: { filters: LexiconFilters; seen: readonly string[]; offset: number }[] = [];
let pages: LexiconPage[] = [];

vi.mock('#lib/mower/error-lexicon.js', async (original) => ({
  ...(await original<typeof import('#lib/mower/error-lexicon.js')>()),
  browseLexicon: vi.fn(
    (_id: string, filters: LexiconFilters, seen: readonly string[], _l: string, offset = 0) => {
      calls.push({ filters: { ...filters }, seen: [...seen], offset });
      // every filter change asks again: the first page for offset 0, the second for more
      return Promise.resolve((offset === 0 ? pages[0] : (pages[1] ?? pages[0]))!);
    },
  ),
  loadMarks: vi.fn(() => Promise.resolve({ seen: { stuck: 3 }, current: 'lifted' })),
}));
vi.mock('#lib/mower/error-explain.js', async (original) => ({
  ...(await original<typeof import('#lib/mower/error-explain.js')>()),
  explainMowerCodes: vi.fn((_id: string, codes: string[]) =>
    Promise.resolve(codes.map((code) => entry(code, code === 'lifted' ? 'critical' : 'warning'))),
  ),
}));

import ErrorLexiconPanel from './ErrorLexiconPanel.svelte';

function entry(
  code: string,
  severity: ErrorExplanation['severity'] = 'warning',
  verified = true,
): ErrorExplanation {
  return {
    code,
    vendor: 'simulated',
    vendor_name: 'Simulator',
    known: true,
    locale: 'de',
    title: code === 'lifted' ? 'Angehoben' : `Titel ${code}`,
    description: 'Beschreibung',
    cause: 'Ursache',
    steps: ['Schritt eins'],
    severity,
    category: code === 'lifted' ? 'lift_tilt' : 'trapped',
    user_action: true,
    links: [],
    verified,
  };
}

function page(entries: ErrorExplanation[], total = entries.length, offset = 0): LexiconPage {
  return {
    lexicon: {
      vendor: 'simulated',
      vendor_name: 'Simulator',
      connection_kinds: ['simulated'],
      source: null,
      entries: 3,
      unverified: 1,
      locales: ['de', 'en'],
    },
    total,
    offset,
    entries,
    categories: { lift_tilt: 1, trapped: 2 },
    severities: { critical: 1, warning: 2 },
  };
}

beforeEach(() => {
  setLocale('de', { reload: false });
  calls.length = 0;
  pages = [];
});

describe('ErrorLexiconPanel', () => {
  it('lists the lexicon with the mower marks, pins the current error and pages on', async () => {
    pages = [
      page([entry('lifted', 'critical'), entry('stuck'), entry('E7', 'warning', false)], 4),
      page([entry('Z9')], 4, 3),
    ];
    render(ErrorLexiconPanel, {
      props: { mowerId: 'm1', currentCode: 'lifted', errorCounts: { stuck: 3 } },
    });
    const rows = await screen.findAllByTestId('lexicon-entry');
    expect(rows.map((r) => r.dataset.code)).toEqual(['lifted', 'stuck', 'E7']);
    expect(rows[0]!.dataset.current).toBe('true');
    expect(rows[1]!.dataset.seen).toBe('true');
    expect(rows[1]!.textContent).toContain('3× bei diesem Mäher');
    expect(rows[2]!.dataset.seen).toBe('false');
    expect(screen.getAllByTestId('lexicon-unverified')).toHaveLength(1);
    expect(screen.getByTestId('lexicon-summary').textContent).toContain('3 Codes');
    expect(screen.getByTestId('lexicon-count').textContent).toContain('3 von 4');
    const pinned = await screen.findByTestId('lexicon-current');
    expect(pinned.textContent).toContain('Angehoben');
    // a row opens to the full explanation
    expect(rows[1]!.querySelector('[data-testid="error-steps"]')).toBeTruthy();
    await fireEvent.click(screen.getByTestId('lexicon-more'));
    await waitFor(() => expect(screen.getAllByTestId('lexicon-entry')).toHaveLength(4));
    expect(calls.at(-1)?.offset).toBe(3);
    expect(screen.queryByTestId('lexicon-more')).toBeNull();
  });

  it('filters by severity and to the codes this mower reported', async () => {
    pages = [page([entry('lifted', 'critical'), entry('stuck')])];
    render(ErrorLexiconPanel, { props: { mowerId: 'm1', errorCounts: { stuck: 3 } } });
    await screen.findAllByTestId('lexicon-entry');
    const critical = screen
      .getByTestId('lexicon-severity')
      .querySelector<HTMLButtonElement>('[data-value="critical"]')!;
    expect(critical.textContent).toContain('1');
    await fireEvent.click(critical);
    await waitFor(() => expect(calls.at(-1)?.filters.severity).toBe('critical'));
    expect(critical.getAttribute('aria-pressed')).toBe('true');
    await fireEvent.click(screen.getByTestId('lexicon-only-seen'));
    await waitFor(() => expect(calls.at(-1)?.filters.onlySeen).toBe(true));
    expect(calls.at(-1)?.seen).toEqual(['lifted', 'stuck']);
    await fireEvent.click(screen.getByTestId('lexicon-reset'));
    await waitFor(() => expect(calls.at(-1)?.filters.severity).toBe(''));
  });

  it('says so when no lexicon applies', async () => {
    pages = [{ ...page([]), lexicon: null, total: 0 }];
    render(ErrorLexiconPanel, { props: { mowerId: 'm1' } });
    const note = await screen.findByTestId('lexicon-unavailable');
    expect(note.textContent).toContain('noch keine Liste');
  });
});
