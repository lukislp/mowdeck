import { describe, expect, it } from 'vitest';

import {
  DEFAULT_SPACING_MM,
  defaultsOf,
  FALLBACK,
  angleApplies,
  initialDraft,
  minLaps,
  offeredPatterns,
  readPattern,
  samePattern,
  spacingRange,
  stepSpacing,
  toPattern,
} from '$lib/mower/mow-pattern';

const ALL = offeredPatterns(['random_angle', 'edge_only', 'checkerboard', 'diagonal', 'stripes']);

describe('offered patterns', () => {
  it('keeps the sheet order and drops unknown names', () => {
    expect(ALL).toEqual(['stripes', 'diagonal', 'checkerboard', 'edge_only', 'random_angle']);
    expect(offeredPatterns(['checkerboard', 'spiral', 'stripes'])).toEqual([
      'stripes',
      'checkerboard',
    ]);
    expect(offeredPatterns(undefined)).toEqual([]);
    expect(offeredPatterns(null)).toEqual([]);
  });
});

describe('spacing range', () => {
  it('reads a valid range only', () => {
    expect(spacingRange([200, 350])).toEqual([200, 350]);
    expect(spacingRange(null)).toBeNull();
    expect(spacingRange([200])).toBeNull();
    expect(spacingRange([350, 200])).toBeNull();
    expect(spacingRange([0, 200])).toBeNull();
    expect(spacingRange([Number.NaN, 200])).toBeNull();
  });

  it('steps from the model default into the range', () => {
    expect(stepSpacing(null, 1, [200, 350])).toBe(DEFAULT_SPACING_MM);
    expect(stepSpacing(null, -1, [300, 350])).toBe(300);
    expect(stepSpacing(250, 1, [200, 350])).toBe(260);
    expect(stepSpacing(350, 1, [200, 350])).toBe(350);
    expect(stepSpacing(200, -1, [200, 350])).toBe(200);
  });
});

describe('initial draft', () => {
  it('falls back to stripes, 0 degrees and one border lap', () => {
    expect(initialDraft(null, ALL, null)).toEqual(FALLBACK);
    expect(FALLBACK).toEqual({ name: 'stripes', angle: 0, laps: 1, spacingMm: null });
  });

  it('pre-fills the saved defaults, clamped', () => {
    const saved = { name: 'diagonal' as const, angle_deg: 45, border_laps: 2, spacing_m: 0.3 };
    expect(initialDraft(saved, ALL, [200, 350])).toEqual({
      name: 'diagonal',
      angle: 45,
      laps: 2,
      spacingMm: 300,
    });
    expect(
      initialDraft(
        { name: 'stripes', angle_deg: 400, border_laps: 9, spacing_m: 0.9 },
        ALL,
        [200, 350],
      ),
    ).toEqual({ name: 'stripes', angle: 180, laps: 4, spacingMm: 350 });
    // Without a spacing range the saved spacing is not sent.
    expect(initialDraft(saved, ALL, null).spacingMm).toBeNull();
  });

  it('replaces a pattern the mower cannot run', () => {
    const luba1 = offeredPatterns(['stripes', 'diagonal', 'checkerboard', 'edge_only']);
    expect(initialDraft({ name: 'random_angle' }, luba1, null).name).toBe('stripes');
    expect(initialDraft({ name: 'stripes' }, ['checkerboard'], null).name).toBe('checkerboard');
    expect(initialDraft({ name: 'edge_only', border_laps: 0 }, ALL, null).laps).toBe(1);
  });
});

