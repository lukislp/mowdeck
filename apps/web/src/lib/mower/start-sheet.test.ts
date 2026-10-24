import { describe, expect, it, vi } from 'vitest';

import { ApiProblem } from '$lib/api/problem';
import { m } from '$lib/paraglide/messages';
import type { HourBar } from '$lib/schedules/preview';

import type { MowPath } from './mow-path';
import type { MowPattern } from './mow-pattern';
import {
  defaultsChanged,
  jobActive,
  jobMinutes,
  previewFailure,
  rainStatusOf,
} from './start-sheet';
import { StartSheetFlow, type StartSheetContext, type StartSheetDeps } from './start-sheet.svelte';

const SQUARE = [
  [0, 0],
  [10, 0],
  [10, 10],
  [0, 10],
];
const AREAS = [
  { id: 'a', boundary: SQUARE },
  { id: 'b', boundary: SQUARE },
];
const STRIPES: MowPattern = { name: 'stripes', angle_deg: 0, border_laps: 1 };
const DIAGONAL: MowPattern = { name: 'diagonal', angle_deg: 45, border_laps: 2 };
const PATH: MowPath = {
  polylines: [],
  kinds: [],
  points_total: 600,
  path_hash: 'h1',
  progress: null,
} as unknown as MowPath;

const NOON = Date.parse('2026-06-01T12:00:00Z');
const hour = (offset: number, wet: boolean, mm = 0): HourBar => ({
  start: new Date(NOON + offset * 3_600_000).toISOString(),
  end: new Date(NOON + (offset + 1) * 3_600_000).toISOString(),
  probability: wet ? 0.8 : 0.1,
  mm,
  wet,
});

function context(over: Partial<StartSheetContext> = {}): StartSheetContext {
  return {
    areas: AREAS,
    offered: ['stripes', 'diagonal'],
    spacing: null,
    savedPattern: null,
    bladeRange: [25, 70],
    speedRange: [0.2, 0.6],
    reportedBladeMm: 50,
    reportedSpeedMps: 0.4,
    cuttingWidthMm: 400,
    rain: null,
    ...over,
  };
}

interface Fake {
  deps: StartSheetDeps;
  /** Every command in the order it went out. */
  sent: string[];
  saved: MowPattern[];
  previews: { request: unknown; signal: AbortSignal }[];
  replaced: (MowPath | null)[];
  ended: number;
  now: number;
  ctx: StartSheetContext;
}

