import { describe, expect, it } from 'vitest';

import { m } from '#lib/paraglide/messages.js';

import { areaLabel, areaNamer, fleetAreaNamer, isPlaceholderName } from './map-labels';

describe('areaLabel', () => {
  it('keeps real names and shortens digit-only hashes to their last four digits', () => {
    expect(areaLabel({ id: 'front', name: 'Front lawn' })).toBe('Front lawn');
    expect(areaLabel({ id: 'a1', name: '4525245168006937683' })).toBe(
      m.area_unnamed({ id: '7683' }),
    );
    expect(areaLabel({ id: 'a1', name: ' 4525245168006937683 ' })).toBe(
      m.area_unnamed({ id: '7683' }),
    );
    // Short numbers are real names (e.g. "2").
    expect(areaLabel({ id: 'a1', name: '2' })).toBe('2');
  });

  it('falls back to the id, shortened when it is a hash', () => {
    expect(areaLabel({ id: 'back' })).toBe(m.area_unnamed({ id: 'back' }));
    expect(areaLabel({ id: '4525245168006937683', name: null })).toBe(
      m.area_unnamed({ id: '7683' }),
    );
    expect(areaLabel({ id: 'x', name: '  ' })).toBe(m.area_unnamed({ id: 'x' }));
  });
});

describe('areaLabel with custom names and the map order', () => {
  it('prefers the custom name, then the own name, then "Area N"', () => {
    const overrides = { a1: '  Vorgarten ', a3: '   ' };
    expect(areaLabel({ id: 'a1', name: 'Front' }, { overrides, index: 0 })).toBe('Vorgarten');
    // A blank custom name counts as none.
    expect(areaLabel({ id: 'a3', name: 'Back' }, { overrides, index: 2 })).toBe('Back');
    expect(areaLabel({ id: 'a2', name: '4525245168006937683' }, { overrides, index: 1 })).toBe(
      m.area_numbered({ n: 2 }),
    );
    expect(areaLabel({ id: 'a4' }, { index: 3 })).toBe(m.area_numbered({ n: 4 }));
  });

  it('keeps the legacy fallback without an index', () => {
    expect(areaLabel({ id: '4525245168006937683' }, { overrides: null })).toBe(
      m.area_unnamed({ id: '7683' }),
    );
    expect(areaLabel({ id: 'x' }, { index: null })).toBe(m.area_unnamed({ id: 'x' }));
  });

  it('isPlaceholderName treats blanks and long numbers as no name', () => {
    expect(isPlaceholderName(undefined)).toBe(true);
    expect(isPlaceholderName(' 12345678 ')).toBe(true);
    expect(isPlaceholderName('1234567')).toBe(false);
    expect(isPlaceholderName('Front')).toBe(false);
  });
});

describe('areaNamer', () => {
  const areas = [
    { id: 'h1', name: '4525245168006937683' },
    { id: 'h2', name: 'Beet' },
    { id: 'h3', name: null },
  ];

  it('numbers unnamed areas by their position in the map, stable when others are renamed', () => {
    const plain = areaNamer(areas);
    expect(areas.map(plain)).toEqual([
      m.area_numbered({ n: 1 }),
      'Beet',
      m.area_numbered({ n: 3 }),
    ]);
    const renamed = areaNamer(areas, { h1: 'Rasen' });
    expect(areas.map(renamed)).toEqual(['Rasen', 'Beet', m.area_numbered({ n: 3 })]);
  });

  it('falls back to areaLabel for areas missing from the map', () => {
    expect(areaNamer(null)({ id: 'zz' })).toBe(m.area_unnamed({ id: 'zz' }));
    expect(areaNamer(areas, { zz: 'Alt' })({ id: 'zz' })).toBe('Alt');
  });
});

describe('fleetAreaNamer', () => {
  it('names each area against its own mower', () => {
    const name = fleetAreaNamer([
      { map: { areas: [{ id: 'a' }, { id: 'b' }] }, area_names: { b: 'Hof' } },
      { map: { areas: [{ id: 'c' }] }, area_names: undefined },
      { map: null },
    ]);
    expect(name({ id: 'a' })).toBe(m.area_numbered({ n: 1 }));
    expect(name({ id: 'b' })).toBe('Hof');
    expect(name({ id: 'c' })).toBe(m.area_numbered({ n: 1 }));
    expect(name({ id: 'other' })).toBe(m.area_unnamed({ id: 'other' }));
  });
});
