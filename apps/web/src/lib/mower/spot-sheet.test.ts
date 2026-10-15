import { describe, expect, it } from 'vitest';

import { fromWgs84, toWgs84, type GeoReference, type MapSpot } from '@mowdeck/ui';

import { setLocale } from '$lib/paraglide/runtime';

import {
  SPOT_SPEED_MAX_MPS,
  allGreen,
  bladesRequested,
  conditionHint,
  conditionText,
  clampRadiusM,
  clampSpacingMm,
  clampSpeedMps,
  defaultSpacingMm,
  driveLinkUp,
  hasTurnKinds,
  initialSettings,
  isSpotRunActive,
  isSpotRunFinal,
  planRequest,
  previewKind,
  previewMowPath,
  resolveTurnStyle,
  spotCommand,
  spotConditions,
  spotFromGap,
  spotPreviewKey,
  spotReasonLabel,
  spotRunLabel,
  spotTarget,
  spotTransitFrom,
  startsFromDock,
  turnFallsBack,
  turnOptions,
  turnStyleHint,
  turnStyleLabel,
  type ConditionState,
  type PlanOut,
  type PreviewOut,
  type SpotRunStatus,
} from './spot-sheet';

const reference: GeoReference = { origin_lat: 48.1, origin_lon: 11.5 };
const square = {
  id: 'front',
  boundary: [
    [0, 0],
    [20, 0],
    [20, 20],
    [0, 20],
  ],
};
const circle: MapSpot = { kind: 'circle', center: [10, 10], radius_m: 1.5 };
const NOW = Date.parse('2026-10-15T10:00:00Z');

function stateAt(local: [number, number], patch: Partial<ConditionState> = {}): ConditionState {
  const [lat, lon] = toWgs84(reference, local[0], local[1]);
  return {
    connection: 'ble_bridge',
    links: null,
    rtk_fix: 'fixed',
    position_source: 'rtk',
    position: { lat, lon },
    activity: 'idle',
    battery_percent: 80,
    observed_at: new Date(NOW - 1_000).toISOString(),
    spot_run: null,
    ...patch,
  };
}

function run(patch: Partial<SpotRunStatus> = {}): SpotRunStatus {
  return {
    run_id: 'r1',
    state: 'spot_mowing',
    progress_percent: 40,
    segment_index: 2,
    segments_total: 5,
    planned_points: 30,
    driven_points: 12,
    dry_run: true,
    reason: null,
    ...patch,
  };
}

describe('limits', () => {
  it('spacing opens with the cutting width minus 50 mm and stays within the width', () => {
    expect(defaultSpacingMm(220)).toBe(170);
    expect(defaultSpacingMm(null)).toBe(350);
    expect(clampSpacingMm(500, 220)).toBe(220);
    expect(clampSpacingMm(10, 220)).toBe(50);
    expect(clampSpacingMm(174, 220)).toBe(170);
  });

  it('speed is hard capped at 0.3 m/s and never below 0.1', () => {
    expect(clampSpeedMps(1)).toBe(SPOT_SPEED_MAX_MPS);
    expect(clampSpeedMps(0)).toBe(0.1);
    expect(clampSpeedMps(0.22)).toBe(0.2);
    expect(clampSpeedMps(Number.NaN)).toBe(0.2);
  });

  it('radius stays between 0.5 and 3 m in 0.1 m steps', () => {
    expect(clampRadiusM(0.1)).toBe(0.5);
    expect(clampRadiusM(9)).toBe(3);
    expect(clampRadiusM(1.26)).toBe(1.3);
  });

  it('blades are off by default and need the capability, the switch and the confirm', () => {
    const s = initialSettings(220);
    expect(s.blades).toBe(false);
    expect(s.pattern).toBe('spiral');
    expect(bladesRequested({ ...s, blades: true, confirmBlades: true }, false)).toBe(false);
    expect(bladesRequested({ ...s, blades: true, confirmBlades: false }, true)).toBe(false);
    expect(bladesRequested({ ...s, blades: true, confirmBlades: true }, true)).toBe(true);
  });
});

