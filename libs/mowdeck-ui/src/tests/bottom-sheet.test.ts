import { describe, expect, it } from 'vitest';
import {
  FLING_PX_PER_MS,
  MIN_UNCOVERED_PX,
  PEEK_MIN_PX,
  allowedSnaps,
  clampHeight,
  coerceSnap,
  nearestSnap,
  nextSnap,
  releaseSnap,
  releaseVelocity,
  snapHeights,
  type SnapPoint,
} from '../lib/layout/bottom-sheet';

const ALL: SnapPoint[] = ['peek', 'half', 'full'];

describe('snapHeights', () => {
  it('peek as measured, half 45 %, full 90 % of the available height', () => {
    expect(snapHeights(800, 160)).toEqual({ peek: 160, half: 360, full: 720 });
  });

  it('full always leaves the map uncovered above the sheet', () => {
    const heights = snapHeights(400, 160);
    expect(heights.full).toBe(400 - MIN_UNCOVERED_PX);
    expect(heights.half).toBe(180);
  });

  it('grows from peek to full and keeps a minimum peek', () => {
    expect(snapHeights(800, 40).peek).toBe(PEEK_MIN_PX);
    const tight = snapHeights(300, 220);
    expect(tight.peek).toBeLessThanOrEqual(tight.half);
    expect(tight.half).toBeLessThanOrEqual(tight.full);
    expect(tight.full).toBe(300 - MIN_UNCOVERED_PX);
  });

  it('never goes negative on a tiny viewport', () => {
    expect(snapHeights(20, 160)).toEqual({ peek: 0, half: 0, full: 0 });
  });
});

describe('allowed snaps', () => {
  it('defaults to all three in order and always keeps peek', () => {
    expect(allowedSnaps(undefined)).toEqual(ALL);
    expect(allowedSnaps(['full', 'half'])).toEqual(ALL);
    expect(allowedSnaps(['half'])).toEqual(['peek', 'half']);
    expect(allowedSnaps([])).toEqual(ALL);
  });

  it('a snap that is not allowed falls back to the next lower one', () => {
    expect(coerceSnap('full', ['peek', 'half'])).toBe('half');
    expect(coerceSnap('full', ['peek'])).toBe('peek');
    expect(coerceSnap('half', ALL)).toBe('half');
  });
});

describe('nextSnap (tap on the handle)', () => {
  it('cycles peek, half, full and back to peek', () => {
    expect(nextSnap('peek', ALL)).toBe('half');
    expect(nextSnap('half', ALL)).toBe('full');
    expect(nextSnap('full', ALL)).toBe('peek');
  });

  it('skips snaps that are not allowed', () => {
    expect(nextSnap('peek', ['peek', 'half'])).toBe('half');
    expect(nextSnap('half', ['peek', 'half'])).toBe('peek');
    expect(nextSnap('peek', ['peek'])).toBe('peek');
  });
});

describe('drag release', () => {
  const heights = snapHeights(800, 160); // 160, 360, 720

  it('clamps the drag between the lowest and highest allowed snap', () => {
    expect(clampHeight(50, heights, ALL)).toBe(160);
    expect(clampHeight(900, heights, ALL)).toBe(720);
    expect(clampHeight(500, heights, ['peek', 'half'])).toBe(360);
  });

  it('a slow release goes to the nearest snap', () => {
    expect(nearestSnap(250, heights, ALL)).toBe('peek');
    expect(nearestSnap(270, heights, ALL)).toBe('half');
    expect(nearestSnap(600, heights, ALL)).toBe('full');
    expect(releaseSnap(600, 0.1, heights, ALL)).toBe('full');
    expect(releaseSnap(600, -0.1, heights, ALL)).toBe('full');
  });

  it('a fling goes on to the next snap in its direction', () => {
    const fast = FLING_PX_PER_MS + 0.1;
    expect(releaseSnap(180, fast, heights, ALL)).toBe('half');
    expect(releaseSnap(380, fast, heights, ALL)).toBe('full');
    expect(releaseSnap(700, -fast, heights, ALL)).toBe('half');
    expect(releaseSnap(340, -fast, heights, ALL)).toBe('peek');
  });

  it('a fling past the last snap stays there', () => {
    const fast = FLING_PX_PER_MS * 3;
    expect(releaseSnap(720, fast, heights, ALL)).toBe('full');
    expect(releaseSnap(160, -fast, heights, ALL)).toBe('peek');
    expect(releaseSnap(360, fast, heights, ['peek', 'half'])).toBe('half');
  });
});

describe('releaseVelocity', () => {
  it('is positive while the finger moves up (the sheet grows)', () => {
    const v = releaseVelocity([
      { t: 0, y: 500 },
      { t: 50, y: 470 },
      { t: 100, y: 440 },
    ]);
    expect(v).toBeCloseTo(0.6);
  });

  it('looks only at the last 100 ms', () => {
    const v = releaseVelocity([
      { t: 0, y: 900 },
      { t: 400, y: 500 },
      { t: 450, y: 520 },
      { t: 500, y: 540 },
    ]);
    expect(v).toBeCloseTo(-0.4);
  });

  it('is zero without movement data', () => {
    expect(releaseVelocity([])).toBe(0);
    expect(releaseVelocity([{ t: 10, y: 300 }])).toBe(0);
    expect(
      releaseVelocity([
        { t: 10, y: 300 },
        { t: 10, y: 200 },
      ]),
    ).toBe(0);
  });
});
