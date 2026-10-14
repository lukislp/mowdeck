import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import HeatmapControls from './HeatmapControls.svelte';
import HeatmapGaps from './HeatmapGaps.svelte';

const handlers = () => ({
  onlayer: vi.fn(),
  onperiod: vi.fn(),
  oncell: vi.fn(),
  onrefresh: vi.fn(),
});

const base = { period: '7d' as const, cell: 1 as const };

describe('HeatmapControls', () => {
  it('holds the mow path toggle only when the page offers it', async () => {
    const on = handlers();
    const onpath = vi.fn();
    const { unmount } = render(HeatmapControls, {
      props: { ...base, ...on, layer: 'off', status: 'idle' },
    });
    expect(screen.queryByTestId('mow-path-toggle')).toBeNull();
    unmount();
    render(HeatmapControls, {
      props: {
        ...base,
        ...on,
        layer: 'off',
        status: 'idle',
        pathShown: true,
        pathNote: '230 of 1,800 points',
        onpath,
      },
    });
    const toggle = screen.getByTestId('mow-path-toggle');
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByTestId('mow-path-note').textContent).toBe('230 of 1,800 points');
    await fireEvent.click(toggle);
    expect(onpath).toHaveBeenCalledWith(false);
  });

  it('shows only the switcher while the layer is off', async () => {
    const on = handlers();
    render(HeatmapControls, { props: { ...base, ...on, layer: 'off', status: 'idle' } });
    expect(screen.getByTestId('heatmap-layer-off').getAttribute('aria-checked')).toBe('true');
    expect(screen.queryByTestId('heatmap-legend')).toBeNull();
    await fireEvent.click(screen.getByTestId('heatmap-layer-coverage'));
    expect(on.onlayer).toHaveBeenCalledWith('coverage');
  });

  it('moves through the layers with the arrow keys', async () => {
    const on = handlers();
    render(HeatmapControls, { props: { ...base, ...on, layer: 'rtk', status: 'idle' } });
    await fireEvent.keyDown(screen.getByTestId('heatmap-layer'), { key: 'ArrowRight' });
    expect(on.onlayer).toHaveBeenCalledWith('wifi');
    await fireEvent.keyDown(screen.getByTestId('heatmap-layer'), { key: 'ArrowLeft' });
    expect(on.onlayer).toHaveBeenLastCalledWith('coverage');
  });

  it('reports period, cell size and refresh', async () => {
    const on = handlers();
    render(HeatmapControls, { props: { ...base, ...on, layer: 'coverage', status: 'ready' } });
    await fireEvent.change(screen.getByTestId('heatmap-period'), { target: { value: 'job' } });
    expect(on.onperiod).toHaveBeenCalledWith('job');
    await fireEvent.click(screen.getByTestId('heatmap-cell-2'));
    expect(on.oncell).toHaveBeenCalledWith(2);
    await fireEvent.click(screen.getByTestId('heatmap-refresh'));
    expect(on.onrefresh).toHaveBeenCalledOnce();
  });

  it('draws the legend of the layer: five signal steps, three RTK bands', () => {
    const { unmount } = render(HeatmapControls, {
      props: { ...base, ...handlers(), layer: 'wifi', status: 'loading' },
    });
    const legend = screen.getByTestId('heatmap-legend');
    expect(legend.dataset.kind).toBe('dbm');
    expect(legend.querySelectorAll('.swatch')).toHaveLength(5);
    expect(legend.textContent).toContain('-90');
    expect(legend.textContent).toContain('-40');
    expect(screen.getByTestId('heatmap-refresh')).toHaveProperty('disabled', true);
    unmount();
    render(HeatmapControls, { props: { ...base, ...handlers(), layer: 'rtk', status: 'idle' } });
    expect(screen.getByTestId('heatmap-legend').dataset.kind).toBe('fix');
    expect(screen.getByTestId('heatmap-legend').querySelectorAll('.swatch')).toHaveLength(3);
  });

  it.each([
    ['empty', {}],
    ['no-map', {}],
    ['error', { error: 'Netzwerk' }],
  ])('shows the %s state', (status, extra) => {
    render(HeatmapControls, {
      props: { ...base, ...handlers(), layer: 'mnet', status, ...extra } as never,
    });
    const line = screen.getByTestId('heatmap-status');
    expect(line.dataset.state).toBe(status);
    expect(line.textContent?.trim()).not.toBe('');
  });

  it('says when the server coarsened the cells and when the job fell back', () => {
    render(HeatmapControls, {
      props: {
        ...base,
        ...handlers(),
        layer: 'coverage',
        status: 'ready',
        answeredCell: 2,
        samples: 1234,
        truncated: true,
        fallback: true,
      },
    });
    const line = screen.getByTestId('heatmap-status');
    expect(screen.getByTestId('heatmap-samples')).toBeInTheDocument();
    expect(line.textContent).toMatch(/2 m/);
    expect(line.textContent?.length).toBeGreaterThan(20);
  });
});