describe('planner request', () => {
  it('sends the spot as a polygon inside its area with the sheet mower data', () => {
    const request = planRequest({
      mowerId: 'm1',
      spot: circle,
      areaId: 'front',
      settings: { ...initialSettings(220), speedMps: 0.9 },
      cuttingWidthMm: 220,
    });
    expect(request).not.toBeNull();
    expect(request!.area.area_id).toBe('front');
    expect(request!.area.polygon!.length).toBeGreaterThanOrEqual(8);
    expect(request!.chain).toEqual([{ pattern: 'spiral', settings: { direction: 'inward' } }]);
    expect(request!.mower).toMatchObject({
      cutting_width_m: 0.22,
      overlap_m: 0.05,
      max_speed_mps: 0.3,
      transfer_speed_mps: 0.3,
    });
    expect(request!.wgs84).toBe(false);
  });

  it('stripes carry their angle; no request without an area or a complete spot', () => {
    const settings = { ...initialSettings(220), pattern: 'stripes' as const, angleDeg: 30 };
    const request = planRequest({
      mowerId: 'm1',
      spot: circle,
      areaId: 'front',
      settings,
      cuttingWidthMm: 220,
    });
    expect(request!.chain).toEqual([{ pattern: 'stripes', settings: { angle_deg: 30 } }]);
    expect(
      planRequest({ mowerId: 'm1', spot: circle, areaId: null, settings, cuttingWidthMm: 220 }),
    ).toBeNull();
    const twoCorners: MapSpot = {
      kind: 'polygon',
      points: [
        [1, 1],
        [2, 2],
      ],
    };
    expect(
      planRequest({
        mowerId: 'm1',
        spot: twoCorners,
        areaId: 'front',
        settings,
        cuttingWidthMm: 220,
      }),
    ).toBeNull();
  });

  it('the preview key ignores key order but follows every value', () => {
    const settings = initialSettings(220);
    const a = planRequest({
      mowerId: 'm1',
      spot: circle,
      areaId: 'front',
      settings,
      cuttingWidthMm: 220,
    });
    const b = planRequest({
      mowerId: 'm1',
      spot: { ...circle, radius_m: 2 },
      areaId: 'front',
      settings,
      cuttingWidthMm: 220,
    });
    expect(spotPreviewKey(a)).toBe(spotPreviewKey(structuredClone(a)));
    expect(spotPreviewKey(a)).not.toBe(spotPreviewKey(b));
    expect(spotPreviewKey(null)).toBe('');
  });

  it('turns the preview LineStrings into mow path polylines (transfer kept apart)', () => {
    const preview = {
      plan_hash: 'h1',
      preview: {
        type: 'FeatureCollection',
        features: [
          {
            geometry: {
              type: 'LineString',
              coordinates: [
                [0, 0],
                [1, 0],
                [1, 1],
              ],
            },
            properties: { kind: 'transfer' },
          },
          {
            geometry: {
              type: 'LineString',
              coordinates: [
                [1, 1],
                [2, 1],
              ],
            },
            properties: { kind: 'mow' },
          },
          { geometry: { type: 'LineString', coordinates: [[5, 5]] }, properties: { kind: 'mow' } },
          { geometry: { type: 'Point', coordinates: [0, 0] }, properties: {} },
        ],
      },
    } as unknown as PreviewOut;
    const path = previewMowPath(preview, '2026-10-15T10:00:00Z');
    expect(path.path_hash).toBe('h1');
    expect(path.kinds).toEqual(['transfer', 'mow']);
    expect(path.points_total).toBe(5);
    expect(path.progress).toBeNull();
  });

  it('draws a found transit first as a transfer, none without a route', () => {
    const preview = {
      plan_hash: 'h2',
      preview: {
        type: 'FeatureCollection',
        features: [
          {
            geometry: {
              type: 'LineString',
              coordinates: [
                [5, 5],
                [6, 5],
              ],
            },
            properties: { kind: 'mow' },
          },
        ],
      },
      transit: {
        found: true,
        from_dock: true,
        length_m: 7.1,
        route: [
          [-1, 1],
          [0.5, 1],
          [5, 5],
        ],
        reason: null,
      },
    } as unknown as PreviewOut;
    const path = previewMowPath(preview, '2026-10-15T10:00:00Z');
    expect(path.kinds).toEqual(['transfer', 'mow']);
    expect(path.polylines?.[0]).toEqual([
      [-1, 1],
      [0.5, 1],
      [5, 5],
    ]);
    const blocked = {
      ...preview,
      transit: { found: false, from_dock: true, length_m: 0, route: [], reason: 'no_route: x' },
    } as unknown as PreviewOut;
    expect(previewMowPath(blocked, '2026-10-15T10:00:00Z').kinds).toEqual(['mow']);
  });

  it('asks for the transit from the dock or from the rounded position', () => {
    const settings = initialSettings(220);
    const docked = stateAt([3, 4], { activity: 'charging' });
    expect(spotTransitFrom(docked, reference, true)).toEqual({ from_dock: true });
    expect(spotTransitFrom(stateAt([3.26, 4.1]), reference, true)).toEqual({
      from_dock: false,
      position: [3.5, 4],
    });
    expect(spotTransitFrom(docked, reference, false)).toEqual({
      from_dock: false,
      position: [3, 4],
    });
    expect(spotTransitFrom(null, reference, true)).toBeNull();
    const request = planRequest({
      mowerId: 'm1',
      spot: circle,
      areaId: 'front',
      settings,
      cuttingWidthMm: 220,
      transitFrom: { from_dock: true },
    });
    expect(request?.transit).toEqual({ from_dock: true });
    const plain = planRequest({
      mowerId: 'm1',
      spot: circle,
      areaId: 'front',
      settings,
      cuttingWidthMm: 220,
    });
    expect(plain && 'transit' in plain).toBe(false);
  });
});

