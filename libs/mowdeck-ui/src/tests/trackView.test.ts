import { describe, expect, it } from 'vitest';

import { fillZoom, intersectBoxes, marginBox, servedBox } from '../lib/map/trackView';

const TRACK = [
  { lat: 48.0, lon: 11.0 },
  { lat: 48.01, lon: 11.0 },
];

describe('marginBox', () => {
  it('adds the margin on every side like the server (2 km is about 0.018 degrees of latitude)', () => {
    const box = marginBox(TRACK, 2000)!;
    expect(box[0]).toBeCloseTo(48.0 - 0.017966, 5);
    expect(box[2]).toBeCloseTo(48.01 + 0.017966, 5);
    // Longitude degrees are shorter at 48° north, so the margin is wider in degrees.
    expect(box[1]).toBeCloseTo(11.0 - 0.017966 / Math.cos((48.005 * Math.PI) / 180), 5);
    expect(box[3]).toBeCloseTo(11.0 + 0.017966 / Math.cos((48.005 * Math.PI) / 180), 5);
  });

  it('needs points and stays within the world', () => {
    expect(marginBox([], 2000)).toBeNull();
    const pole = marginBox([{ lat: 89.99, lon: 179.99 }], 50_000)!;
    expect(pole[2]).toBe(90);
    expect(pole[3]).toBe(180);
  });
});

describe('servedBox', () => {
  it('cuts the area around the track to the coverage of the source', () => {
    const around = marginBox(TRACK, 2000)!;
    const coverage = [47.0, 10.0, 48.005, 12.0] as const;
    expect(servedBox(TRACK, 2000, coverage)).toEqual([around[0], around[1], 48.005, around[3]]);
    expect(servedBox(TRACK, 2000, null)).toEqual(around);
  });

  it('is the coverage alone without a margin, and null without either', () => {
    expect(servedBox(TRACK, 0, [47, 10, 49, 12])).toEqual([47, 10, 49, 12]);
    expect(servedBox(TRACK, undefined, null)).toBeNull();
    expect(servedBox([], 2000, null)).toBeNull();
  });

  it('grows with the track', () => {
    const small = servedBox(TRACK, 2000, null)!;
    const grown = servedBox([...TRACK, { lat: 48.2, lon: 11.3 }], 2000, null)!;
    expect(grown[2]).toBeGreaterThan(small[2]);
    expect(grown[3]).toBeGreaterThan(small[3]);
    expect(grown[0]).toBe(small[0]);
  });

  it('is null when the coverage does not overlap the track', () => {
    expect(intersectBoxes([0, 0, 1, 1], [2, 2, 3, 3])).toBeNull();
    expect(servedBox(TRACK, 2000, [52, 13, 53, 14])).toBeNull();
  });
});

describe('fillZoom', () => {
  it('is the zoom where the box covers the whole view, rounded up to the snap', () => {
    // The whole Mercator world is 256 px wide at zoom 0, so 512 px need zoom 1.
    expect(fillZoom([-85.0511287798066, -180, 85.0511287798066, 180], 512, 512)).toBe(1);
    // A quarter of the world's width is 64 px at zoom 0: 256 px need zoom 2, 300 px a bit more.
    expect(fillZoom([-10, -45, 10, 45], 256, 1, 0.25)).toBe(2);
    const quarter = fillZoom([-10, -45, 10, 45], 300, 1)!;
    expect(quarter).toBe(2.25);
    expect(fillZoom([-10, -45, 10, 45], 300, 1, 1)).toBe(3);
  });

  it('takes the side that runs out first (a tall phone needs more zoom for a wide box)', () => {
    const wide = [48.0, 10.8, 48.05, 11.4] as const;
    const landscape = fillZoom(wide, 1200, 400)!;
    const portrait = fillZoom(wide, 390, 800)!;
    expect(portrait).toBeGreaterThan(landscape);
  });

  it('is null before the map has a size', () => {
    expect(fillZoom([48, 11, 49, 12], 0, 300)).toBeNull();
  });

  it('gives zoom 13.75 for the 2 km area of a short track on a phone', () => {
    // The box is 4 km wide and 5.1 km high; at 48° north a pixel is 104.75 km / 2^z. 600 px of
    // height need 2^z >= 12 300 (z >= 13.6), 390 px of width only z >= 13.3.
    expect(fillZoom(marginBox(TRACK, 2000)!, 390, 600)).toBe(13.75);
  });
});
