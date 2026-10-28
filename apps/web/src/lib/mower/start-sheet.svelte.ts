/**
 * The start sheet's flow, without any markup: the picked areas, the pattern and setpoint drafts
 * the sheet opens with, "Pfad berechnen" (one preview at a time, keyed by areas and pattern),
 * the rain warning with its extra click, and "Starten" (the changed setpoints one after the
 * other, then `start_mow`, then the mow defaults when the rule says so). The dock choice ("end
 * the job?") lives here too. The page constructs it with the real API calls and binds to its
 * state; `start-sheet.test.ts` drives it with fakes.
 */
import { untrack } from 'svelte';

import { asProblem } from '#lib/api/problem.js';
import type { HourBar } from '#lib/schedules/preview.js';
import {
  DEFAULT_STEP,
  nextManualAngle,
  validStep,
  type MowerAngles,
} from '#lib/schedules/rotation.js';

import type { Range } from './limits';
import {
  commandClearsPath,
  currentPreview,
  previewKey,
  previewOutdated,
  type MowPath,
  type PreviewState,
} from './mow-path';
import type { JobOptionsSpec } from './job-options';
import {
  FALLBACK,
  angleApplies,
  defaultsOf,
  initialDraft,
  toPattern,
  type MowPattern,
  type PatternDraft,
  type PatternName,
  type SpacingRange,
} from './mow-pattern';
import { rainWarning, type RainWarning } from './rain';
import {
  initialSetpoints,
  setpointCommands,
  type SetpointCommand,
  type SetpointDraft,
} from './setpoints';
import {
  defaultsChanged,
  jobMinutes,
  previewFailure,
  startRequest,
  type PreviewRequest,
  type SheetArea,
  type StartRequest,
} from './start-sheet';

/** What the page knows about the mower; read whenever the flow needs it (so it stays current). */
export interface StartSheetContext {
  areas: readonly SheetArea[];
  /** Patterns the mower runs natively (none: the start names no pattern). */
  offered: readonly PatternName[];
  spacing: SpacingRange | null;
  /** The job options the mower offers (`capabilities.start_options`), or null for none. */
  jobOptions?: JobOptionsSpec | null;
  /** The mower's saved mow defaults, or null. */
  savedPattern: MowPattern | null;
  bladeRange: Range | null;
  speedRange: Range | null;
  /** The reported blade height and speed (the sheet opens with them). */
  reportedBladeMm: number | null | undefined;
  reportedSpeedMps: number | null | undefined;
  cuttingWidthMm: number | null | undefined;
  /** The rain bars and the estimated dry time, when the weather is known; else null. */
  rain: { bars: readonly HourBar[]; wetUntil: string | null } | null;
}

export interface StartSheetDeps {
  /** `POST /mowers/{id}/path-preview`. */
  preview(request: PreviewRequest, signal: AbortSignal): Promise<MowPath>;
  /** Send one setpoint command; false when it did not go out. */
  setpoint(command: SetpointCommand): Promise<boolean>;
  /** Send `start_mow`; false when it did not go out. */
  start(request: StartRequest): Promise<boolean>;
  /** Send `return_to_dock`; false when it did not go out. */
  dock(endJob: boolean): Promise<boolean>;
  /** `PUT /mowers/{id}/mow-defaults` (reports its own failure). */
  saveDefaults(pattern: MowPattern): Promise<void>;
  /** The planned path on the map. */
  paths: {
    /** A new preview or start replaces the last job's path; a start seeds it with the preview. */
    replace(seed: MowPath | null): void;
    /** The owner ended the job: its path goes and is not shown again. */
    ended(): void;
  };
  /** Epoch ms of now, for the rain warning. */
  now(): number;
  context(): StartSheetContext;
  /** The mower's angle rotation in the Scheduler (ADR 0061); absent: no rotation offered. */
  rotation?: RotationDeps;
}

/** The Scheduler's angle rotation of the mower (`/scheduler/mowers/{id}/angles`). */
export interface RotationDeps {
  /** The last angle per area and the step of manual starts. */
  load(): Promise<MowerAngles>;
  /** Remember the step of manual starts (null: rotation off). */
  saveStep(step: number | null): Promise<void>;
  /** A start with rotation chosen mowed `areaIds` with `angle`. */
  record(areaIds: string[], angle: number): Promise<void>;
}

export class StartSheetFlow {
  /** The sheet is open. */
  picking = $state(false);
  #picked = $state<string[]>([]);
  #patternDraft = $state<PatternDraft>({ ...FALLBACK });
  dockChoice = $state(false);
  rememberPattern = $state(true);
  /** Blade height and speed of the sheet, and what it opened with (only changes are sent). */
  setpointDraft = $state<SetpointDraft>({ bladeMm: null, speedMps: null });
  #setpointStart: SetpointDraft = { bladeMm: null, speedMps: null };
  starting = $state(false);
  preview = $state.raw<PreviewState>({ kind: 'idle' });
  #previewAbort: AbortController | null = null;
  /** The extra click a rain warning asks for was given. */
  rainConfirmed = $state(false);
  /** Rotate the angle of this start by the mower's step (ADR 0061). */
  rotate = $state(false);
  /** The step of manual starts in degrees. */
  rotateStep = $state<number>(DEFAULT_STEP);
  /** Where the mower's rotation stands; null until loaded. */
  rotation = $state.raw<MowerAngles | null>(null);
  /** Loading the rotation failed: the sheet offers none this time. */
  rotationFailed = $state(false);
  #rotationLoad = 0;