describe('pattern json', () => {
  it('sends the spacing only when chosen and edge only with a lap', () => {
    expect(toPattern(FALLBACK)).toEqual({ name: 'stripes', angle_deg: 0, border_laps: 1 });
    expect(toPattern({ name: 'edge_only', angle: 30, laps: 0, spacingMm: 280 })).toEqual({
      name: 'edge_only',
      angle_deg: 30,
      border_laps: 1,
      spacing_m: 0.28,
    });
  });

  it('compares patterns with a missing spacing as null', () => {
    const a = { name: 'stripes' as const, angle_deg: 0, border_laps: 1 };
    expect(samePattern(a, { ...a, spacing_m: null })).toBe(true);
    expect(samePattern(a, { ...a, angle_deg: 15 })).toBe(false);
    expect(samePattern(a, { ...a, name: 'diagonal' })).toBe(false);
    expect(samePattern(a, { ...a, border_laps: 2 })).toBe(false);
    expect(samePattern(a, { ...a, spacing_m: 0.3 })).toBe(false);
    expect(samePattern(null, undefined)).toBe(true);
    expect(samePattern(a, null)).toBe(false);
  });

  it('reads command patterns tolerantly', () => {
    expect(readPattern({ name: 'checkerboard', angle_deg: 10, border_laps: 2 })).toEqual({
      name: 'checkerboard',
      angle_deg: 10,
      border_laps: 2,
      spacing_m: null,
    });
    expect(readPattern({ angle: 45 })).toEqual({
      name: 'stripes',
      angle_deg: 0,
      border_laps: 0,
      spacing_m: null,
    });
    expect(readPattern({ name: 'spiral' })).toBeNull();
    expect(readPattern(null)).toBeNull();
    expect(readPattern('stripes')).toBeNull();
  });

  it('knows where angle and laps apply', () => {
    expect(angleApplies('stripes')).toBe(true);
    expect(angleApplies('checkerboard')).toBe(true);
    expect(angleApplies('edge_only')).toBe(false);
    expect(angleApplies('random_angle')).toBe(false);
    expect(minLaps('edge_only')).toBe(1);
    expect(minLaps('diagonal')).toBe(0);
  });
});

describe('job options in the pattern', () => {
  const SPEC = {
    detection: ['off', 'standard', 'sensitive'],
    obstacleLaps: [0, 4],
    orders: ['border_first', 'grid_first'],
    progress: [0, 99],
  } as const;

  it('opens with options only when the mower offers them', () => {
    expect(initialDraft(null, ALL, null).options).toBeUndefined();
    expect(
      initialDraft({ obstacle_laps: 2 }, ALL, null, {
        ...SPEC,
        detection: [...SPEC.detection],
        orders: [...SPEC.orders],
      }).options,
    ).toEqual({ detection: null, obstacleLaps: 2, order: null, progress: 0 });
  });

  it('sends the set options with the pattern', () => {
    expect(
      toPattern({
        ...FALLBACK,
        options: { detection: 'standard', obstacleLaps: null, order: 'border_first', progress: 0 },
      }),
    ).toEqual({
      name: 'stripes',
      angle_deg: 0,
      border_laps: 1,
      obstacle_detection: 'standard',
      order: 'border_first',
    });
    expect(defaultsOf({ ...toPattern(FALLBACK), start_progress_pct: 20 })).toEqual(
      toPattern(FALLBACK),
    );
  });

  it('compares and reads the options', () => {
    const a = { name: 'stripes' as const, angle_deg: 0, border_laps: 1 };
    expect(samePattern(a, { ...a, obstacle_detection: null, start_progress_pct: 0 })).toBe(true);
    expect(samePattern(a, { ...a, obstacle_laps: 0 })).toBe(false);
    expect(samePattern(a, { ...a, order: 'grid_first' })).toBe(false);
    expect(samePattern(a, { ...a, obstacle_detection: 'sensitive' })).toBe(false);
    expect(samePattern(a, { ...a, start_progress_pct: 10 })).toBe(false);
    expect(
      readPattern({
        ...a,
        obstacle_detection: 'sensitive',
        obstacle_laps: 0,
        order: 'grid_first',
        start_progress_pct: 30,
      }),
    ).toEqual({
      ...a,
      spacing_m: null,
      obstacle_detection: 'sensitive',
      obstacle_laps: 0,
      order: 'grid_first',
      start_progress_pct: 30,
    });
    expect(readPattern({ ...a, obstacle_detection: 'eager', order: 'sideways' })).toEqual({
      ...a,
      spacing_m: null,
    });
  });
});
