// @vitest-environment jsdom
import { render, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it, vi } from 'vitest';

import TrackMap, { type TrackMapLayer } from '../lib/map/TrackMap.svelte';

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
});
