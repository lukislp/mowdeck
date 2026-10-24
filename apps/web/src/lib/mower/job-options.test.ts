import { describe, expect, it } from 'vitest';

import {
  NO_OPTIONS,
  initialOptions,
  jobOptionsSpec,
  optionKeys,
  optionsSet,
  stepObstacleLaps,
  stepProgress,
  type JobOptionsSpec,
} from '#lib/mower/job-options.js';

/** `capabilities.start_options` of a LUBA 2 AWD on current firmware. */
const LUBA = {
  obstacle_detection: ['off', 'standard', 'sensitive'],
  obstacle_laps_range: [0, 4],
  orders: ['border_first', 'grid_first'],
  start_progress_range_pct: [0, 99],
};

const SPEC: JobOptionsSpec = {
  detection: ['off', 'standard', 'sensitive'],
  obstacleLaps: [0, 4],
  orders: ['border_first', 'grid_first'],
  progress: [0, 99],
};

describe('job options spec', () => {
  it('reads the declared options in the mower order', () => {
    expect(jobOptionsSpec(LUBA)).toEqual(SPEC);
    expect(
      jobOptionsSpec({ obstacle_detection: ['standard', 'off', 'eager', 'off'] })?.detection,
    ).toEqual(['standard', 'off']);
  });

  it('is null when the mower offers nothing', () => {
    expect(jobOptionsSpec(undefined)).toBeNull();
    expect(jobOptionsSpec(null)).toBeNull();
    expect(
      jobOptionsSpec({
        obstacle_detection: [],
        obstacle_laps_range: null,
        orders: [],
        start_progress_range_pct: null,
      }),
    ).toBeNull();
    // A progress range of only 0 % is no choice.
    expect(jobOptionsSpec({ start_progress_range_pct: [0, 0] })).toBeNull();
  });

  it('drops malformed ranges', () => {
    expect(jobOptionsSpec({ obstacle_laps_range: [3, 1] })).toBeNull();
    expect(jobOptionsSpec({ obstacle_laps_range: [0, 9] })).toBeNull();
    expect(jobOptionsSpec({ start_progress_range_pct: [0, 100] })).toBeNull();
    expect(jobOptionsSpec({ obstacle_laps_range: [0, 2] })?.obstacleLaps).toEqual([0, 2]);
  });
});

describe('initial options', () => {
  it('keeps what the mower offers from the saved defaults', () => {
    expect(
      initialOptions(
        { obstacle_detection: 'sensitive', obstacle_laps: 2, order: 'grid_first' },
        SPEC,
      ),
    ).toEqual({ detection: 'sensitive', obstacleLaps: 2, order: 'grid_first', progress: 0 });
  });

  it('leaves the rest to the mower and always opens at 0 %', () => {
    expect(initialOptions(null, SPEC)).toEqual(NO_OPTIONS);
    expect(
      initialOptions(
        { obstacle_detection: 'slow_touch', obstacle_laps: 7, start_progress_pct: 40 },
        SPEC,
      ),
    ).toEqual(NO_OPTIONS);
    const bare: JobOptionsSpec = { detection: [], obstacleLaps: null, orders: [], progress: null };
    expect(initialOptions({ obstacle_detection: 'off', order: 'grid_first' }, bare)).toEqual(
      NO_OPTIONS,
    );
  });
});

describe('option keys', () => {
  it('sends only what is set', () => {
    expect(optionKeys(NO_OPTIONS)).toEqual({});
    expect(optionKeys(undefined)).toEqual({});
    expect(
      optionKeys({ detection: 'standard', obstacleLaps: 0, order: 'border_first', progress: 35 }),
    ).toEqual({
      obstacle_detection: 'standard',
      obstacle_laps: 0,
      order: 'border_first',
      start_progress_pct: 35,
    });
    expect(optionsSet({ ...NO_OPTIONS, obstacleLaps: 0, progress: 10 })).toBe(2);
  });
});

describe('steps', () => {
  it('steps the no-go laps from the mower setting into the range', () => {
    expect(stepObstacleLaps(null, 1, [0, 4])).toBe(1);
    expect(stepObstacleLaps(null, -1, [0, 4])).toBe(0);
    expect(stepObstacleLaps(4, 1, [0, 4])).toBe(4);
    expect(stepObstacleLaps(2, -1, [0, 4])).toBe(1);
    expect(stepObstacleLaps(null, -1, [1, 4])).toBe(1);
  });

  it('steps the progress by five percent up to 99', () => {
    expect(stepProgress(0, 1, [0, 99])).toBe(5);
    expect(stepProgress(5, -1, [0, 99])).toBe(0);
    expect(stepProgress(0, -1, [0, 99])).toBe(0);
    expect(stepProgress(95, 1, [0, 99])).toBe(99);
    expect(stepProgress(99, -1, [0, 99])).toBe(95);
    expect(stepProgress(42, 1, [0, 99])).toBe(45);
  });
});
