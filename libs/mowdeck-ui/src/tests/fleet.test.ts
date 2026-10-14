import { describe, expect, it } from 'vitest';

import {
  FLEET_TONES,
  fleetBounds,
  fleetGeometry,
  fleetGrid,
  fleetLegend,
  fleetMarkers,
  fleetReference,
  fleetTone,
  gridStep,
  unplaced,
  type FleetMower,
} from '../lib/map/fleet';
import { fromWgs84 } from '../lib/map/geo';
import { createProjection } from '../lib/map/projection';
import { scaleFor } from '../lib/map/scale';

const at = (lat: number, lon: number, activity = 'docked', heading?: number) => ({
  activity,
  position: { lat, lon, heading: heading ?? null },
});

const MOWERS: FleetMower[] = [
  { id: 'a', name: 'Luba Nord', state: at(48.0, 11.0, 'mowing', 90) },
  { id: 'b', name: 'Luba Süd', state: at(48.01, 11.02, 'error') },
  { id: 'c', name: 'Yuka', state: at(48.005, 11.01, 'idle'), stale: true },
  { id: 'd', name: 'Neu', state: null },
  { id: 'e', name: 'Offline', state: at(48.002, 11.004, 'offline') },
  { id: 'f', name: 'Ohne Position', state: { activity: 'docked', position: null } },
];

describe('fleet model', () => {
  it('maps activities to tones and counts them in legend order', () => {
    expect(fleetTone(null)).toBe('unknown');
    expect(fleetTone({ activity: 'idle' })).toBe('docked');
    expect(fleetTone({ activity: 'dancing' })).toBe('unknown');
    expect(fleetLegend(MOWERS)).toEqual([
      { tone: 'error', count: 1 },
      { tone: 'mowing', count: 1 },
      { tone: 'docked', count: 2 },
      { tone: 'offline', count: 1 },
      { tone: 'unknown', count: 1 },
    ]);
    expect(FLEET_TONES[0]).toBe('error');
    expect(fleetLegend([])).toEqual([]);
  });

  it('lists mowers without a position separately', () => {
    expect(unplaced(MOWERS).map((m) => m.id)).toEqual(['d', 'f']);
  });

  it('centres the reference on the bounding box of positions and map origins', () => {
    expect(fleetReference([])).toBeNull();
    const ref = fleetReference(MOWERS);
    expect(ref?.origin_lat).toBeCloseTo(48.005, 9);
    expect(ref?.origin_lon).toBeCloseTo(11.01, 9);
    const withMap = fleetReference([
      { id: 'x', name: 'x', state: null, map: { reference: { origin_lat: 50, origin_lon: 8 } } },
    ]);
    expect(withMap).toEqual({ origin_lat: 50, origin_lon: 8 });
  });

  it('builds markers in WGS84 with tone, heading and hollow state', () => {
    const projection = createProjection({ origin_lat: 48, origin_lon: 11 }, 'geo');
    const markers = fleetMarkers(MOWERS, projection);
    expect(markers.map((m) => m.id)).toEqual(['a', 'b', 'c', 'e']);
    const [north, south, stale, offline] = markers;
    expect(north).toMatchObject({ at: [48, 11], tone: 'mowing', rotation: 90, hollow: false });
    expect(south).toMatchObject({ tone: 'error', rotation: null, hollow: false });
    expect(stale).toMatchObject({ tone: 'docked', hollow: true });
    expect(offline).toMatchObject({ tone: 'offline', hollow: true });
  });

  it('draws the areas and docks of loaded maps in each mower’s own frame', () => {
    const reference = { origin_lat: 48.1, origin_lon: 11.5 };
    const geometry = fleetGeometry([
      {
        id: 'a',
        name: 'a',
        state: null,
        map: {
          reference,
          areas: [
            {
              id: 'front',
              boundary: [
                [0, 0],
                [10, 0],
                [10, 10],
              ],
            },
          ],
          dock: { x: 0, y: 0, heading_deg: 0 },
        },
      },
      { id: 'b', name: 'b', state: null },
    ]);
    expect(geometry.areas).toHaveLength(1);
    expect(geometry.areas[0]?.key).toBe('a:front');
    expect(geometry.areas[0]?.ring[0]).toEqual([48.1, 11.5]);
    expect(geometry.docks).toEqual([{ mowerId: 'a', at: [48.1, 11.5], rotation: 90 }]);
  });

  it('bounds markers and areas', () => {
    expect(fleetBounds([])).toBeNull();
    const projection = createProjection({ origin_lat: 48, origin_lon: 11 }, 'geo');
    const bounds = fleetBounds(fleetMarkers(MOWERS, projection), [
      { key: 'k', mowerId: 'a', ring: [[47.9, 11.5]] },
    ]);
    expect(bounds).toEqual([
      [47.9, 11],
      [48.01, 11.5],
    ]);
  });

  it('picks 1-2-5 grid steps for the visible span', () => {
    expect(gridStep(0)).toBe(1);
    expect(gridStep(Number.NaN)).toBe(1);
    expect(gridStep(8)).toBe(1);
    expect(gridStep(30)).toBe(5);
    expect(gridStep(150)).toBe(20);
    expect(gridStep(4_000)).toBe(500);
    expect(gridStep(40_000, 8)).toBe(5_000);
  });

  it('draws a metric grid with every fifth line major', () => {
    const ref = { origin_lat: 48, origin_lon: 11 };
    const projection = createProjection(ref, 'geo');
    const grid = fleetGrid([-12, -3, 37, 21], projection, 10);
    // span 49 m: step 5, x from -15 to 40 (12 lines), y from -5 to 25 (7 lines)
    expect(grid).toHaveLength(19);
    const majors = grid.filter((line) => line.major);
    expect(majors).toHaveLength(4); // x = 0 and 25, y = 0 and 25 (major every 25 m)
    const first = grid[0];
    expect(first && fromWgs84(ref, ...first.from)[0]).toBeCloseTo(-15, 6);
  });

  it('formats the scale bar', () => {
    expect(scaleFor(0)).toEqual({ width: 0, label: '' });
    expect(scaleFor(1)).toEqual({ width: 50, label: '50 m' });
    expect(scaleFor(0.01)).toEqual({ width: 50, label: '50 cm' });
    expect(scaleFor(30)).toEqual({ width: 67, label: '2 km' });
  });
});