describe('HeatmapGaps', () => {
  const gaps = [
    {
      index: 1,
      gap: {
        polygon: [
          [2, 2],
          [5, 2],
          [5, 4],
        ],
        area_m2: 4.2,
      },
    },
    {
      index: 0,
      gap: {
        polygon: [
          [0, 0],
          [1, 0],
          [1, 1],
        ],
        area_m2: 0.5,
      },
    },
  ];

  it('lists the spots with their area and an empty state', () => {
    const { unmount } = render(HeatmapGaps, {
      props: {
        gaps,
        selected: null,
        onselect: vi.fn(),
        areaOf: () => null,
        canStart: true,
        onmowarea: vi.fn(),
      },
    });
    expect(screen.getByTestId('heatmap-gap-1').textContent).toMatch(/4[.,]2/);
    expect(screen.queryByTestId('heatmap-gap-remow')).toBeNull();
    unmount();
    render(HeatmapGaps, {
      props: {
        gaps: [],
        selected: null,
        onselect: vi.fn(),
        areaOf: () => null,
        canStart: true,
        onmowarea: vi.fn(),
      },
    });
    expect(screen.getByTestId('heatmap-gaps-empty')).toBeInTheDocument();
  });

  it('selects a spot and offers the follow-ups of the selected one', async () => {
    const onselect = vi.fn();
    const onmowarea = vi.fn();
    render(HeatmapGaps, {
      props: {
        gaps,
        selected: 1,
        onselect,
        areaOf: () => ({ id: 'front', name: 'Vorgarten' }),
        canStart: true,
        onmowarea,
      },
    });
    const remow = screen.getByTestId('heatmap-gap-remow');
    expect(remow).toHaveProperty('disabled', true);
    expect(remow.getAttribute('aria-describedby')).toBeTruthy();
    expect(remow.parentElement?.getAttribute('title')).toBeTruthy();
    await fireEvent.click(screen.getByTestId('heatmap-gap-mow-area'));
    expect(onmowarea).toHaveBeenCalledWith('front');
    await fireEvent.click(screen.getByTestId('heatmap-gap-0'));
    expect(onselect).toHaveBeenCalledWith(0);
    await fireEvent.click(screen.getByTestId('heatmap-gap-1'));
    expect(onselect).toHaveBeenLastCalledWith(null);
  });

  it('hides "mow area" without permission to start', () => {
    render(HeatmapGaps, {
      props: {
        gaps,
        selected: 1,
        onselect: vi.fn(),
        areaOf: () => ({ id: 'front', name: 'Vorgarten' }),
        canStart: false,
        onmowarea: vi.fn(),
      },
    });
    expect(screen.queryByTestId('heatmap-gap-mow-area')).toBeNull();
    expect(screen.getByTestId('heatmap-gap-clear')).toBeInTheDocument();
  });
});
