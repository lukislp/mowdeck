/**
 * The camera panel in the panorama layout (simulator session): one wide canvas, the calibration
 * controls stored per mower, and both raw pictures still marked for AI assist's frame capture.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import CameraPanel from '$lib/components/CameraPanel.svelte';
import { m } from '$lib/paraglide/messages';
import { findCameraSources, stereoSources } from '$lib/vision/snapshot';

import { LAYOUT_KEY } from './layout';
import { panoramaKey } from './calibration/storage';

const NOW = Date.now();

vi.mock('$lib/api/client', () => ({
  fleetApi: {
    POST: async () => ({
      provider: 'fake',
      app_id: 'app',
      channel: 'ch',
      token: 't',
      uid: 7,
      cameras: [
        { id: 'front_left', name: 'Front left', uid: 1, placement: 'front_left' },
        { id: 'front_right', name: 'Front right', uid: 2, placement: 'front_right' },
      ],
      encryption: null,
      expires_at: new Date(NOW + 600_000).toISOString(),
      quota: null,
    }),
  },
  unwrap: async (p: Promise<unknown>) => p,
}));

vi.mock('$lib/config', () => ({
  loadConfig: async () => ({ mapTiles: null, legalEnabled: false, cameraEnabled: true }),
}));

class Observer {
  observe(): void {}
  disconnect(): void {}
  unobserve(): void {}
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(LAYOUT_KEY, 'panorama');
  vi.stubGlobal('ResizeObserver', Observer);
  vi.stubGlobal('IntersectionObserver', Observer);
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('camera panel in the panorama layout', () => {
  it('shows one wide canvas and keeps both raw pictures for the frame capture', async () => {
    render(CameraPanel, { props: { mowerId: 'm1', mowerName: 'Luba' } });
    const panel = screen.getByTestId('camera-panel');
    await waitFor(() => expect(panel.dataset.phase).toBe('live'));
    expect(panel.querySelector('.stage')?.getAttribute('data-layout')).toBe('panorama');
    expect(screen.getByTestId('camera-layout-panorama').getAttribute('aria-checked')).toBe('true');
    expect(screen.getByTestId('camera-panorama')).toBeInTheDocument();
    const caption = screen.getByTestId('camera-panorama-caption').textContent ?? '';
    expect(caption).toContain(m.camera_front_left());
    expect(caption).toContain(m.camera_front_right());
    // The tiles are hidden, not removed: AI assist still finds the left and right picture.
    expect(screen.getByTestId('camera-tile').classList.contains('offstage')).toBe(true);
    expect(screen.getByTestId('camera-tile-right').classList.contains('offstage')).toBe(true);
    const sources = findCameraSources(document);
    expect(sources.map((s) => s.side)).toEqual(['left', 'right']);
    expect(sources.map((s) => s.element.dataset.testid)).toEqual([
      'camera-pattern',
      'camera-pattern-right',
    ]);
    expect(stereoSources(sources)).not.toBeNull();
  });

  it('reads slider settings stored before calibrations existed', async () => {
    localStorage.setItem(
      panoramaKey('m1'),
      JSON.stringify({ overlap: 0.2, offset: 0.05, mirror: true }),
    );
    render(CameraPanel, { props: { mowerId: 'm1', mowerName: 'Luba' } });
    await waitFor(() => expect(screen.getByTestId('camera-panel').dataset.phase).toBe('live'));
    const canvas = screen.getByTestId('camera-panorama');
    expect(canvas.dataset.method).toBe('manual');
    expect(canvas.dataset.overlap).toBe('20');
    expect(canvas.dataset.offset).toBe('5');
    expect(canvas.dataset.mirror).toBe('true');
  });

  it('calibrates per mower: sliders, mirror and reset', async () => {
    render(CameraPanel, { props: { mowerId: 'm1', mowerName: 'Luba' } });
    await waitFor(() => expect(screen.getByTestId('camera-panel').dataset.phase).toBe('live'));
    await fireEvent.click(screen.getByTestId('camera-panorama-calibrate'));
    const overlap = screen.getByTestId('camera-panorama-overlap') as HTMLInputElement;
    const offset = screen.getByTestId('camera-panorama-offset') as HTMLInputElement;
    expect(overlap.value).toBe('12');
    expect(overlap.max).toBe('40');
    expect(offset.min).toBe('-15');
    await fireEvent.input(overlap, { target: { value: '30' } });
    await fireEvent.input(offset, { target: { value: '-5' } });
    expect(JSON.parse(localStorage.getItem(panoramaKey('m1')) ?? '{}')).toMatchObject({
      version: 2,
      method: 'manual',
      overlapPercent: 30,
      offsetPercent: -5,
      mirror: false,
      capturedAt: null,
    });
    expect(screen.getByTestId('camera-panorama').dataset.overlap).toBe('30');
    expect(screen.getByTestId('camera-panorama-note').textContent).toBe(m.camera_panorama_note());
    // Auto without real frames (no canvas in the test DOM): no match, said in words.
    await fireEvent.click(screen.getByTestId('camera-panorama-auto'));
    expect(screen.getByTestId('camera-panorama-auto-result').dataset.kind).toBe('failed');
    await fireEvent.click(screen.getByTestId('camera-panorama-reset'));
    expect(localStorage.getItem(panoramaKey('m1'))).toBeNull();
    expect((screen.getByTestId('camera-panorama-overlap') as HTMLInputElement).value).toBe('12');
  });

  it('goes back to the side-by-side view with visible tiles', async () => {
    render(CameraPanel, { props: { mowerId: 'm1', mowerName: 'Luba' } });
    await waitFor(() => expect(screen.getByTestId('camera-panel').dataset.phase).toBe('live'));
    await fireEvent.click(screen.getByTestId('camera-layout-both'));
    await waitFor(() => expect(screen.queryByTestId('camera-panorama')).toBeNull());
    expect(screen.getByTestId('camera-tile').classList.contains('offstage')).toBe(false);
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('both');
  });
});
