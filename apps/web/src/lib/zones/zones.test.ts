import { describe, expect, it } from 'vitest';

import {
  currentZoneName,
  draftProblemText,
  formatZoneTime,
  timeShares,
  undoCorner,
  zoneMap,
  zonesShown,
  type ZonesOverview,
} from './zones';

const overview = (changes: Partial<ZonesOverview> = {}): ZonesOverview => ({
  availability: 'available',
  zones: [],
  presence: null,
  zone_plans: [],
  anchor: { lat: 52.5, lon: 13.4 },
  limits: {
    max_zones: 32,
    min_corners: 3,
    max_corners: 64,
    min_area_m2: 1,
    max_area_m2: 100000,
    max_extent_m: 1000,
    max_anchor_distance_m: 1000,
    max_name_length: 60,
  },
  ...changes,
});

const zone = (id: string, name: string) => ({
  id,
  name,
  ring: [
    [52.5, 13.4],
    [52.5, 13.401],
    [52.501, 13.401],
  ],
  area_m2: 100,
  start_plan_ref: null,
  created_at: '2026-10-30T10:00:00Z',
  updated_at: '2026-10-30T10:00:00Z',
});

describe('zonesShown', () => {
  it('follows the availability the server decided, and keeps existing zones visible', () => {
    expect(zonesShown(null)).toBe(false);
    expect(zonesShown(overview())).toBe(true);
    expect(zonesShown(overview({ availability: 'vendor_map' }))).toBe(false);
    expect(zonesShown(overview({ availability: 'no_position' }))).toBe(false);
    expect(zonesShown(overview({ availability: 'vendor_map', zones: [zone('a', 'A')] }))).toBe(
      true,
    );
  });
});

describe('zoneMap', () => {
  it('uses the mower snapshot when it is anchored, else the server anchor', () => {
    const snapshot = { reference: { origin_lat: 1, origin_lon: 2 }, areas: [] };
    expect(zoneMap(snapshot, { lat: 5, lon: 6 })).toBe(snapshot);
    const empty = { reference: { origin_lat: 0, origin_lon: 0 }, areas: [] };
    expect(zoneMap(empty, { lat: 5, lon: 6 })?.reference).toEqual({
      origin_lat: 5,
      origin_lon: 6,
      rotation_deg: 0,
    });
    expect(zoneMap(null, null)).toBeNull();
  });
});

describe('formatZoneTime', () => {
  it('reads like a stopwatch', () => {
    expect(formatZoneTime(0)).toBe('0 min');
    expect(formatZoneTime(Number.NaN)).toBe('0 min');
    expect(formatZoneTime(30)).toBe('< 1 min');
    expect(formatZoneTime(12 * 60)).toBe('12 min');
    expect(formatZoneTime(2 * 3600 + 5 * 60)).toBe('2 h 05 min');
  });
});

describe('timeShares', () => {
  it('scales bars to the zone with the most time', () => {
    const shares = timeShares([
      { zone_id: 'a', seconds: 100 },
      { zone_id: 'b', seconds: 50 },
      { zone_id: 'c', seconds: 0 },
    ]);
    expect(shares.get('a')).toBe(1);
    expect(shares.get('b')).toBe(0.5);
    expect(shares.get('c')).toBe(0);
    expect(timeShares([{ zone_id: 'a', seconds: 0 }]).get('a')).toBe(0);
  });
});

describe('currentZoneName', () => {
  it('names the zone the mower is in, else null', () => {
    const base = overview({ zones: [zone('a', 'Front')] });
    expect(currentZoneName(base)).toBeNull();
    expect(
      currentZoneName({
        ...base,
        presence: {
          zone_id: 'a',
          zone_name: null,
          since: '2026-10-30T10:00:00Z',
          observed_at: '2026-10-30T10:00:00Z',
        },
      }),
    ).toBe('Front');
    expect(
      currentZoneName({
        ...base,
        presence: {
          zone_id: null,
          since: '2026-10-30T10:00:00Z',
          observed_at: '2026-10-30T10:00:00Z',
        },
      }),
    ).toBeNull();
  });
});

describe('drawing helpers', () => {
  it('undoes the last corner and explains every problem', () => {
    expect(
      undoCorner([
        [0, 0],
        [1, 1],
      ]),
    ).toEqual([[0, 0]]);
    expect(undoCorner([])).toEqual([]);
    for (const problem of ['corners', 'crossing', 'small', 'large', 'many'] as const) {
      expect(draftProblemText(problem).length).toBeGreaterThan(5);
    }
  });
});