function fake(
  options: {
    ctx?: Partial<StartSheetContext>;
    preview?: StartSheetDeps['preview'];
    setpoint?: (kind: string) => boolean;
    start?: boolean;
    dock?: boolean;
  } = {},
): Fake {
  const f: Fake = {
    sent: [],
    saved: [],
    previews: [],
    replaced: [],
    ended: 0,
    now: NOON,
    ctx: context(options.ctx),
    deps: {
      preview: async (request, signal) => {
        f.previews.push({ request, signal });
        return options.preview ? options.preview(request, signal) : PATH;
      },
      setpoint: async (command) => {
        f.sent.push(command.kind);
        return options.setpoint ? options.setpoint(command.kind) : true;
      },
      start: async () => {
        f.sent.push('start_mow');
        return options.start ?? true;
      },
      dock: async (endJob) => {
        f.sent.push(`return_to_dock:${endJob}`);
        return options.dock ?? true;
      },
      saveDefaults: async (pattern) => {
        f.saved.push(pattern);
      },
      paths: {
        replace: (seed) => f.replaced.push(seed),
        ended: () => (f.ended += 1),
      },
      now: () => f.now,
      context: () => f.ctx,
    },
  };
  return f;
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('previewFailure', () => {
  it('names a timeout and an adapter without previews, else the problem', () => {
    expect(previewFailure(new ApiProblem({ status: 504, title: 'Timeout' }))).toBe(
      m.start_preview_timeout(),
    );
    expect(
      previewFailure(
        new ApiProblem({
          status: 409,
          title: 'Unsupported',
          type: 'urn:mowdeck:problem:path-preview-unsupported',
        }),
      ),
    ).toBe(m.start_preview_unsupported());
    expect(previewFailure(new ApiProblem({ status: 503, title: 'Down' }))).toBe(
      m.problem_unavailable(),
    );
  });
});

describe('defaultsChanged', () => {
  it('compares with the saved defaults, else with the fallback', () => {
    expect(defaultsChanged(STRIPES, null, ['stripes'], null)).toBe(false);
    expect(defaultsChanged(DIAGONAL, null, ['stripes', 'diagonal'], null)).toBe(true);
    expect(defaultsChanged(DIAGONAL, DIAGONAL, ['stripes', 'diagonal'], null)).toBe(false);
    expect(defaultsChanged(STRIPES, DIAGONAL, ['stripes', 'diagonal'], null)).toBe(true);
  });
});

describe('jobMinutes', () => {
  it('is unknown without picked areas and uses the preview when there is one', () => {
    const base = { areas: AREAS, cuttingWidthMm: 400, speedMps: 0.5 };
    expect(jobMinutes({ ...base, picked: [], pointsTotal: 600 })).toBeNull();
    // 600 points * 1 m / 0.5 m/s = 1200 s = 20 min, lifted to the 30 min floor.
    expect(jobMinutes({ ...base, picked: ['a'], pointsTotal: 600 })).toBe(30);
    // 200 m² / (0.4 m * 0.5 m/s) * 1.3 = 1300 s -> 21.7 min, lifted to 30.
    expect(jobMinutes({ ...base, picked: ['a', 'b'], pointsTotal: null })).toBe(30);
    expect(jobMinutes({ ...base, picked: ['a', 'b'], pointsTotal: null, speedMps: 0.05 })).toBe(
      Math.min(360, ((200 / (0.4 * 0.05)) * 1.3) / 60),
    );
  });
});

describe('rainStatusOf and jobActive', () => {
  it('maps the verdict to the readout status', () => {
    expect(rainStatusOf(null)).toBe('no_location');
    expect(rainStatusOf('disabled')).toBe('disabled');
    expect(rainStatusOf('unknown_run')).toBe('unavailable');
    expect(rainStatusOf('unknown_hold')).toBe('unavailable');
    expect(rainStatusOf('run')).toBe('ok');
    expect(rainStatusOf('hold')).toBe('ok');
  });

  it('needs the dock choice while a job exists', () => {
    expect(jobActive('mowing')).toBe(true);
    expect(jobActive('paused')).toBe(true);
    expect(jobActive('error')).toBe(true);
    expect(jobActive('idle')).toBe(false);
    expect(jobActive(null)).toBe(false);
  });
});

describe('StartSheetFlow', () => {
  it('opens with the known areas, the saved pattern and the reported setpoints', () => {
    const f = fake({ ctx: { savedPattern: DIAGONAL } });
    const flow = new StartSheetFlow(f.deps);
    flow.picked = ['b', 'gone'];
    flow.open();
    expect(flow.picking).toBe(true);
    expect(flow.picked).toEqual(['b']);
    expect(flow.patternDraft).toEqual({ name: 'diagonal', angle: 45, laps: 2, spacingMm: null });
    expect(flow.rememberPattern).toBe(true);
    expect(flow.setpointDraft).toEqual({ bladeMm: 50, speedMps: 0.4 });
    expect(flow.pattern).toEqual(DIAGONAL);
  });

  it('sends the changed setpoints one by one before the start', async () => {
    const f = fake();
    const flow = new StartSheetFlow(f.deps);
    flow.picked = ['a'];
    flow.open();
    flow.setpointDraft = { bladeMm: 60, speedMps: 0.5 };
    await flow.start();
    expect(f.sent).toEqual(['set_blade_height', 'set_speed', 'start_mow']);
    expect(flow.picking).toBe(false);
    expect(flow.picked).toEqual([]);
    expect(flow.starting).toBe(false);
  });

  it('sends only the setpoints that changed and stops when one does not go out', async () => {
    const f = fake({ setpoint: (kind) => kind !== 'set_speed' });
    const flow = new StartSheetFlow(f.deps);
    flow.picked = ['a'];
    flow.open();
    flow.setpointDraft = { bladeMm: 50, speedMps: 0.5 };
    await flow.start();
    expect(f.sent).toEqual(['set_speed']);
    // The sheet stays open for another try.
    expect(flow.picking).toBe(true);
    expect(flow.picked).toEqual(['a']);
    expect(f.saved).toEqual([]);
  });

  it('saves the defaults only after a start that went out, with the switch on and a change', async () => {
    const changed = fake();
    const flow = new StartSheetFlow(changed.deps);
    flow.picked = ['a'];
    flow.open();
    flow.patternDraft = { ...flow.patternDraft, name: 'diagonal', angle: 45 };
    await flow.start();
    expect(changed.saved).toEqual([{ name: 'diagonal', angle_deg: 45, border_laps: 1 }]);

    const unchanged = fake();
    const same = new StartSheetFlow(unchanged.deps);
    same.picked = ['a'];
    same.open();
    await same.start();
    expect(unchanged.saved).toEqual([]);

    const off = fake();
    const notRemembered = new StartSheetFlow(off.deps);
    notRemembered.picked = ['a'];
    notRemembered.open();
    notRemembered.patternDraft = { ...notRemembered.patternDraft, name: 'diagonal' };
    notRemembered.rememberPattern = false;
    await notRemembered.start();
    expect(off.saved).toEqual([]);

    const failed = fake({ start: false });
    const notSent = new StartSheetFlow(failed.deps);
    notSent.picked = ['a'];
    notSent.open();
    notSent.patternDraft = { ...notSent.patternDraft, name: 'diagonal' };
    await notSent.start();
    expect(failed.saved).toEqual([]);
    expect(notSent.picking).toBe(true);

    const bare = fake({ ctx: { offered: [] } });
    const noPattern = new StartSheetFlow(bare.deps);
    noPattern.picked = ['a'];
    noPattern.open();
    await noPattern.start();
    expect(noPattern.pattern).toBeNull();
    expect(bare.saved).toEqual([]);
  });

  it("computes a preview for the sheet's areas and pattern and keys it", async () => {
    const f = fake();
    const flow = new StartSheetFlow(f.deps);
    flow.picked = ['b', 'a'];
    flow.open();
    const key = flow.key;
    const run = flow.computePreview();
    expect(flow.preview).toEqual({ kind: 'loading', key });
    expect(f.replaced).toEqual([null]);
    await run;
    expect(f.previews[0]?.request).toEqual({ area_ids: ['b', 'a'], pattern: STRIPES });
    expect(flow.preview).toEqual({ kind: 'ready', key, path: PATH });
    expect(flow.previewPath).toBe(PATH);
    expect(flow.previewStale).toBe(false);
    expect(flow.jobMinutes).toBe(30);
    // Another area: the preview is outdated and not drawn; a start seeds no path from it.
    flow.togglePick('b');
    expect(flow.previewStale).toBe(true);
    expect(flow.previewPath).toBeNull();
    await flow.start();
    expect(f.replaced).toEqual([null, null]);
    expect(flow.preview).toEqual({ kind: 'idle' });
  });

  it('seeds the map with a preview that matches the start and drops a superseded one', async () => {
    const f = fake();
    const flow = new StartSheetFlow(f.deps);
    flow.picked = ['a'];
    flow.open();
    await flow.computePreview();
    await flow.start();
    expect(f.replaced).toEqual([null, PATH]);

    const slow = fake({
      preview: (_request, signal) =>
        new Promise((resolve, reject) => {
          signal.addEventListener('abort', () => reject(new Error('aborted')));
          setTimeout(() => resolve(PATH), 50);
        }),
    });
    const racing = new StartSheetFlow(slow.deps);
    racing.picked = ['a'];
    racing.open();
    const first = racing.computePreview();
    racing.togglePick('b');
    const second = racing.computePreview();
    await Promise.all([first, second]);
    expect(slow.previews[0]?.signal.aborted).toBe(true);
    expect(racing.preview).toEqual({ kind: 'ready', key: racing.key, path: PATH });
  });

  it('turns a failed preview into a message', async () => {
    const f = fake({
      preview: async () => {
        throw new ApiProblem({ status: 504, title: 'Timeout' });
      },
    });
    const flow = new StartSheetFlow(f.deps);
    flow.picked = ['a'];
    flow.open();
    await flow.computePreview();
    expect(flow.preview).toEqual({
      kind: 'error',
      key: flow.key,
      reason: m.start_preview_timeout(),
    });
  });

  it('asks twice when rain is expected within the job, with an injected clock', async () => {
    const rain = { bars: [hour(0, false), hour(1, true)], wetUntil: null };
    // At noon the wet hour starts in 60 min; the job (30 min) ends before: dry.
    const early = fake({ ctx: { rain } });
    const dry = new StartSheetFlow(early.deps);
    dry.picked = ['a'];
    dry.open();
    expect(dry.jobMinutes).toBe(30);
    expect(dry.startWarning).toBeNull();
    // Half an hour later the wet hour overlaps the job. (The page's clock is a signal; the
    // fake's is a plain number, so it is set before the first read.)
    const f = fake({ ctx: { rain } });
    f.now = NOON + 31 * 60_000;
    const flow = new StartSheetFlow(f.deps);
    flow.picked = ['a'];
    flow.open();
    expect(flow.startWarning).toMatchObject({ kind: 'soon', start: hour(1, true).start });
    expect(flow.rainConfirmed).toBe(false);
    flow.startClick();
    await flush();
    expect(f.sent).toEqual([]);
    expect(flow.rainConfirmed).toBe(true);
    // A changed selection asks again.
    flow.togglePick('b');
    expect(flow.rainConfirmed).toBe(false);
    flow.togglePick('b');
    expect(flow.rainConfirmed).toBe(false);
    flow.startClick();
    expect(flow.rainConfirmed).toBe(true);
    flow.startClick();
    await flush();
    expect(f.sent).toEqual(['start_mow']);
    expect(flow.picking).toBe(false);
    expect(flow.rainConfirmed).toBe(false);
  });

  it('starts at once without a warning and forgets the confirmation when the sheet closes', async () => {
    const f = fake({ ctx: { rain: { bars: [hour(0, true, 1)], wetUntil: null } } });
    const flow = new StartSheetFlow(f.deps);
    flow.picked = ['a'];
    flow.open();
    expect(flow.startWarning?.kind).toBe('now');
    flow.startClick();
    expect(flow.rainConfirmed).toBe(true);
    flow.close();
    expect(flow.rainConfirmed).toBe(false);
    flow.open();
    expect(flow.rainConfirmed).toBe(false);

    const dry = fake();
    const plain = new StartSheetFlow(dry.deps);
    plain.picked = ['a'];
    plain.open();
    plain.startClick();
    await flush();
    expect(dry.sent).toEqual(['start_mow']);
  });

  it('docks with the choice and drops the path only when the job ends', async () => {
    const f = fake();
    const flow = new StartSheetFlow(f.deps);
    flow.dockChoice = true;
    await flow.dock(false);
    expect(flow.dockChoice).toBe(false);
    expect(f.sent).toEqual(['return_to_dock:false']);
    expect(f.ended).toBe(0);
    await flow.dock(true);
    expect(f.ended).toBe(1);

    const failing = fake({ dock: false });
    const other = new StartSheetFlow(failing.deps);
    await other.dock(true);
    expect(failing.ended).toBe(0);
  });

  it('forgets the selection and the preview for another mower', async () => {
    const f = fake();
    const flow = new StartSheetFlow(f.deps);
    flow.picked = ['a'];
    flow.open();
    await flow.computePreview();
    flow.reset();
    expect(flow.picked).toEqual([]);
    expect(flow.preview).toEqual({ kind: 'idle' });
  });

  it('ignores a second start while one is on its way', async () => {
    const f = fake();
    const start = vi.fn(f.deps.start);
    f.deps.start = start;
    const flow = new StartSheetFlow(f.deps);
    flow.picked = ['a'];
    flow.open();
    const first = flow.start();
    expect(flow.starting).toBe(true);
    await flow.start();
    await first;
    expect(start).toHaveBeenCalledTimes(1);
  });
});

describe('StartSheetFlow job options', () => {
  const OPTIONS = {
    detection: ['off', 'standard', 'sensitive'],
    obstacleLaps: [0, 4],
    orders: ['border_first', 'grid_first'],
    progress: [0, 99],
  } as const satisfies StartSheetContext['jobOptions'];

  it('opens with the saved options, sends them and keeps the progress out of the defaults', async () => {
    const saved: MowPattern = { ...STRIPES, obstacle_detection: 'sensitive', order: 'grid_first' };
    const f = fake({
      ctx: {
        jobOptions: {
          ...OPTIONS,
          detection: [...OPTIONS.detection],
          orders: [...OPTIONS.orders],
        },
        savedPattern: saved,
      },
    });
    const requests: unknown[] = [];
    f.deps.start = async (request) => {
      requests.push(request);
      return true;
    };
    const flow = new StartSheetFlow(f.deps);
    flow.picked = ['a'];
    flow.open();
    expect(flow.patternDraft.options).toEqual({
      detection: 'sensitive',
      obstacleLaps: null,
      order: 'grid_first',
      progress: 0,
    });
    flow.patternDraft = {
      ...flow.patternDraft,
      options: { detection: 'sensitive', obstacleLaps: 0, order: 'grid_first', progress: 40 },
    };
    await flow.start();
    expect(requests).toEqual([
      {
        area_ids: ['a'],
        pattern: {
          ...STRIPES,
          obstacle_detection: 'sensitive',
          obstacle_laps: 0,
          order: 'grid_first',
          start_progress_pct: 40,
        },
      },
    ]);
    // The changed laps are remembered, the start progress is not.
    expect(f.saved).toEqual([{ ...saved, obstacle_laps: 0 }]);
  });

  it('does not save the defaults when only the start progress changed', async () => {
    const f = fake({
      ctx: {
        jobOptions: { ...OPTIONS, detection: [...OPTIONS.detection], orders: [...OPTIONS.orders] },
      },
    });
    const flow = new StartSheetFlow(f.deps);
    flow.picked = ['a'];
    flow.open();
    flow.patternDraft = {
      ...flow.patternDraft,
      options: { detection: null, obstacleLaps: null, order: null, progress: 50 },
    };
    await flow.start();
    expect(f.sent).toEqual(['start_mow']);
    expect(f.saved).toEqual([]);
  });

  it('counts a changed option as a change of the defaults', () => {
    const spec = { ...OPTIONS, detection: [...OPTIONS.detection], orders: [...OPTIONS.orders] };
    expect(defaultsChanged(STRIPES, null, ['stripes'], null, spec)).toBe(false);
    expect(
      defaultsChanged({ ...STRIPES, obstacle_detection: 'off' }, STRIPES, ['stripes'], null, spec),
    ).toBe(true);
    expect(
      defaultsChanged({ ...STRIPES, start_progress_pct: 30 }, STRIPES, ['stripes'], null, spec),
    ).toBe(false);
  });
});