describe('command', () => {
  const plan = { spot_plan: { plan_hash: 'h1' } } as unknown as PlanOut;

  it('a dry run unless blades are allowed, switched on and confirmed', () => {
    const settings = { ...initialSettings(220), blades: true, confirmBlades: true };
    const dry = spotCommand({ plan, settings, bladesAllowed: false, target: null });
    expect(dry).toMatchObject({
      kind: 'spot_mow',
      blades: false,
      confirm_blades: false,
      speed_mps: 0.2,
    });
    const cut = spotCommand({ plan, settings, bladesAllowed: true, target: null });
    expect(cut).toMatchObject({ blades: true, confirm_blades: true });
    expect(cut.plan).toBe(plan.spot_plan);
  });

  it('the audit target is WGS84: centre and radius, or the corners', () => {
    const target = spotTarget(circle, reference);
    expect(target).toMatchObject({ radius_m: 1.5 });
    const [lat, lon] = (target as { center_wgs84: [number, number] }).center_wgs84;
    const back = fromWgs84(reference, lat, lon);
    expect(back[0]).toBeCloseTo(10, 3);
    expect(back[1]).toBeCloseTo(10, 3);
    const shape = spotTarget(spotFromGap({ polygon: [[1, 1], [3, 1], [3, 3], [9]] }), reference);
    expect((shape as { polygon_wgs84: unknown[] }).polygon_wgs84).toHaveLength(3);
  });
});

describe('run states', () => {
  it('active and final states, labels and reasons in words', () => {
    expect(isSpotRunActive(run())).toBe(true);
    expect(isSpotRunActive(run({ state: 'paused_rtk' }))).toBe(true);
    expect(isSpotRunActive(run({ state: 'done' }))).toBe(false);
    expect(isSpotRunFinal(run({ state: 'aborted' }))).toBe(true);
    expect(isSpotRunActive(null)).toBe(false);
    expect(spotRunLabel('paused_rtk')).toBeTruthy();
    for (const state of ['undocking', 'waiting_rtk', 'transit', 'returning'] as const) {
      expect(isSpotRunActive(run({ state }))).toBe(true);
      expect(spotRunLabel(state)).toBeTruthy();
      expect(spotRunLabel(state)).not.toBe(spotRunLabel('spot_mowing'));
    }
    expect(spotReasonLabel('undock_failed')).not.toBe('undock_failed');
    expect(spotReasonLabel('no_route')).not.toBe('no_route');
    expect(spotReasonLabel('geofence')).not.toBe('geofence');
    expect(spotReasonLabel('something_new')).toBe('something_new');
    setLocale('de', { reload: false });
    expect(spotReasonLabel('stalled')).toBe('seit 60 s kein Fortschritt entlang des Plans');
    expect(spotReasonLabel('timeout')).toBe('Gesamtzeitlimit erreicht');
    setLocale('en', { reload: false });
    expect(spotReasonLabel('stalled')).toBe('no progress along the plan for 60 s');
    expect(spotReasonLabel('timeout')).toBe('overall time limit reached');
    setLocale('de', { reload: false });
    expect(spotReasonLabel(null)).toBeNull();
  });
});

