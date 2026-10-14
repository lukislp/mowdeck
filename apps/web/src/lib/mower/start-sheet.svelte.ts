/**
 * The start sheet's flow, without any markup: the picked areas, the pattern and setpoint drafts
 * the sheet opens with, "Pfad berechnen" (one preview at a time, keyed by areas and pattern),
 * the rain warning with its extra click, and "Starten" (the changed setpoints one after the
 * other, then `start_mow`, then the mow defaults when the rule says so). The dock choice ("end
 * the job?") lives here too. The page constructs it with the real API calls and binds to its
 * state; `start-sheet.test.ts` drives it with fakes.
 */
import { untrack } from 'svelte';

import { asProblem } from '$lib/api/problem';
import type { HourBar } from '$lib/schedules/preview';

import type { Range } from './limits';
import {
  commandClearsPath,
  currentPreview,
  previewKey,
  previewOutdated,
  type MowPath,
  type PreviewState,
} from './mow-path';
import {
  FALLBACK,
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

  /** The pattern the sheet would send now (null: the mower's own settings). */
  pattern = $derived.by((): MowPattern | null =>
    this.deps.context().offered.length ? toPattern(this.patternDraft) : null,
  );
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
    this.patternDraft = initialDraft(ctx.savedPattern, ctx.offered, ctx.spacing);
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

  /** Another mower: the selection and the preview belong to the old one. */
  reset(): void {
    this.picked = [];
    this.#previewAbort?.abort();
    this.preview = { kind: 'idle' };
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
      // The defaults change only with a start that went out, and only when they differ from
      // what the sheet opened with (the saved defaults, else the fallback).
      if (
        sent &&
        pattern &&
        remember &&
        defaultsChanged(pattern, ctx.savedPattern, ctx.offered, ctx.spacing)
      ) {
        await this.deps.saveDefaults(pattern);
      }
    } finally {
      this.starting = false;
    }
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
