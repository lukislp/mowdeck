// @vitest-environment jsdom
import * as L from 'leaflet';
import { describe, expect, it, vi } from 'vitest';

import { createAerialLayer, type MapAerial } from '../lib/map/aerial';
import { createProjection } from '../lib/map/projection';

const REF = { origin_lat: 48.137154, origin_lon: 11.576124, rotation_deg: 0 };

function aerial(load: MapAerial['load'], extra: Partial<MapAerial> = {}): MapAerial {
  return {
    url: '/api/v1/telemetry/mowers/m/aerial/{z}/{x}/{y}',
    opacity: 0.7,
    minZoom: 14,
    maxZoom: 19,
    bounds: [48.136, 11.574, 48.139, 11.578],
    load,
    ...extra,
  };
}

describe('createAerialLayer', () => {
  it('fills the tile route, loads through the callback and shows the blob', async () => {
    const load = vi.fn(
      async (_url: string, _signal: AbortSignal) => new Blob(['x'], { type: 'image/jpeg' }),
    );
    URL.createObjectURL = vi.fn(() => 'blob:tile');
    URL.revokeObjectURL = vi.fn();
    const layer = createAerialLayer(L, aerial(load), () => load);
    expect(layer.options.opacity).toBe(0.7);
    expect(layer.options.maxNativeZoom).toBe(19);
    expect(layer.options.minZoom).toBe(14);
    const done = vi.fn();
    // no map here: the zoom Leaflet would have set while loading a level
    (layer as unknown as { _tileZoom: number })._tileZoom = 17;
    const img = (
      layer as unknown as {
        createTile: (c: { x: number; y: number; z: number }, d: typeof done) => HTMLImageElement;
      }
    ).createTile({ x: 3, y: 4, z: 17 }, done);
    await vi.waitFor(() => expect(load).toHaveBeenCalled());
    expect(load.mock.calls[0]![0]).toBe('/api/v1/telemetry/mowers/m/aerial/17/3/4');
    await vi.waitFor(() => expect(img.src).toBe('blob:tile'));
  });

  it('reports a failing load to Leaflet instead of throwing', async () => {
    const load = vi.fn(async (_url: string, _signal: AbortSignal): Promise<Blob> => {
      throw new Error('429');
    });
    const layer = createAerialLayer(L, aerial(load), () => load);
    const done = vi.fn();
    (
      layer as unknown as {
        createTile: (c: { x: number; y: number; z: number }, d: typeof done) => HTMLImageElement;
      }
    ).createTile({ x: 1, y: 1, z: 15 }, done);
    await vi.waitFor(() => expect(done).toHaveBeenCalledWith(expect.any(Error), expect.anything()));
  });

  it('limits requests to the bounds of the map', () => {
    const layer = createAerialLayer(L, aerial(vi.fn()), () => undefined);
    const b = layer.options.bounds as L.LatLngBounds;
    expect(b.getSouth()).toBe(48.136);
    expect(b.getEast()).toBe(11.578);
  });
});

describe('alignment of the map with the tiles', () => {
  it('puts a local point where Web Mercator tiles place its WGS84 position', () => {
    // 100 m east of the reference on a rotation-free map: the same longitude shift the server
    // uses to bound the tiles (equirectangular around the origin, ADR 0053).
    const projection = createProjection(REF, 'geo');
    const [lat, lon] = projection.fromLocal(100, 0);
    expect(lat).toBeCloseTo(REF.origin_lat, 9);
    const metres =
      (lon - REF.origin_lon) *
      (Math.PI / 180) *
      6_378_137 *
      Math.cos((REF.origin_lat * Math.PI) / 180);
    expect(metres).toBeCloseTo(100, 6);
    const crs = L.CRS.EPSG3857;
    const a = crs.project(L.latLng(REF.origin_lat, REF.origin_lon));
    const b = crs.project(L.latLng(lat, lon));
    expect(b.x - a.x).toBeCloseTo(100 / Math.cos((REF.origin_lat * Math.PI) / 180), 1);
  });
});
