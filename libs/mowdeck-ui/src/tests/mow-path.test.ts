import { describe, expect, it } from 'vitest';

import { pathDoneShare, pathLayers, splitPathProgress, type MapMowPath } from '../lib/map/mow-path';
import { createProjection } from '../lib/map/projection';

const EDGE = [
  [0, 0],
  [4, 0],
  [4, 2],
];
const STRIPES = [
  [0.5, 0.5],
  [3.5, 0.5],
  [3.5, 1],
  [0.5, 1],
];
const LINES = [EDGE, STRIPES];
const PATH: MapMowPath = {
  path_hash: '18446744073709551557',
  polylines: LINES,
  kinds: ['edge', 'mow'],
  points_total: 7,
  progress: { index: 5, direction: 1 },
};

describe('splitPathProgress', () => {
  it('splits the active polyline so both parts meet at the last done point', () => {
    const pieces = splitPathProgress(LINES, 5, PATH.kinds);
    expect(pieces).toEqual([
      { points: EDGE, done: true, kind: 'edge' },
      { points: STRIPES.slice(0, 2), done: true, kind: 'mow' },
      { points: STRIPES.slice(1), done: false, kind: 'mow' },
    ]);
  });

  it('shows everything ahead without progress and everything done at the end', () => {
    expect(splitPathProgress(LINES, null).every((p) => !p.done)).toBe(true);
    expect(splitPathProgress(LINES, 0).map((p) => p.done)).toEqual([false, false]);
    expect(splitPathProgress(LINES, 7).every((p) => p.done)).toBe(true);
    expect(splitPathProgress(LINES, 99).every((p) => p.done)).toBe(true);
  });

  it('cuts at a polyline boundary without empty pieces', () => {
    const pieces = splitPathProgress(LINES, 3);
    expect(pieces.map((p) => [p.done, p.points.length])).toEqual([
      [true, 3],
      [false, 4],
    ]);
  });

  it('drops a one point piece but keeps the line ahead whole', () => {
    const pieces = splitPathProgress(LINES, 4);
    expect(pieces.map((p) => [p.done, p.points.length])).toEqual([
      [true, 3],
      [false, 4],
    ]);
    expect(splitPathProgress([[[1, 1]]], 0)).toEqual([]);
  });

  it('defaults the kind to mow and ignores negative or fractional indexes', () => {
    expect(splitPathProgress([EDGE], -3)[0]).toEqual({ points: EDGE, done: false, kind: 'mow' });
    expect(splitPathProgress([EDGE], 2.9).map((p) => p.points.length)).toEqual([2, 2]);
  });
});

describe('pathDoneShare and pathLayers', () => {
  it('reports the share done', () => {
    expect(pathDoneShare(PATH)).toBeCloseTo(5 / 7);
    expect(pathDoneShare({ ...PATH, progress: null })).toBe(0);
    expect(pathDoneShare({ ...PATH, points_total: 0, polylines: [] })).toBe(0);
    expect(pathDoneShare(null)).toBe(0);
  });

  it('projects the pieces to map coordinates', () => {
    const projection = createProjection({ origin_lat: 48, origin_lon: 11 }, 'local');
    const layers = pathLayers(PATH, projection);
    expect(layers).toHaveLength(3);
    expect(layers[0]?.points[1]).toEqual([0, 4]); // [y, x] in the local projection
    expect(pathLayers(null, projection)).toEqual([]);
  });
});
