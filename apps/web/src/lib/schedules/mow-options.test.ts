import { describe, expect, it } from 'vitest';

import { setLocale } from '$lib/paraglide/runtime';

import {
  NO_OFFER,
  commonOffer,
  emptyMow,
  fitMowDraft,
  mowBody,
  mowDraftOf,
  mowSummary,
  offerOf,
  offersAnything,
  type CapsLike,
} from './mow-options';

const AWD: CapsLike = {
  model: 'LUBA 2 AWD 3000',
  native_patterns: ['stripes', 'diagonal', 'checkerboard'],
  path_spacing_range_mm: [200, 350],
  blade_height_range_mm: [25, 70],
  min_speed_mps: 0.2,
  max_speed_mps: 1.0,
  start_options: {
    obstacle_detection: ['off', 'standard', 'sensitive'],
    obstacle_laps_range: [0, 4],
    orders: ['border_first', 'grid_first'],
    start_progress_range_pct: [0, 99],
  },
};
const YUKA: CapsLike = {
  model: 'Yuka',
  native_patterns: ['stripes', 'edge_only', 'checkerboard'],
  blade_height_range_mm: [30, 60],
  min_speed_mps: 0.3,
  max_speed_mps: 0.8,
  start_options: { obstacle_detection: ['standard'], obstacle_laps_range: [0, 2] },
};

describe('offerOf', () => {
  it('offers what one mower declares, never the start progress', () => {
    expect(offerOf(AWD)).toEqual({
      offered: ['stripes', 'diagonal', 'checkerboard'],
      spacing: [200, 350],
      options: {
        detection: ['off', 'standard', 'sensitive'],
        obstacleLaps: [0, 4],
        orders: ['border_first', 'grid_first'],
        progress: null,
      },
      blade: [25, 70],
      speed: [0.2, 1.0],
    });
  });

  it('offers no job options to a mower without start_options', () => {
    const bare = offerOf({ native_patterns: ['stripes'] });
    expect(bare.options).toBeNull();
    expect(bare.spacing).toBeNull();
    expect(bare.blade).toBeNull();
    expect(bare.speed).toBeNull();
    // Only a start progress declared: still nothing to offer a schedule.
    expect(offerOf({ start_options: { start_progress_range_pct: [0, 99] } }).options).toBeNull();
    expect(offerOf(null)).toEqual(NO_OFFER);
  });
});

describe('commonOffer', () => {
  it('offers a group only what every member offers', () => {
    expect(commonOffer([AWD, YUKA])).toEqual({
      offered: ['stripes', 'checkerboard'],
      spacing: null,
      options: { detection: ['standard'], obstacleLaps: [0, 2], orders: [], progress: null },
      blade: [30, 60],
      speed: [0.3, 0.8],
    });
    expect(commonOffer([AWD])).toEqual(offerOf(AWD));
  });

  it('offers nothing when a member is unknown or there are none', () => {
    expect(commonOffer([AWD, null])).toEqual(NO_OFFER);
    expect(commonOffer([AWD, undefined])).toEqual(NO_OFFER);
    expect(commonOffer([])).toEqual(NO_OFFER);
    expect(offersAnything(NO_OFFER)).toBe(false);
    // Ranges that do not overlap offer nothing.
    const low = { blade_height_range_mm: [20, 30] };
    const high = { blade_height_range_mm: [40, 60] };
    expect(commonOffer([low, high]).blade).toBeNull();
    // One member without job options: the group has none.
    expect(commonOffer([AWD, { native_patterns: ['stripes'] }]).options).toBeNull();
  });
});

describe('draft and body', () => {
  it('sends only what is set and reads it back', () => {
    expect(mowBody(emptyMow())).toEqual({ pattern: null, blade_height_mm: null, speed_mps: null });
    const draft = emptyMow();
    draft.pattern = {
      name: 'diagonal',
      angle: 30,
      laps: 0,
      spacingMm: 250,
      options: { detection: 'sensitive', obstacleLaps: 1, order: 'grid_first', progress: 0 },
    };
    draft.setpoints = { bladeMm: 45, speedMps: 0.6 };
    const body = mowBody(draft);
    expect(body).toEqual({
      pattern: {
        name: 'diagonal',
        angle_deg: 30,
        border_laps: 0,
        spacing_m: 0.25,
        obstacle_detection: 'sensitive',
        obstacle_laps: 1,
        order: 'grid_first',
      },
      blade_height_mm: 45,
      speed_mps: 0.6,
    });
    expect(mowDraftOf(body)).toEqual(draft);
  });

  it('clears what another target does not offer', () => {
    const draft = mowDraftOf({
      pattern: {
        name: 'diagonal',
        angle_deg: 30,
        spacing_m: 0.25,
        obstacle_detection: 'sensitive',
        obstacle_laps: 3,
        order: 'grid_first',
      },
      blade_height_mm: 65,
      speed_mps: 0.5,
    });
    const fitted = fitMowDraft(draft, commonOffer([AWD, YUKA]));
    expect(fitted.pattern).toEqual({
      name: null,
      angle: 30,
      laps: null,
      spacingMm: null,
      options: { detection: null, obstacleLaps: null, order: null, progress: 0 },
    });
    expect(fitted.setpoints).toEqual({ bladeMm: null, speedMps: 0.5 });
    expect(fitMowDraft(draft, offerOf(AWD))).toEqual(draft);
    expect(fitMowDraft(draft, NO_OFFER)).toEqual(emptyMow());
  });
});

describe('mowSummary', () => {
  it('lists what the schedule sets, nothing for the defaults', () => {
    setLocale('de', { reload: false });
    expect(mowSummary({ pattern: null, rotation_angles: [] })).toEqual([]);
    expect(
      mowSummary({
        pattern: {
          name: 'diagonal',
          angle_deg: 45,
          border_laps: 2,
          obstacle_detection: 'sensitive',
          order: 'border_first',
          obstacle_laps: 1,
        },
        blade_height_mm: 40,
        speed_mps: 0.6,
        rotation_angles: [],
      }),
    ).toEqual([
      'Diagonal',
      '45°',
      '2 Randrunden',
      'Empfindlich',
      'Zuerst Rand',
      'Sperrzone 1×',
      '40 mm',
      '0,60 m/s',
    ]);
    // The rotation replaces the angle.
    expect(mowSummary({ pattern: { angle_deg: 10 }, rotation_angles: [0, 90, 225] })).toEqual([
      '0°/90°/45°',
    ]);
  });
});