describe('start conditions', () => {
  const base = {
    spot: circle,
    areaId: 'front' as string | null,
    areas: [square],
    reference,
    linksSupported: ['ble_bridge'],
    driveSession: false,
    now: NOW,
    transit: { found: true } as { found: boolean } | null,
    undock: false as boolean | null,
  };

  function failing(patch: Partial<typeof base> & { state?: ConditionState | null }): string[] {
    return spotConditions({ ...base, state: stateAt([12, 10]), ...patch })
      .filter((c) => !c.ok)
      .map((c) => c.id);
  }

  it('all green for an idle mower with RTK fix near the spot on the bridge', () => {
    const conditions = spotConditions({ ...base, state: stateAt([12, 10]) });
    expect(conditions.map((c) => c.id)).toEqual([
      'spot',
      'link',
      'signal',
      'rtk',
      'route',
      'idle',
      'free',
      'battery',
      'fresh',
    ]);
    expect(allGreen(conditions)).toBe(true);
  });

  it('each condition turns red on its own', () => {
    expect(failing({ areaId: null })).toEqual(['spot', 'route']);
    expect(failing({ state: stateAt([12, 10], { connection: 'cloud' }) })).toEqual(['link']);
    expect(failing({ state: stateAt([12, 10], { rtk_fix: 'float' }) })).toEqual(['rtk']);
    expect(failing({ state: stateAt([12, 10], { position_source: 'gps' }) })).toEqual(['rtk']);
    expect(failing({ transit: { found: false } })).toEqual(['route']);
    expect(failing({ transit: null })).toEqual(['route']);
    // no distance limit any more: a far mower with a route is fine
    expect(failing({ state: stateAt([250, 10]) })).toEqual([]);
    expect(failing({ state: stateAt([10, 10], { activity: 'docked' }) })).toEqual(['idle']);
    expect(failing({ state: stateAt([10, 10], { activity: 'mowing' }) })).toEqual(['idle', 'free']);
    expect(failing({ driveSession: true })).toEqual(['free']);
    expect(failing({ state: stateAt([10, 10], { spot_run: run() }) })).toEqual(['free']);
    expect(failing({ state: stateAt([10, 10], { battery_percent: 20 }) })).toEqual(['battery']);
    expect(
      failing({
        state: stateAt([10, 10], {
          observed_at: new Date(NOW - 61_000).toISOString(),
          heartbeat_at: new Date(NOW - 2_000).toISOString(),
        }),
      }),
    ).toEqual(['fresh']);
  });

  it('a cloud-first state of 20 s is fresh enough (the run waits for live positions)', () => {
    const state = stateAt([10, 10], { observed_at: new Date(NOW - 20_000).toISOString() });
    expect(failing({ state })).toEqual([]);
    // A minute-old report counts while the link heartbeat says the mower is online.
    const minute = stateAt([10, 10], {
      observed_at: new Date(NOW - 60_000).toISOString(),
      heartbeat_at: new Date(NOW - 8_000).toISOString(),
    });
    expect(failing({ state: minute })).toEqual([]);
    const silent = stateAt([10, 10], { observed_at: new Date(NOW - 50_000).toISOString() });
    expect(failing({ state: silent })).toEqual(['fresh']);
  });

  it('a state of a mower whose link the gateway reported lost is not fresh', () => {
    expect(failing({ state: stateAt([10, 10], { connected: false }) })).toEqual(['fresh']);
    const beat = new Date(NOW - 5_000).toISOString();
    const old = new Date(NOW - 30_000).toISOString();
    expect(failing({ state: stateAt([10, 10], { observed_at: old, heartbeat_at: beat }) })).toEqual(
      [],
    );
  });

  it('without a state every mower condition is red', () => {
    expect(failing({ state: null })).toEqual([
      'link',
      'rtk',
      'route',
      'idle',
      'free',
      'battery',
      'fresh',
    ]);
  });

  it('a docked mower that can undock may start: ready, RTK waits until it is out', () => {
    const docked = stateAt([10, 10], { activity: 'docked', rtk_fix: 'float' });
    expect(failing({ state: docked, undock: true })).toEqual([]);
    expect(failing({ state: { ...docked, activity: 'charging' }, undock: true })).toEqual([]);
    expect(failing({ state: docked, undock: false })).toEqual(['rtk', 'idle']);
    expect(failing({ state: docked, undock: true, transit: { found: false } })).toEqual(['route']);
    expect(startsFromDock(docked, true)).toBe(true);
    expect(startsFromDock(stateAt([10, 10]), true)).toBe(false);
    expect(startsFromDock(docked, undefined)).toBe(false);
  });

  it('the drive link: bridge link flag first, then the connection kind', () => {
    expect(
      driveLinkUp({ connection: 'cloud', links: { ble_bridge: { connected: true } } }, [
        'ble_bridge',
      ]),
    ).toBe(true);
    expect(
      driveLinkUp({ connection: 'cloud', links: { ble_bridge: { connected: false } } }, [
        'ble_bridge',
      ]),
    ).toBe(false);
    expect(driveLinkUp({ connection: 'ble_bridge', links: null }, ['ble_bridge'])).toBe(true);
    expect(driveLinkUp({ connection: 'cloud', links: null }, ['ble_bridge'])).toBe(false);
    // A connection without a bridge link drives over its primary connection (the simulator).
    expect(driveLinkUp({ connection: 'simulator', links: null }, [])).toBe(true);
    expect(driveLinkUp(null, [])).toBe(false);
  });

  it('the bridge signal: a row only on the bridge, red below -90 dBm, neutral while unknown', () => {
    const at = (rssi: number | null): ConditionState =>
      stateAt([12, 10], { connection: 'cloud', links: { ble_bridge: { connected: true, rssi } } });
    const signal = (state: ConditionState) =>
      spotConditions({ ...base, state }).find((c) => c.id === 'signal');
    expect(failing({ state: at(-97) })).toEqual(['signal']);
    expect(failing({ state: at(-74) })).toEqual([]);
    const weak = signal(at(-97))!;
    expect(conditionText(weak)).toBe('Bluetooth-Signal ausreichend: −97 dBm (ab −90 dBm)');
    expect(conditionHint(weak)).toContain('näher an den Mäher');
    const unknown = signal(at(null))!;
    expect(unknown).toMatchObject({ ok: true, neutral: true });
    expect(conditionHint(unknown)).toContain('der Start bleibt möglich');
    expect(failing({ state: at(null) })).toEqual([]);
    // a mower that drives over its primary connection has no signal row
    expect(signal(stateAt([12, 10], { connection: 'simulated' }))).toBeUndefined();
    expect(conditionHint({ id: 'rtk', ok: false })).toBeNull();
  });

  it('a lost drive link names the bridge when the mower drives over it', () => {
    expect(spotReasonLabel('link_lost', { bridge: true })).toBe(
      'Bluetooth-Bridge nicht erreichbar oder zu schwaches Signal',
    );
    expect(spotReasonLabel('link_lost')).toBe('Fahrverbindung unterbrochen');
    expect(spotReasonLabel('frame_rejected', { bridge: true })).toBe('frame_rejected');
  });
});

