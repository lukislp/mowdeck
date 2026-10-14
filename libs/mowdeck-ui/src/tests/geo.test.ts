import { describe, expect, it } from 'vitest';

import { compassHeading, fromWgs84, toWgs84 } from '../lib/map/geo';
import { createProjection, normalizeDeg } from '../lib/map/projection';

import vectors from './fixtures/geo-vectors.json';

/** Golden vectors printed by scripts/geo_vectors.py from the Python mowdeck_control.geo. */
interface Vectors {
  source: string;
  cases: {
    reference: { origin_lat: number; origin_lon: number; rotation_deg: number };
    points: { x: number; y: number; lat: number; lon: number; bx: number; by: number }[];
    headings: { angle_rad: number; heading: number }[];
  }[];
}

const golden = vectors as Vectors;
const DEG_TOLERANCE = 1e-9; // well inside the required 1e-6 degrees
const M_TOLERANCE = 1e-6;

describe('geo port against mowdeck_control.geo', () => {
  it('has vectors for every reference', () => {
    expect(golden.source).toBe('mowdeck_control.geo');
    expect(golden.cases.length).toBe(16);
  });

  for (const c of golden.cases) {
    const ref = c.reference;
    const name = `origin ${ref.origin_lat},${ref.origin_lon} rotation ${ref.rotation_deg}`;

    it(`to_wgs84 matches (${name})`, () => {
      for (const p of c.points) {
        const [lat, lon] = toWgs84(ref, p.x, p.y);
        expect(Math.abs(lat - p.lat)).toBeLessThan(DEG_TOLERANCE);
        expect(Math.abs(lon - p.lon)).toBeLessThan(DEG_TOLERANCE);
      }
    });

    it(`from_wgs84 matches and inverts (${name})`, () => {
      for (const p of c.points) {
        const [x, y] = fromWgs84(ref, p.lat, p.lon);
        expect(Math.abs(x - p.bx)).toBeLessThan(M_TOLERANCE);
        expect(Math.abs(y - p.by)).toBeLessThan(M_TOLERANCE);
        expect(Math.abs(x - p.x)).toBeLessThan(M_TOLERANCE);
        expect(Math.abs(y - p.y)).toBeLessThan(M_TOLERANCE);
      }
    });

    it(`compass_heading matches (${name})`, () => {
      for (const h of c.headings) {
        const heading = compassHeading(ref, h.angle_rad);
        const diff = Math.abs(heading - h.heading);
        expect(Math.min(diff, 360 - diff)).toBeLessThan(DEG_TOLERANCE);
      }
    });
  }

  it('treats a missing rotation as zero', () => {
    const [lat, lon] = toWgs84({ origin_lat: 48, origin_lon: 11 }, 0, 0);
    expect([lat, lon]).toEqual([48, 11]);
  });
});

describe('projection adapter', () => {
  const ref = { origin_lat: 48.137154, origin_lon: 11.576124, rotation_deg: 12.5 };

  it('local mode keeps metres: [lat, lng] is [y, x]', () => {
    const p = createProjection(ref, 'local');
    expect(p.mode).toBe('local');
    expect(p.fromLocal(12, 8)).toEqual([8, 12]);
    expect(p.toLocal([8, 12])).toEqual([12, 8]);
  });

  it('local mode converts WGS84 positions back to metres', () => {
    const p = createProjection(ref, 'local');
    const [lat, lon] = toWgs84(ref, -1, 1);
    const [y, x] = p.fromWgs84(lat, lon);
    expect(x).toBeCloseTo(-1, 9);
    expect(y).toBeCloseTo(1, 9);
  });

  it('geo mode converts metres to WGS84 and back', () => {
    const p = createProjection(ref, 'geo');
    const coord = p.fromLocal(10, 17);
    expect(coord).toEqual(toWgs84(ref, 10, 17));
    expect(p.fromWgs84(48.1, 11.5)).toEqual([48.1, 11.5]);
    const [x, y] = p.toLocal(coord);
    expect(x).toBeCloseTo(10, 9);
    expect(y).toBeCloseTo(17, 9);
  });

  it('rotates north and headings by the reference rotation in local mode', () => {
    const p = createProjection(ref, 'local');
    expect(p.northScreen).toBeCloseTo(12.5);
    // Compass north points 12.5 degrees clockwise from screen up.
    expect(p.headingToScreen(0)).toBeCloseTo(12.5);
    expect(p.headingToScreen(350)).toBeCloseTo(2.5);
    // Local +x is screen right, local +y is screen up.
    expect(p.localAngleToScreen(0)).toBeCloseTo(90);
    expect(p.localAngleToScreen(90)).toBeCloseTo(0);
  });

  it('a local direction lands on the same screen angle in both modes', () => {
    const local = createProjection(ref, 'local');
    const geo = createProjection(ref, 'geo');
    expect(geo.northScreen).toBe(0);
    for (const deg of [0, 45, 90, 180, 270]) {
      // In geo mode the screen is north up, so the screen angle is the compass heading; in
      // local mode the frame is rotated, so the same direction differs by the rotation.
      const viaHeading = local.headingToScreen(geo.localAngleToScreen(deg));
      expect(normalizeDeg(viaHeading - local.localAngleToScreen(deg) + 0.5) - 0.5).toBeCloseTo(
        0,
        9,
      );
    }
  });

  it('normalises angles to [0, 360)', () => {
    expect(normalizeDeg(-90)).toBe(270);
    expect(normalizeDeg(720)).toBe(0);
    expect(normalizeDeg(359.5)).toBe(359.5);
  });
});
