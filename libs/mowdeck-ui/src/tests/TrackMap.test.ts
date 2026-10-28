// @vitest-environment jsdom
import { render, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import TrackMap, { type TrackMapLayer } from '../lib/map/TrackMap.svelte';
import { fillZoom, servedBox } from '../lib/map/trackView';

const LABELS = {
  region: 'Map',
  zoomIn: '+',
  zoomOut: '-',
  fit: 'Fit',
  follow: 'Follow',
  start: 'Start',
  last: 'Last',
  noTiles: 'No background map',
  layers: 'Layer',
  uncovered: (layer: string) => `${layer} not here`,
  noneHere: 'Nothing covers this place',
};

const TRACK = [
  { lat: 48.1, lon: 11.5 },
  { lat: 48.101, lon: 11.501 },
  { lat: 48.102, lon: 11.503 },
];

function layer(id: string, load: TrackMapLayer['load']): TrackMapLayer {
  return { id, label: id.toUpperCase(), url: `${id}/{z}/{x}/{y}`, minZoom: 2, maxZoom: 19, load };
}

function blobLoader() {
  return vi.fn(
    async (_url: string, _signal: AbortSignal) => new Blob(['x'], { type: 'image/png' }),
  );
}

describe('TrackMap', () => {
  it('draws the start and the last position, and keeps them when the tile settings arrive late', async () => {
    const { container, rerender } = render(TrackMap, { track: TRACK, labels: LABELS });
    await waitFor(() => expect(container.querySelector('.md-track-dot.last')).not.toBeNull());
    expect(container.querySelector('.md-track-dot.start')).not.toBeNull();
    const map = container.querySelector('[data-testid="track-map"]')!;
    expect(map.getAttribute('data-layer')).toBe('grid');
    expect(map.textContent).toContain('No background map');

    // The installation's tile settings come in after Leaflet loaded (the bug: an empty map).
    await rerender({
      track: TRACK,
      labels: LABELS,
      tiles: { url: 'https://tile.example/{z}/{x}/{y}.png', attribution: '© Tiles' },
    });
    await tick();
    expect(container.querySelector('.md-track-dot.last')).not.toBeNull();
    expect(container.querySelector('.md-track-dot.start')).not.toBeNull();
    expect(map.getAttribute('data-layer')).toBe('tiles');
    expect(map.textContent).toContain('© Tiles');
    expect(map.textContent).not.toContain('No background map');
  });

  it('loads the chosen base map through its loader and switches without losing the track', async () => {
    URL.createObjectURL = vi.fn(() => 'blob:tile');
    URL.revokeObjectURL = vi.fn();
    const street = blobLoader();
    const aerial = blobLoader();
    const layers = [
      { ...layer('map', street), attribution: '© Street' },
      { ...layer('aerial', aerial), attribution: '© Photo' },
    ];
    const { container, getByTestId } = render(TrackMap, {
      track: TRACK,
      labels: LABELS,
      layers,
      tiles: { url: 'https://tile.example/{z}/{x}/{y}.png' },
    });
    await waitFor(() => expect(container.querySelector('.md-track-dot.last')).not.toBeNull());
    const map = getByTestId('track-map');
    expect(map.getAttribute('data-layer')).toBe('map');
    expect(map.textContent).toContain('© Street');
    expect(getByTestId('track-map-layer-map').getAttribute('aria-checked')).toBe('true');

    getByTestId('track-map-layer-aerial').click();
    await tick();
    expect(map.getAttribute('data-layer')).toBe('aerial');
    expect(map.textContent).toContain('© Photo');
    expect(getByTestId('track-map-layer-aerial').getAttribute('aria-checked')).toBe('true');
    expect(container.querySelector('.md-track-dot.last')).not.toBeNull();
    // Tiles of the browser side server are never requested while MowDeck serves a base map.
    expect(container.querySelector('img[src^="https://tile.example"]')).toBeNull();
  });

  it('has no layer switch with a single base map', async () => {
    const { container, queryByRole } = render(TrackMap, {
      track: TRACK,
      labels: LABELS,
      layers: [layer('aerial', blobLoader())],
    });
    await waitFor(() => expect(container.querySelector('.md-track-dot.last')).not.toBeNull());
    expect(queryByRole('radiogroup')).toBeNull();
  });

  it('falls back to a covered layer when the chosen one has no data at the position', async () => {
    URL.createObjectURL = vi.fn(() => 'blob:tile');
    URL.revokeObjectURL = vi.fn();
    const layers = [
      {
        ...layer('map', blobLoader()),
        attribution: '© OpenStreetMap contributors',
        attributionUrl: 'https://www.openstreetmap.org/copyright',
      },
      { ...layer('aerial', blobLoader()), covered: false },
    ];
    const { container, getByTestId } = render(TrackMap, {
      track: TRACK,
      labels: LABELS,
      layers,
      layer: 'aerial',
    });
    await waitFor(() => expect(container.querySelector('.md-track-dot.last')).not.toBeNull());
    expect(getByTestId('track-map').getAttribute('data-layer')).toBe('map');
    expect(getByTestId('track-map-uncovered').textContent).toContain('AERIAL not here');
    expect(getByTestId('track-map-layer-aerial')).toBeDisabled();
    const credit = container.querySelector('.attribution a') as HTMLAnchorElement;
    expect(credit.href).toBe('https://www.openstreetmap.org/copyright');
    expect(credit.rel).toBe('noopener noreferrer');
  });

  it('shows the grid with a hint when no layer covers the position', async () => {
    const { container, getByTestId } = render(TrackMap, {
      track: TRACK,
      labels: LABELS,
      layers: [{ ...layer('aerial', blobLoader()), covered: false }],
    });
    await waitFor(() => expect(container.querySelector('.md-track-dot.last')).not.toBeNull());
    expect(getByTestId('track-map').getAttribute('data-layer')).toBe('grid');
    expect(getByTestId('track-map-bare').textContent).toContain('Nothing covers this place');
  });

  it('gives up a layer whose tiles keep failing and uses the next one', async () => {
    URL.createObjectURL = vi.fn(() => 'blob:tile');
    URL.revokeObjectURL = vi.fn();
    const broken = vi.fn(async (_url: string, _signal: AbortSignal): Promise<Blob> => {
      throw new Error('502');
    });
    const { getByTestId } = render(TrackMap, {
      track: TRACK,
      labels: LABELS,
      layers: [layer('aerial', broken), layer('map', blobLoader())],
    });
    await waitFor(() => expect(broken).toHaveBeenCalled());
    const map = getByTestId('track-map');
    await waitFor(() => expect(map.getAttribute('data-layer')).toBe('map'), { timeout: 3000 });
    expect(getByTestId('track-map-uncovered').textContent).toContain('AERIAL not here');
  });
});

describe('TrackMap view', () => {
  // jsdom lays nothing out; Leaflet reads the map size from clientWidth/clientHeight.
  const WIDTH = 400;
  const HEIGHT = 300;
  let restore: (() => void) | null = null;

  beforeEach(() => {
    URL.createObjectURL = vi.fn(() => 'blob:tile');
    URL.revokeObjectURL = vi.fn();
    const width = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth');
    const height = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight');
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
      configurable: true,
      get: () => WIDTH,
    });
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
      configurable: true,
      get: () => HEIGHT,
    });
    restore = () => {
      if (width) Object.defineProperty(HTMLElement.prototype, 'clientWidth', width);
      if (height) Object.defineProperty(HTMLElement.prototype, 'clientHeight', height);
    };
  });

  afterEach(() => {
    restore?.();
    restore = null;
  });

  const GROWN = [...TRACK, { lat: 48.12, lon: 11.56 }];

  function served(id: string, extra: Partial<TrackMapLayer> = {}): TrackMapLayer {
    return { ...layer(id, blobLoader()), minZoom: 8, maxZoom: 19, margin: 2000, ...extra };
  }

  async function mounted(props: Record<string, unknown>) {
    const view = render(TrackMap, { track: TRACK, labels: LABELS, ...props });
    const map = view.getByTestId('track-map');
    await waitFor(() => expect(map.getAttribute('data-zoom')).not.toBeNull());
    await waitFor(() => expect(map.getAttribute('data-min-zoom')).not.toBeNull());
    return { ...view, map, zoom: () => Number(map.getAttribute('data-zoom')) };
  }

  function userZoomIn(map: HTMLElement): void {
    // A double tap zooms like a pinch: no drag, no wheel, only Leaflet's own zoom.
    const canvas = map.querySelector('.leaflet-container') as HTMLElement;
    canvas.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, clientX: 200, clientY: 150 }));
  }

  it('fits the track while the user has not touched the map, and follows its growth', async () => {
    const layers = [served('map')];
    const { zoom, rerender } = await mounted({ layers });
    const first = zoom();
    await rerender({ track: GROWN, labels: LABELS, layers });
    await tick();
    expect(zoom()).toBeLessThan(first);
  });

  it('never refits after the user zoomed, also not on poll updates', async () => {
    const layers = [served('map')];
    const { map, zoom, rerender } = await mounted({ layers });
    const fitted = zoom();
    userZoomIn(map);
    await waitFor(() => expect(zoom()).toBeGreaterThan(fitted));
    const zoomed = zoom();
    // A poll: same points as a fresh array, then a grown track.
    await rerender({ track: [...TRACK], labels: LABELS, layers });
    await tick();
    expect(zoom()).toBe(zoomed);
    await rerender({ track: GROWN, labels: LABELS, layers });
    await tick();
    expect(zoom()).toBe(zoomed);
  });

  it('keeps the zoom when the layer is switched', async () => {
    const layers = [served('map'), served('aerial')];
    const { map, zoom, getByTestId } = await mounted({ layers });
    getByTestId('track-map-layer-map').click();
    const before = zoom();
    getByTestId('track-map').querySelector<HTMLButtonElement>('button[aria-label="+"]')!.click();
    await waitFor(() => expect(zoom()).toBeGreaterThan(before));
    const zoomed = zoom();
    getByTestId('track-map-layer-aerial').click();
    await tick();
    expect(map.getAttribute('data-layer')).toBe('aerial');
    expect(zoom()).toBe(zoomed);
  });

  it('keeps the user zoom while following new positions', async () => {
    const layers = [served('map')];
    const { map, zoom, rerender } = await mounted({ layers });
    const follow = map.querySelector<HTMLButtonElement>('button[aria-label="Follow"]')!;
    follow.click();
    await waitFor(() => expect(zoom()).toBe(17));
    map.querySelector<HTMLButtonElement>('button[aria-label="-"]')!.click();
    await waitFor(() => expect(zoom()).toBeLessThan(17));
    const zoomed = zoom();
    await rerender({ track: GROWN, labels: LABELS, layers });
    await tick();
    expect(zoom()).toBe(zoomed); // it used to jump back to 17 on every poll
    expect(follow.getAttribute('aria-pressed')).toBe('true');
  });

  it('limits zoom and panning to the area the layer serves tiles in', async () => {
    const layers = [served('map')];
    const { map, zoom } = await mounted({ layers });
    const box = servedBox(TRACK, 2000, null)!;
    // jsdom has no 3D transforms, so Leaflet snaps to whole zooms there (quarters in browsers).
    const minimum = Math.max(8, fillZoom(box, WIDTH, HEIGHT, 1)!);
    expect(Number(map.getAttribute('data-min-zoom'))).toBe(minimum);
    expect(map.getAttribute('data-max-zoom')).toBe('19');
    expect(map.getAttribute('data-bounds')).toBe(box.map((v) => v.toFixed(5)).join(','));
    // Zooming out stops where the served area still fills the view.
    const out = map.querySelector<HTMLButtonElement>('button[aria-label="-"]')!;
    for (let i = 0; i < 20; i += 1) out.click();
    await waitFor(() => expect(zoom()).toBe(minimum));
  });

  it('cuts the area to the coverage of the source', async () => {
    const coverage = [47.0, 10.0, 48.101, 12.0] as const;
    const { map } = await mounted({ layers: [served('aerial', { coverage })] });
    const bounds = map.getAttribute('data-bounds')!.split(',').map(Number);
    expect(bounds[2]).toBe(48.101);
    expect(bounds[0]).toBeCloseTo(servedBox(TRACK, 2000, null)![0], 5);
  });

  it('widens the limits when the track grows, without moving a valid view', async () => {
    const layers = [served('map')];
    const { map, zoom, rerender } = await mounted({ layers });
    userZoomIn(map);
    const before = Number(map.getAttribute('data-min-zoom'));
    await waitFor(() => expect(zoom()).toBeGreaterThan(before));
    const zoomed = zoom();
    await rerender({ track: GROWN, labels: LABELS, layers });
    await tick();
    const grown = servedBox(GROWN, 2000, null)!;
    expect(map.getAttribute('data-bounds')).toBe(grown.map((v) => v.toFixed(5)).join(','));
    expect(Number(map.getAttribute('data-min-zoom'))).toBeLessThan(before);
    expect(zoom()).toBe(zoomed);
  });

  it('has no limits on the grid or the browser side tiles', async () => {
    const { map } = await mounted({});
    expect(map.getAttribute('data-min-zoom')).toBe('2');
    expect(map.getAttribute('data-max-zoom')).toBe('22');
    expect(map.hasAttribute('data-bounds')).toBe(false);
  });
});
