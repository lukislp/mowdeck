import { describe, expect, it } from 'vitest';

import { bulkTargets, lineStates, mergeAnswers, outcomeCounts } from './bulk';
import {
  addressLine,
  filterMowers,
  groupBody,
  groupForm,
  passesQuick,
  sectionsBySite,
  siteBody,
  siteForm,
  sortSites,
  toggleGroup,
  validateSite,
  type SiteLike,
} from './places';

const SITES: SiteLike[] = [
  { id: 's2', name: 'Werk Süd', timezone: 'Europe/Berlin', is_default: false },
  { id: 's1', name: 'Zuhause', timezone: 'Europe/Berlin', is_default: true },
  { id: 's3', name: 'Halle', timezone: 'Europe/Vienna', is_default: false },
];

const MOWERS = [
  { id: 'm1', name: 'Luba', site_id: 's1', group_ids: ['g1'] },
  { id: 'm2', name: 'Yuka', site_id: 's2', group_ids: [] },
  { id: 'm3', name: 'Fremd', site_id: 'hidden', group_ids: ['g1'] },
];

describe('site form', () => {
  it('starts from a site or from the device zone', () => {
    expect(siteForm(null, 'Europe/Berlin')).toMatchObject({ name: '', timezone: 'Europe/Berlin' });
    const form = siteForm(
      {
        ...SITES[0]!,
        address: { street: 'Weg 1', postal_code: '80331', city: 'München', country: 'DE' },
        latitude: 48.1,
        longitude: 11.5,
      },
      'UTC',
    );
    expect(form).toMatchObject({
      name: 'Werk Süd',
      street: 'Weg 1',
      postalCode: '80331',
      city: 'München',
      country: 'DE',
      latitude: '48.1',
      longitude: '11.5',
    });
  });

  it('checks name, zone, country and coordinates', () => {
    const base = siteForm(null, 'Europe/Berlin');
    expect(validateSite({ ...base, name: 'Hof' })).toEqual({});
    expect(validateSite(base)).toEqual({ name: { code: 'required' } });
    expect(validateSite(base, true)).toEqual({});
    expect(validateSite({ ...base, name: 'x', timezone: 'Mars/Base' }).timezone).toEqual({
      code: 'timezone',
    });
    expect(validateSite({ ...base, name: 'x', timezone: ' ' }).timezone).toEqual({
      code: 'required',
    });
    expect(validateSite({ ...base, name: 'x', country: 'DEU' }).country).toEqual({
      code: 'country',
    });
    expect(validateSite({ ...base, name: 'x', latitude: '91', longitude: '0' }).latitude).toEqual({
      code: 'range',
      min: -90,
      max: 90,
    });
    expect(validateSite({ ...base, name: 'x', latitude: '1', longitude: 'abc' }).longitude).toEqual(
      { code: 'range', min: -180, max: 180 },
    );
    expect(validateSite({ ...base, name: 'x', latitude: '48,1' })).toEqual({
      longitude: { code: 'both' },
    });
    expect(validateSite({ ...base, name: 'x', longitude: '11' })).toEqual({
      latitude: { code: 'both' },
    });
  });

  it('builds the request body', () => {
    const form = {
      ...siteForm(null, 'Europe/Berlin'),
      name: ' Hof ',
      city: ' Ulm ',
      country: 'de',
      latitude: '48,4',
      longitude: '9.99',
    };
    expect(siteBody(form)).toEqual({
      name: 'Hof',
      timezone: 'Europe/Berlin',
      address: { street: null, postal_code: null, city: 'Ulm', country: 'DE' },
      latitude: 48.4,
      longitude: 9.99,
    });
    const plain = siteBody({ ...siteForm(null, 'UTC'), name: 'x' }, true);
    expect(plain).toEqual({ timezone: 'UTC', address: null, latitude: null, longitude: null });
  });

  it('formats addresses and orders sites with the default first', () => {
    expect(addressLine(null)).toBeNull();
    expect(addressLine({})).toBeNull();
    expect(addressLine({ street: 'Weg 1', postal_code: '89073', city: 'Ulm', country: 'DE' })).toBe(
      'Weg 1, 89073 Ulm, DE',
    );
    expect(sortSites(SITES).map((s) => s.id)).toEqual(['s1', 's3', 's2']);
  });
});