  constructor(private readonly deps: StartSheetDeps) {}

  /** The chosen areas, in start order (also while the sheet is closed: taps on the map). */
  get picked(): string[] {
    return this.#picked;
  }
  set picked(ids: string[]) {
    const before = untrack(() => this.key);
    this.#picked = ids;
    this.#selectionChanged(before);
  }

  /** The pattern settings: filled from the mower's mow defaults when the sheet opens. */
  get patternDraft(): PatternDraft {
    return this.#patternDraft;
  }
  set patternDraft(draft: PatternDraft) {
    const before = untrack(() => this.key);
    this.#patternDraft = draft;
    this.#selectionChanged(before);
  }

  /**
   * A changed selection (other areas or another pattern) asks for the confirmation again. The
   * comparison reads `key` untracked: a write to the sheet (also `reset()` from the page's
   * mower effect) must not subscribe the caller's effect to the selection, the pattern or the
   * context behind them; otherwise that effect re-runs on every pick and resets it again.
   */
  #selectionChanged(before: string): void {
    if (untrack(() => this.key) !== before) this.rainConfirmed = false;
  }

  /** The sheet can rotate the angle: the Scheduler answered and the pattern has a direction. */
  rotationOffered = $derived.by(
    () =>
      !!this.deps.rotation &&
      this.rotation !== null &&
      this.deps.context().offered.length > 0 &&
      angleApplies(this.patternDraft.name),
  );
  /**
   * The angle a start with rotation sends: the last angle of the picked areas turned by the
   * step, else the sheet's angle. Null when rotation is off or not offered, or nothing is picked.
   */
  rotatedAngle = $derived.by((): number | null =>
    this.rotate && this.rotationOffered
      ? nextManualAngle(this.rotation, this.picked, this.rotateStep, this.patternDraft.angle ?? 0)
      : null,
  );
  /** The pattern the sheet would send now (null: the mower's own settings). */
  pattern = $derived.by((): MowPattern | null => {
    if (!this.deps.context().offered.length) return null;
    const pattern = toPattern(this.patternDraft);
    return this.rotatedAngle === null ? pattern : { ...pattern, angle_deg: this.rotatedAngle };
  });
  key = $derived(previewKey(this.picked, this.pattern));
  /** The preview to draw while the sheet is open. */
  previewPath = $derived(this.picking ? currentPreview(this.preview, this.key) : null);
  previewStale = $derived(previewOutdated(this.preview, this.key));
  /** Estimated job length in minutes for the picked areas (null: unknown). */
  jobMinutes = $derived.by((): number | null => {
    if (!this.picking) return null;
    const ctx = this.deps.context();
    return jobMinutes({
      areas: ctx.areas,
      picked: this.picked,
      pointsTotal:
        this.preview.kind === 'ready' && !this.previewStale ? this.preview.path.points_total : null,
      cuttingWidthMm: ctx.cuttingWidthMm,
      speedMps: this.setpointDraft.speedMps ?? ctx.reportedSpeedMps ?? ctx.speedRange?.[1] ?? null,
    });
  });
  /** Rain now, a wet hour within the job or a wet lawn; null when dry or unknown. */
  startWarning = $derived.by((): RainWarning | null => {
    const rain = this.picking ? this.deps.context().rain : null;
    return rain ? rainWarning(rain.bars, this.deps.now(), this.jobMinutes, rain.wetUntil) : null;
  });

  /** Open the sheet with the areas tapped on the map and the mower's current settings. */
  open(): void {
    const ctx = this.deps.context();
    const known = new Set(ctx.areas.map((a) => a.id));
    this.picked = this.picked.filter((areaId) => known.has(areaId));
    this.patternDraft = initialDraft(ctx.savedPattern, ctx.offered, ctx.spacing, ctx.jobOptions);
    this.rememberPattern = true;
    this.#setpointStart = initialSetpoints(
      ctx.reportedBladeMm,
      ctx.reportedSpeedMps,
      ctx.bladeRange,
      ctx.speedRange,
    );
    this.setpointDraft = { ...this.#setpointStart };
    this.rainConfirmed = false;
    this.picking = true;
    void this.loadRotation();
  }

  /** Read where the mower's rotation stands; the sheet opens with the mower's step. */
  async loadRotation(): Promise<void> {
    const rotation = this.deps.rotation;
    if (!rotation) return;
    const load = ++this.#rotationLoad;
    try {
      const state = await rotation.load();
      if (load !== this.#rotationLoad) return;
      this.rotation = state;
      this.rotationFailed = false;
      this.rotate = validStep(state.manual_step_deg);
      this.rotateStep = validStep(state.manual_step_deg) ? state.manual_step_deg : DEFAULT_STEP;
    } catch {
      if (load !== this.#rotationLoad) return;
      this.rotation = null;
      this.rotationFailed = true;
      this.rotate = false;
    }
  }

  /** A closed sheet asks for the rain confirmation again when it opens. */
  close(): void {
    this.picking = false;
    this.rainConfirmed = false;
  }

  togglePick(areaId: string): void {
    this.picked = this.picked.includes(areaId)
      ? this.picked.filter((a) => a !== areaId)
      : [...this.picked, areaId];
  }

  /** Another mower: the selection, the preview and the rotation belong to the old one. */
  reset(): void {
    this.picked = [];
    this.#previewAbort?.abort();
    this.preview = { kind: 'idle' };
    this.#rotationLoad++;
    this.rotation = null;
    this.rotationFailed = false;
    this.rotate = false;
  }

  /** "Pfad berechnen": the mower plans the sheet's areas and pattern without starting. */
  async computePreview(): Promise<void> {
    const key = this.key;
    const request: PreviewRequest = startRequest(this.picked, this.pattern);
    // A new preview replaces the last job's path on the map.
    this.deps.paths.replace(null);
    this.#previewAbort?.abort();
    const abort = new AbortController();
    this.#previewAbort = abort;
    this.preview = { kind: 'loading', key };
    try {
      const path = await this.deps.preview(request, abort.signal);
      if (abort.signal.aborted) return;
      this.preview = { kind: 'ready', key, path };
    } catch (error) {
      if (abort.signal.aborted) return;
      this.preview = { kind: 'error', key, reason: previewFailure(asProblem(error)) };
    }
  }

  /** "Starten": with a rain warning the first click only asks again ("Trotzdem starten"). */
  startClick(): void {
    if (this.startWarning && !this.rainConfirmed) {
      this.rainConfirmed = true;
      return;
    }
    void this.start();
  }

  async start(): Promise<void> {
    if (this.starting) return;
    const ctx = this.deps.context();
    const areaIds = [...this.picked];
    const pattern = this.pattern;
    const remember = this.rememberPattern;
    const rotated = this.rotatedAngle;
    const loadedStep = this.rotation?.manual_step_deg ?? null;
    const chosenStep = this.rotationOffered && this.rotate ? this.rotateStep : null;
    const stepChanged = this.rotationOffered && chosenStep !== loadedStep;
    const setpoints = setpointCommands(this.setpointDraft, this.#setpointStart);
    this.starting = true;
    try {
      // Setpoints first, one after the other, then the start: the controller runs a mower's
      // commands in arrival order, so the job starts with the new blade height and speed.
      for (const command of setpoints) {
        if (!(await this.deps.setpoint(command))) return;
      }
      // The previewed route stays on the map until the job's own path arrives; an older job's
      // path goes with the new start.
      this.deps.paths.replace(currentPreview(this.preview, previewKey(areaIds, pattern)));
      this.#previewAbort?.abort();
      this.preview = { kind: 'idle' };
      const sent = await this.deps.start(startRequest(areaIds, pattern));
      if (sent) {
        this.picking = false;
        this.picked = [];
        this.rainConfirmed = false;
      }
      // The rotated angle belongs to this start, not to the defaults: they keep the sheet's angle.
      const remembered =
        pattern && rotated !== null
          ? { ...pattern, angle_deg: toPattern(this.patternDraft).angle_deg }
          : pattern;
      // The defaults change only with a start that went out, and only when they differ from
      // what the sheet opened with (the saved defaults, else the fallback).
      if (
        sent &&
        remembered &&
        remember &&
        defaultsChanged(remembered, ctx.savedPattern, ctx.offered, ctx.spacing, ctx.jobOptions)
      ) {
        await this.deps.saveDefaults(defaultsOf(remembered));
      }
      if (sent) await this.#rotationSent(areaIds, rotated, stepChanged ? chosenStep : undefined);
    } finally {
      this.starting = false;
    }
  }

  /**
   * After a start went out: report the rotated angle (a start with an explicit angle is not
   * reported, so the rotation stays where it was) and remember a changed step. A failure here
   * does not undo the start; the next start just continues from the older angle.
   */
  async #rotationSent(
    areaIds: string[],
    angle: number | null,
    step: number | null | undefined,
  ): Promise<void> {
    const rotation = this.deps.rotation;
    if (!rotation) return;
    try {
      if (step !== undefined) await rotation.saveStep(step);
      if (angle !== null) await rotation.record(areaIds, angle);
    } catch {
      // Reported by the API client; nothing to roll back.
    }
    this.rotation = null;
  }

  /** The dock choice's answer: to the station, ending the job or keeping it. */
  async dock(endJob: boolean): Promise<void> {
    this.dockChoice = false;
    const sent = await this.deps.dock(endJob);
    if (sent && commandClearsPath({ kind: 'return_to_dock', end_job: endJob })) {
      this.deps.paths.ended();
    }
  }
}
