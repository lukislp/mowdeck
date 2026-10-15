import { describe, expect, it } from 'vitest';
import { pixelBox, revealPan } from '../lib/map/viewport';

describe('pixelBox', () => {
  it('spans the points', () => {
    expect(
      pixelBox([
        { x: 10, y: 40 },
        { x: 30, y: 5 },
      ]),
    ).toEqual({ left: 10, top: 5, right: 30, bottom: 40 });
    expect(pixelBox([])).toBeNull();
  });
});

describe('revealPan (map under a bottom sheet)', () => {
  // A 400 x 800 map whose bottom 360 px are covered: the free part is y 24..416.
  const W = 400;
  const H = 800;
  const INSET = 360;
  const PAD = 24;

  it('leaves a box that is above the sheet alone', () => {
    const box = { left: 100, top: 100, right: 200, bottom: 300 };
    expect(revealPan(box, W, H, INSET, PAD)).toEqual({ kind: 'inside' });
  });

  it('pans the content up when the box runs under the sheet', () => {
    const box = { left: 100, top: 400, right: 200, bottom: 500 };
    expect(revealPan(box, W, H, INSET, PAD)).toEqual({ kind: 'pan', dx: 0, dy: 500 - 416 });
  });

  it('pans down and sideways for a box off the top left', () => {
    const box = { left: -40, top: -10, right: 60, bottom: 90 };
    expect(revealPan(box, W, H, INSET, PAD)).toEqual({ kind: 'pan', dx: -64, dy: -34 });
  });

  it('asks for a fit when the box is taller than the free part', () => {
    const box = { left: 100, top: 0, right: 200, bottom: 500 };
    expect(revealPan(box, W, H, INSET, PAD)).toEqual({ kind: 'fit' });
    const wide = { left: 0, top: 100, right: 390, bottom: 200 };
    expect(revealPan(wide, W, H, INSET, PAD)).toEqual({ kind: 'fit' });
  });

  it('without a sheet the whole map counts', () => {
    const box = { left: 100, top: 400, right: 200, bottom: 500 };
    expect(revealPan(box, W, H, 0, PAD)).toEqual({ kind: 'inside' });
  });

  it('does nothing on a map that has no free room (hidden, zero size)', () => {
    const box = { left: 0, top: 0, right: 10, bottom: 10 };
    expect(revealPan(box, 0, 0, 0, PAD)).toEqual({ kind: 'inside' });
    expect(revealPan(box, W, 300, 290, PAD)).toEqual({ kind: 'inside' });
  });
});