describe('groups and placement', () => {
  it('builds group bodies (empty site: cross-site)', () => {
    expect(groupForm(null)).toEqual({ name: '', siteId: '' });
    expect(groupForm({ id: 'g', name: 'Nord', site_id: 's1' })).toEqual({
      name: 'Nord',
      siteId: 's1',
    });
    expect(groupBody({ name: ' Nord ', siteId: '' })).toEqual({ name: 'Nord', site_id: null });
  });

  it('toggles a group in a mower’s set', () => {
    expect(toggleGroup(['g1', 'g2'], 'g1', false)).toEqual(['g2']);
    expect(toggleGroup(['g2'], 'g1', true)).toEqual(['g2', 'g1']);
    expect(toggleGroup(['g1'], 'g1', true)).toEqual(['g1']);
  });

  it('sections mowers by site in site order with unknown sites last', () => {
    const sections = sectionsBySite(MOWERS, SITES);
    expect(sections.map((s) => [s.id, s.items.map((m) => m.id)])).toEqual([
      ['s1', ['m1']],
      ['s2', ['m2']],
      ['', ['m3']],
    ]);
  });

  it('filters by site and group', () => {
    expect(filterMowers(MOWERS, { siteId: '', groupId: '' })).toHaveLength(3);
    expect(filterMowers(MOWERS, { siteId: 's1', groupId: '' }).map((m) => m.id)).toEqual(['m1']);
    expect(filterMowers(MOWERS, { siteId: '', groupId: 'g1' }).map((m) => m.id)).toEqual([
      'm1',
      'm3',
    ]);
  });

  it('applies the quick filters', () => {
    expect(passesQuick('all', null, false)).toBe(true);
    expect(passesQuick('active', 'paused', false)).toBe(true);
    expect(passesQuick('active', 'docked', false)).toBe(false);
    expect(passesQuick('attention', 'docked', true)).toBe(true);
    expect(passesQuick('attention', null, false)).toBe(true);
    expect(passesQuick('attention', 'error', false)).toBe(true);
    expect(passesQuick('attention', 'mowing', false)).toBe(false);
    expect(passesQuick('docked', 'charging', false)).toBe(true);
    expect(passesQuick('docked', 'idle', false)).toBe(true);
  });
});

describe('bulk commands', () => {
  it('targets the group, else the site, else every visible site', () => {
    expect(bulkTargets({ siteId: 's1', groupId: 'g1' }, ['s1', 's2'])).toEqual([
      { type: 'group', id: 'g1' },
    ]);
    expect(bulkTargets({ siteId: 's1', groupId: '' }, ['s1', 's2'])).toEqual([
      { type: 'site', id: 's1' },
    ]);
    expect(bulkTargets({ siteId: '', groupId: '' }, ['s1', 's2'])).toEqual([
      { type: 'site', id: 's1' },
      { type: 'site', id: 's2' },
    ]);
  });

  it('merges answers per mower and keeps an accepted outcome', () => {
    const merged = mergeAnswers([
      { items: [{ mower_id: 'm1', outcome: 'forbidden' }] },
      {
        items: [
          { mower_id: 'm1', outcome: 'accepted', command_id: 'c1' },
          { mower_id: 'm2', outcome: 'not_published' },
        ],
      },
      { items: [{ mower_id: 'm2', outcome: 'forbidden' }] },
    ]);
    expect(merged.get('m1')).toMatchObject({ outcome: 'accepted', command_id: 'c1' });
    expect(merged.get('m2')?.outcome).toBe('not_published');
  });

  it('gives each affected mower a line state and counts them', () => {
    expect(lineStates(['m1'], null)).toEqual({ m1: 'pending' });
    const merged = mergeAnswers([{ items: [{ mower_id: 'm1', outcome: 'accepted' }] }]);
    const states = lineStates(['m1', 'm2'], merged);
    expect(states).toEqual({ m1: 'accepted', m2: 'missing' });
    expect(lineStates(['m2'], merged, true)).toEqual({ m2: 'failed' });
    expect(outcomeCounts(states)).toMatchObject({ accepted: 1, missing: 1, forbidden: 0 });
  });
});
