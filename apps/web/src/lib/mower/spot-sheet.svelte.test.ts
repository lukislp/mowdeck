import { flushSync } from 'svelte';
import { describe, expect, it, vi } from 'vitest';

import { toWgs84, type GeoReference } from '@mowdeck/ui';

import type { ConditionState, PlanOut, PreviewOut, SpotRunStatus } from './spot-sheet';
import { SpotSheetFlow, type SpotSheetContext, type SpotSheetDeps } from './spot-sheet.svelte';

// The sheet flow with fakes: spot mode, the debounced preview, the start conditions, the start
// (plan, then `spot_mow`), STOP and the end notice. A `.svelte.test.ts` file, so runes work.

const reference: GeoReference = { origin_lat: 48.1, origin_lon: 11.5 };
const NOW = Date.parse('2026-10-15T10:00:00Z');

function readyState(patch: Partial<ConditionState> = {}): ConditionState {
  const [lat, lon] = toWgs84(reference, 11, 10);
  return {
    connection: 'ble_bridge',
    links: null,
    rtk_fix: 'fixed',
    position_source: 'rtk',
    position: { lat, lon },
    activity: 'idle',
    battery_percent: 80,
    observed_at: new Date(NOW - 500).toISOString(),
    spot_run: null,
    ...patch,
  };
}

function previewOut(hash = 'h1'): PreviewOut {
  return {
    plan_hash: hash,
    estimated_duration_s: 120,
    segment_count: 3,
    warnings: [],
    preview: {
      type: 'FeatureCollection',
      features: [
        {
          geometry: {
            type: 'LineString',
            coordinates: [
              [9, 9],
              [11, 11],
            ],
          },
          properties: { kind: 'mow' },
        },
      ],
    },
  } as unknown as PreviewOut;
}

function setup(contextPatch: Partial<SpotSheetContext> = {}) {
  const context = $state<SpotSheetContext>({
    mowerId: 'm1',
    reference,
    areas: [
      {
        id: 'front',
        boundary: [
          [0, 0],
          [20, 0],
          [20, 20],
          [0, 20],
        ],
      },
    ],
    capabilities: {
      manual_drive: true,
      manual_drive_blades: false,
      cutting_width_mm: 220,
      links_supported: ['ble_bridge'],
    },
    state: readyState(),
    driveSession: false,
    ...contextPatch,
  });
  const timers: (() => void)[] = [];
  const deps = {
    preview: vi.fn(async () => previewOut()),
    plan: vi.fn(async () => ({ spot_plan: { plan_hash: 'h1' } }) as unknown as PlanOut),
    command: vi.fn<SpotSheetDeps['command']>(async () => true),
    paths: { replace: vi.fn() },
    now: () => NOW,
    context: () => context,
    schedule: vi.fn((fn: () => void) => {
      timers.push(fn);
      return () => {
        const i = timers.indexOf(fn);
        if (i >= 0) timers.splice(i, 1);
      };
    }),
  } satisfies SpotSheetDeps;
  const flow = new SpotSheetFlow(deps);
  const stop = $effect.root(() => flow.autoPreview());
  flushSync();
  async function fire(): Promise<void> {
    const pending = timers.splice(0);
    for (const fn of pending) fn();
    await Promise.resolve();
    await Promise.resolve();
    flushSync();
  }
  return { flow, deps, context, fire, timers, stop };
}