describe('turn styles', () => {
  it('resolves the choice, then the mower setting, then arc', () => {
    expect(resolveTurnStyle('in_place', 'multi_point', true)).toBe('in_place');
    expect(resolveTurnStyle(null, 'in_place', false)).toBe('in_place');
    expect(resolveTurnStyle(undefined, 'multi_point', true)).toBe('multi_point');
    expect(resolveTurnStyle(null, null, true)).toBe('arc');
    expect(resolveTurnStyle(undefined, undefined, undefined)).toBe('arc');
  });

  it('multi-point falls back to arc without reverse driving, picked or from the mower', () => {
    expect(resolveTurnStyle('multi_point', null, false)).toBe('arc');
    expect(resolveTurnStyle('multi_point', null, null)).toBe('arc');
    expect(resolveTurnStyle(null, 'multi_point', false)).toBe('arc');
    expect(resolveTurnStyle('multi_point', 'in_place', true)).toBe('multi_point');
    expect(turnFallsBack('multi_point', null, false)).toBe(true);
    expect(turnFallsBack(null, 'multi_point', false)).toBe(true);
    expect(turnFallsBack(null, 'multi_point', true)).toBe(false);
    expect(turnFallsBack('arc', 'multi_point', false)).toBe(false);
  });

  it('the default option names the resolved style; multi-point is disabled without reverse', () => {
    setLocale('de', { reload: false });
    const plain = turnOptions(null, true);
    expect(plain.map((o) => o.value)).toEqual([null, 'in_place', 'arc', 'multi_point']);
    expect(plain.map((o) => o.label)).toEqual([
      'Wie am Mäher eingestellt (Bogen)',
      'Auf der Stelle',
      'Bogen',
      'Mehrpunkt',
    ]);
    expect(plain.every((o) => !o.disabled && o.hint === null)).toBe(true);
    expect(turnOptions('in_place', true)[0]!.label).toBe(
      'Wie am Mäher eingestellt (Auf der Stelle)',
    );
    expect(turnOptions('multi_point', true)[0]!.label).toBe('Wie am Mäher eingestellt (Mehrpunkt)');

    const noReverse = turnOptions('multi_point', false);
    expect(noReverse[0]).toMatchObject({
      value: null,
      resolved: 'arc',
      label: 'Wie am Mäher eingestellt (Bogen)',
      disabled: false,
    });
    expect(noReverse[3]).toMatchObject({
      value: 'multi_point',
      resolved: 'arc',
      disabled: true,
      hint: 'braucht Rückwärtsfahrt',
    });
    expect(noReverse.slice(1, 3).every((o) => !o.disabled)).toBe(true);

    setLocale('en', { reload: false });
    expect(turnOptions(null, false).map((o) => o.label)).toEqual([
      'As set on the mower (Arc)',
      'In place',
      'Arc',
      'Multi-point',
    ]);
    expect(turnOptions(null, false)[3]!.hint).toBe('needs reverse driving');
    expect(turnStyleLabel('multi_point')).toBe('Multi-point');
    expect(turnStyleHint('in_place')).not.toBe(turnStyleHint('arc'));
    setLocale('de', { reload: false });
  });

  it('the preview request carries the resolved style and the key follows it', () => {
    const base = { mowerId: 'm1', spot: circle, areaId: 'front', cuttingWidthMm: 220 };
    const settings = initialSettings(220);
    expect(settings.turnStyle).toBeNull();
    const byMower = planRequest({ ...base, settings, mowerTurnStyle: 'in_place' });
    expect(byMower?.turn_style).toBe('in_place');
    const fallback = planRequest({ ...base, settings });
    expect(fallback?.turn_style).toBe('arc');
    const multi = planRequest({
      ...base,
      settings: { ...settings, turnStyle: 'multi_point' },
      driveReverse: true,
    });
    expect(multi?.turn_style).toBe('multi_point');
    const noReverse = planRequest({
      ...base,
      settings: { ...settings, turnStyle: 'multi_point' },
      driveReverse: false,
    });
    expect(noReverse?.turn_style).toBe('arc');
    expect(spotPreviewKey(byMower)).not.toBe(spotPreviewKey(fallback));
    expect(spotPreviewKey(multi)).not.toBe(spotPreviewKey(noReverse));
    // Same resolved style, same key: the preview is not asked again.
    expect(spotPreviewKey(noReverse)).toBe(spotPreviewKey(fallback));
  });

  it('spot_mow carries the choice, null for the mower setting', () => {
    const plan = { spot_plan: { plan_hash: 'h1' } } as unknown as PlanOut;
    const settings = initialSettings(220);
    expect(spotCommand({ plan, settings, bladesAllowed: false, target: null }).turn_style).toBe(
      null,
    );
    expect(
      spotCommand({
        plan,
        settings: { ...settings, turnStyle: 'multi_point' },
        bladesAllowed: false,
        target: null,
      }).turn_style,
    ).toBe('multi_point');
  });

  it('maps turn segments and reverse legs to their own kinds', () => {
    expect(previewKind({ kind: 'mow' })).toBe('mow');
    expect(previewKind({ kind: 'turn' })).toBe('turn');
    expect(previewKind({ kind: 'turn', reverse: true })).toBe('reverse');
    expect(previewKind({ kind: 'transfer' })).toBe('transfer');
    expect(previewKind({ kind: 'something' })).toBe('mow');
    expect(previewKind(undefined)).toBe('mow');
    const line = (kind: string, reverse = false) => ({
      geometry: {
        type: 'LineString',
        coordinates: [
          [0, 0],
          [1, 1],
        ],
      },
      properties: { kind, reverse, blades_on: !reverse },
    });
    const preview = {
      plan_hash: 'h3',
      preview: {
        type: 'FeatureCollection',
        features: [line('mow'), line('turn'), line('turn', true), line('turn'), line('mow')],
      },
    } as unknown as PreviewOut;
    const path = previewMowPath(preview, '2026-10-15T10:00:00Z');
    expect(path.kinds).toEqual(['mow', 'turn', 'reverse', 'turn', 'mow']);
    expect(hasTurnKinds(path)).toBe(true);
    expect(hasTurnKinds({ kinds: ['mow', 'transfer'] })).toBe(false);
    expect(hasTurnKinds(null)).toBe(false);
  });
});