describe('spot sheet flow', () => {
  it('a tap places a 1.5 m circle and opens the sheet; the radius stays within 0.5 to 3 m', () => {
    const { flow, stop } = setup();
    flow.begin();
    expect(flow.mode).toBe('circle');
    flow.mapTap([10, 10]);
    expect(flow.spot).toEqual({ kind: 'circle', center: [10, 10], radius_m: 1.5 });
    expect(flow.open).toBe(true);
    expect(flow.areaId).toBe('front');
    flow.setRadius(7);
    expect(flow.spot).toMatchObject({ radius_m: 3 });
    expect(flow.settings).toMatchObject({
      spacingMm: 170,
      speedMps: 0.2,
      blades: false,
      pattern: 'spiral',
    });
    stop();
  });

  it('a drawn shape takes at most eight corners, opens at three and undoes corner by corner', () => {
    const { flow, stop } = setup();
    flow.begin();
    flow.drawShape();
    flow.mapTap([5, 5]);
    flow.mapTap([8, 5]);
    expect(flow.open).toBe(false);
    flow.mapTap([8, 8]);
    expect(flow.open).toBe(true);
    for (let i = 0; i < 10; i += 1) flow.mapTap([5 + i * 0.1, 9]);
    expect(flow.corners).toBe(8);
    flow.undoCorner();
    expect(flow.corners).toBe(7);
    flow.useCircle();
    expect(flow.spot?.kind).toBe('circle');
    stop();
  });

  it('asks the Planner once per settled request, then all green lets the start go', async () => {
    const { flow, deps, fire, timers, stop } = setup();
    flow.begin();
    flow.mapTap([10, 10]);
    flushSync();
    flow.setRadius(2);
    flushSync();
    // Debounced: only the last change is still scheduled.
    expect(timers).toHaveLength(1);
    await fire();
    expect(deps.preview).toHaveBeenCalledTimes(1);
    expect(flow.previewReady?.plan_hash).toBe('h1');
    expect(flow.previewPath?.polylines).toHaveLength(1);
    expect(flow.conditions.every((c) => c.ok)).toBe(true);
    expect(flow.canStart).toBe(true);

    await flow.start();
    expect(deps.plan).toHaveBeenCalledTimes(1);
    expect(deps.command).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: 'spot_mow',
        blades: false,
        confirm_blades: false,
        speed_mps: 0.2,
      }),
    );
    const sent = deps.command.mock.calls[0]![0] as { spot: { radius_m: number } };
    expect(sent.spot.radius_m).toBe(2);
    expect(deps.paths.replace).toHaveBeenCalledWith(expect.objectContaining({ path_hash: 'h1' }));
    expect(flow.mode).toBe('off');
    expect(flow.lastSpot).toMatchObject({ kind: 'circle', radius_m: 2 });
    stop();
  });

  it('a red condition blocks the start', async () => {
    const { flow, deps, context, fire, stop } = setup();
    flow.begin();
    flow.mapTap([10, 10]);
    flushSync();
    await fire();
    context.state = readyState({ rtk_fix: 'float' });
    flushSync();
    expect(flow.conditions.find((c) => c.id === 'rtk')?.ok).toBe(false);
    expect(flow.canStart).toBe(false);
    await flow.start();
    expect(deps.plan).not.toHaveBeenCalled();
    stop();
  });

  it('blades go out only when allowed and confirmed a second time', async () => {
    const { flow, deps, fire, stop } = setup({
      capabilities: { manual_drive: true, manual_drive_blades: true, cutting_width_mm: 220 },
      state: readyState({ connection: 'simulator' }),
    });
    flow.begin();
    flow.mapTap([10, 10]);
    flushSync();
    await fire();
    flow.setBlades(true);
    expect(flow.bladesOn).toBe(false);
    flow.settings = { ...flow.settings, confirmBlades: true };
    expect(flow.bladesOn).toBe(true);
    flow.setBlades(false);
    expect(flow.settings.confirmBlades).toBe(false);
    flow.setBlades(true);
    flow.settings = { ...flow.settings, confirmBlades: true };
    await flow.start();
    expect(deps.command).toHaveBeenCalledWith(
      expect.objectContaining({ blades: true, confirm_blades: true }),
    );
    stop();
  });

  it('a refused plan is shown and spot mode stays', async () => {
    const { flow, deps, fire, stop } = setup();
    deps.plan.mockRejectedValueOnce(new Error('boom'));
    flow.begin();
    flow.mapTap([10, 10]);
    flushSync();
    await fire();
    await flow.start();
    expect(flow.startError).toBeTruthy();
    expect(flow.mode).toBe('circle');
    expect(deps.command).not.toHaveBeenCalled();
    stop();
  });

  it('"Nachmähen" opens the sheet with the gap polygon as the spot', () => {
    const { flow, stop } = setup();
    flow.fromGap({
      polygon: [
        [4, 4],
        [6, 4],
        [6, 6],
        [4, 6],
      ],
    });
    expect(flow.mode).toBe('polygon');
    expect(flow.open).toBe(true);
    expect(flow.corners).toBe(4);
    expect(flow.areaId).toBe('front');
    stop();
  });

  it('STOP sends spot_stop; a run that ends while watched becomes the end notice', async () => {
    const { flow, deps, stop } = setup();
    const status = (patch: Partial<SpotRunStatus>): SpotRunStatus => ({
      run_id: 'r1',
      state: 'spot_mowing',
      progress_percent: 10,
      segment_index: 1,
      segments_total: 4,
      planned_points: 20,
      driven_points: 3,
      dry_run: true,
      reason: null,
      ...patch,
    });
    flow.observe(null);
    flow.observe(status({}));
    await flow.stop();
    expect(deps.command).toHaveBeenCalledWith({ kind: 'spot_stop' });
    flow.observe(status({ state: 'aborted', reason: 'stopped_by_user' }));
    expect(flow.ended?.reason).toBe('stopped_by_user');
    flow.dismissEnd();
    flow.observe(status({ state: 'aborted', reason: 'stopped_by_user' }));
    expect(flow.ended).toBeNull();
    stop();
  });

  it('after a start the run is "launching" until it shows up in the state', async () => {
    const { flow, timers, fire, stop } = setup();
    flow.begin();
    flow.mapTap([10, 10]);
    flushSync();
    await fire();
    await flow.start();
    expect(flow.launching).toBe(true);
    expect(timers).toHaveLength(1);
    flow.observe({
      run_id: 'r1',
      state: 'preparing',
      progress_percent: 0,
      segment_index: 0,
      segments_total: 2,
      planned_points: 10,
      driven_points: 0,
      dry_run: true,
      reason: null,
    });
    expect(flow.launching).toBe(false);
    expect(timers).toHaveLength(0);
    stop();
  });

  it('a launch that never shows up ends after the wait', async () => {
    const { flow, fire, stop } = setup();
    flow.begin();
    flow.mapTap([10, 10]);
    flushSync();
    await fire();
    await flow.start();
    expect(flow.launching).toBe(true);
    await fire();
    expect(flow.launching).toBe(false);
    stop();
  });

  it('a final run already there when the page opens is not announced', () => {
    const { flow, stop } = setup();
    flow.observe({
      run_id: 'old',
      state: 'done',
      progress_percent: 100,
      segment_index: 4,
      segments_total: 4,
      planned_points: 20,
      driven_points: 20,
      dry_run: true,
      reason: null,
    });
    expect(flow.ended).toBeNull();
    stop();
  });
});
