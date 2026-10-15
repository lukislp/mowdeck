/**
 * The spot sheet's flow, without any markup: spot mode on the map (a circle around a tap, or a
 * polygon of up to eight taps, or the polygon of a missed spot), the pattern, spacing, speed and
 * blade settings, the debounced Planner preview (one at a time, keyed by the whole request), the
 * start conditions, "Starten" (`POST /planner/plans`, then the `spot_mow` command), the STOP of a
 * running spot run and the end notice of the run this page watched. The page constructs it with
 * the real API calls and binds to its state; `spot-sheet.test.ts` drives it with fakes.
 */
import { untrack } from 'svelte';

import { asProblem } from '$lib/api/problem';
import { problemMessage } from '$lib/i18n';
import {
  containingArea,
  spotCenter,
  spotComplete,
  type GeoReference,
  type LocalPoint,
  type MapSpot,
} from '@mowdeck/ui';

import type { MowPath } from './mow-path';
import {
  SPOT_CORNERS_MAX,
  SPOT_LAUNCH_WAIT_MS,
  SPOT_PREVIEW_DEBOUNCE_MS,
  SPOT_RADIUS_DEFAULT_M,
  allGreen,
  bladesRequested,
  clampRadiusM,
  clampSpacingMm,
  clampSpeedMps,
  initialSettings,
  isSpotRunActive,
  isSpotRunFinal,
  planRequest,
  previewMowPath,
  spotCommand,
  spotConditions,
  spotFromGap,
  spotPreviewKey,
  spotTarget,
  type PlanOut,
  type PlanRequest,
  type PreviewOut,
  type SpotCondition,
  type SpotMowCommand,
  type SpotRunStatus,
  type SpotSettings,
  type ConditionState,
} from './spot-sheet';
import type { SheetArea } from './start-sheet';

/** What the page knows about the mower; read whenever the flow needs it (so it stays current). */
export interface SpotSheetContext {
  mowerId: string;
  reference: GeoReference | null;
  areas: readonly SheetArea[];
  capabilities: {
    manual_drive?: boolean;
    manual_drive_blades?: boolean;
    cutting_width_mm?: number | null;
    links_supported?: string[];
  } | null;
  state: ConditionState | null;
  /** This browser drives the mower with the joystick right now. */
  driveSession: boolean;
}

export interface SpotSheetDeps {
  /** `POST /planner/plans/preview`. */
  preview(request: PlanRequest, signal: AbortSignal): Promise<PreviewOut>;
  /** `POST /planner/plans`. */
  plan(request: PlanRequest): Promise<PlanOut>;
  /** Send `spot_mow` or `spot_stop`; false when it did not go out. */
  command(command: SpotMowCommand | { kind: 'spot_stop' }): Promise<boolean>;
  /** The planned path on the map: a start seeds it with the preview. */
  paths: { replace(seed: MowPath | null): void };
  /** Epoch ms of now. */
  now(): number;
  context(): SpotSheetContext;
  /** Run `fn` after `ms` (injectable in tests); returns the cancel function. */
  schedule?(fn: () => void, ms: number): () => void;
}

export type SpotMode = 'off' | 'circle' | 'polygon';

export type SpotPreviewState =
  | { kind: 'idle' }
  | { kind: 'loading'; key: string }
  | { kind: 'ready'; key: string; out: PreviewOut; path: MowPath }
  | { kind: 'error'; key: string; reason: string };

export class SpotSheetFlow {
  /** Spot mode: taps on the map place a circle or the corners of a polygon. */
  mode = $state<SpotMode>('off');
  /** The sheet is open (spot mode stays on while it is closed, so the map can be tapped). */
  open = $state(false);
  spot = $state.raw<MapSpot | null>(null);
  radiusM = $state(SPOT_RADIUS_DEFAULT_M);
  settings = $state<SpotSettings>(initialSettings(null));
  preview = $state.raw<SpotPreviewState>({ kind: 'idle' });
  starting = $state(false);
  /** Why the last start did not go out (the Planner refused), shown in the sheet. */
  startError = $state<string | null>(null);
  stopping = $state(false);
  /** The run this page watched that has ended (shown until dismissed or the next run). */
  ended = $state.raw<SpotRunStatus | null>(null);
  /** The spot of the last run started here, for "Nachmähen" again. */
  lastSpot = $state.raw<MapSpot | null>(null);
  /**
   * A run was started here and its first status has not arrived yet: the run bar says it is
   * starting (and waits for live positions) and already offers STOP. Ends when the run shows up
   * in the state, or after `SPOT_LAUNCH_WAIT_MS` without it (the command result says why).
   */
  launching = $state(false);

  #previewAbort: AbortController | null = null;
  #cancelDebounce: (() => void) | null = null;
  /** A final run already shown (or present at page load) is not announced again. */
  #ignoredRunId: string | null | undefined = undefined;
  #cancelLaunch: (() => void) | null = null;

  constructor(private readonly deps: SpotSheetDeps) {}

  get active(): boolean {
    return this.mode !== 'off';
  }

  bladesAllowed = $derived.by(() => this.deps.context().capabilities?.manual_drive_blades === true);
  cuttingWidthMm = $derived.by(() => this.deps.context().capabilities?.cutting_width_mm ?? null);
  /** The mapped area that holds the whole spot (the Planner clips to it), or null. */
  areaId = $derived.by(() => containingArea(this.spot, this.deps.context().areas));
  complete = $derived(spotComplete(this.spot));
  corners = $derived(this.spot?.kind === 'polygon' ? this.spot.points.length : 0);
  request = $derived.by((): PlanRequest | null =>
    planRequest({
      mowerId: this.deps.context().mowerId,
      spot: this.spot,
      areaId: this.areaId,
      settings: this.settings,
      cuttingWidthMm: this.cuttingWidthMm,
    }),
  );
  key = $derived(spotPreviewKey(this.request));
  /** The preview to draw while spot mode is on: only one computed for the current request. */
  previewPath = $derived(
    this.active && this.preview.kind === 'ready' && this.preview.key === this.key
      ? this.preview.path
      : null,
  );
  previewReady = $derived(
    this.preview.kind === 'ready' && this.preview.key === this.key ? this.preview.out : null,
  );
  conditions = $derived.by((): SpotCondition[] => {
    const ctx = this.deps.context();
    return spotConditions({
      spot: this.spot,
      areaId: this.areaId,
      areas: ctx.areas,
      reference: ctx.reference,
      state: ctx.state,
      linksSupported: ctx.capabilities?.links_supported,
      driveSession: ctx.driveSession,
      now: this.deps.now(),
    });
  });
  /** Blades really run: allowed, switched on and confirmed. */
  bladesOn = $derived(bladesRequested(this.settings, this.bladesAllowed));
  canStart = $derived(
    this.request !== null &&
      allGreen(this.conditions) &&
      this.previewReady !== null &&
      !this.starting,
  );
  run = $derived.by(() => this.deps.context().state?.spot_run ?? null);
  running = $derived(isSpotRunActive(this.run));

  /**
   * Ask for a preview whenever the request changes while the sheet is open, a moment after the
   * last change. Call once from the component (it registers an `$effect`); tests wrap it in
   * `$effect.root`.
   */
  autoPreview(): void {
    $effect(() => {
      const key = this.key;
      const open = this.open;
      untrack(() => this.#requestChanged(key, open));
    });
  }

  #requestChanged(key: string, open: boolean): void {
    this.#cancelDebounce?.();
    this.#cancelDebounce = null;
    if (!open || !key) {
      this.#previewAbort?.abort();
      this.#previewAbort = null;
      if (this.preview.kind === 'loading') this.preview = { kind: 'idle' };
      return;
    }
    if (this.preview.kind !== 'idle' && this.preview.key === key) return;
    this.#cancelDebounce = this.#schedule(
      () => void this.computePreview(),
      SPOT_PREVIEW_DEBOUNCE_MS,
    );
  }

  #schedule(fn: () => void, ms: number): () => void {
    if (this.deps.schedule) return this.deps.schedule(fn, ms);
    const timer = setTimeout(fn, ms);
    return () => clearTimeout(timer);
  }

  #launch(): void {
    this.#cancelLaunch?.();
    this.launching = true;
    this.#cancelLaunch = this.#schedule(() => this.#endLaunch(), SPOT_LAUNCH_WAIT_MS);
  }

  #endLaunch(): void {
    this.#cancelLaunch?.();
    this.#cancelLaunch = null;
    if (this.launching) this.launching = false;
  }

  /** The Planner computes the route for the current spot and settings without starting. */
  async computePreview(): Promise<void> {
    const request = this.request;
    const key = this.key;
    if (!request) return;
    this.#previewAbort?.abort();
    const abort = new AbortController();
    this.#previewAbort = abort;
    this.preview = { kind: 'loading', key };
    try {
      const out = await this.deps.preview(request, abort.signal);
      if (abort.signal.aborted) return;
      const path = previewMowPath(out, new Date(this.deps.now()).toISOString());
      this.preview = { kind: 'ready', key, out, path };
    } catch (error) {
      if (abort.signal.aborted) return;
      this.preview = { kind: 'error', key, reason: problemMessage(asProblem(error)) };
    }
  }

  // --- spot mode ------------------------------------------------------------------------------

  /** "Spot mähen": the next tap on the map places a circle. */
  begin(): void {
    this.#endLaunch();
    this.settings = initialSettings(untrack(() => this.cuttingWidthMm));
    this.radiusM = SPOT_RADIUS_DEFAULT_M;
    this.spot = null;
    this.startError = null;
    this.ended = null;
    this.mode = 'circle';
    this.open = false;
  }

  /** Leave spot mode: the spot and the preview go. */
  cancel(): void {
    this.mode = 'off';
    this.open = false;
    this.spot = null;
    this.startError = null;
    this.#previewAbort?.abort();
    this.#previewAbort = null;
    this.preview = { kind: 'idle' };
  }

  /** The sheet closes but spot mode stays (more taps on the map, or "Einstellungen" again). */
  close(): void {
    this.open = false;
  }

  /** A tap on the map: the circle's centre, or the next corner of the polygon. */
  mapTap(local: [number, number]): void {
    if (this.mode === 'circle') {
      this.spot = { kind: 'circle', center: local, radius_m: this.radiusM };
      this.open = true;
      return;
    }
    if (this.mode === 'polygon') {
      const points: LocalPoint[] = this.spot?.kind === 'polygon' ? [...this.spot.points] : [];
      if (points.length >= SPOT_CORNERS_MAX) return;
      points.push(local);
      this.spot = { kind: 'polygon', points };
      if (points.length >= 3) this.open = true;
    }
  }

  /** "Form zeichnen": the next taps are the corners of a polygon (up to eight). */
  drawShape(): void {
    this.mode = 'polygon';
    this.spot = null;
    this.open = false;
  }

  /** Back to a circle, around the drawn shape's centre when there is one. */
  useCircle(): void {
    const previous = this.spot;
    this.mode = 'circle';
    this.spot = previous
      ? { kind: 'circle', center: spotCenter(previous), radius_m: this.radiusM }
      : null;
  }

  /** The last corner goes; with none left the polygon starts over. */
  undoCorner(): void {
    if (this.spot?.kind !== 'polygon') return;
    const points = this.spot.points.slice(0, -1);
    this.spot = points.length ? { kind: 'polygon', points } : null;
    if (points.length < 3) this.open = false;
  }

  setRadius(value: number): void {
    this.radiusM = clampRadiusM(value);
    if (this.spot?.kind === 'circle') this.spot = { ...this.spot, radius_m: this.radiusM };
  }

  setSpacing(value: number): void {
    this.settings = {
      ...this.settings,
      spacingMm: clampSpacingMm(
        value,
        untrack(() => this.cuttingWidthMm),
      ),
    };
  }

  setSpeed(value: number): void {
    this.settings = { ...this.settings, speedMps: clampSpeedMps(value) };
  }

  setBlades(on: boolean): void {
    this.settings = {
      ...this.settings,
      blades: on,
      confirmBlades: on ? this.settings.confirmBlades : false,
    };
  }

  /** "Nachmähen" from the missed spots: the gap's polygon becomes the spot. */
  fromGap(gap: { polygon: readonly (readonly number[])[] }): void {
    this.begin();
    this.mode = 'polygon';
    this.spot = spotFromGap(gap);
    this.open = true;
  }

  /** "Nachmähen" again after a run: the same spot, the settings start over. */
  remowLast(): void {
    const spot = this.lastSpot;
    if (!spot) return;
    this.begin();
    this.mode = spot.kind;
    if (spot.kind === 'circle') this.radiusM = spot.radius_m;
    this.spot = spot;
    this.open = true;
  }

  // --- start and stop --------------------------------------------------------------------------

  /** `POST /planner/plans`, then `spot_mow` with the plan inline (the gateway drives it). */
  async start(): Promise<void> {
    if (this.starting) return;
    const request = this.request;
    const spot = this.spot;
    const ctx = this.deps.context();
    if (!request || !spot || !allGreen(this.conditions)) return;
    const settings = { ...this.settings };
    const bladesAllowed = this.bladesAllowed;
    const seed = this.previewPath;
    this.starting = true;
    this.startError = null;
    try {
      const plan = await this.deps.plan(request);
      const target = ctx.reference ? spotTarget(spot, ctx.reference) : null;
      const sent = await this.deps.command(spotCommand({ plan, settings, bladesAllowed, target }));
      if (!sent) return;
      this.lastSpot = spot;
      this.deps.paths.replace(seed);
      this.#ignoredRunId = null;
      this.cancel();
      this.#launch();
    } catch (error) {
      this.startError = problemMessage(asProblem(error));
    } finally {
      this.starting = false;
    }
  }

  /** The big STOP of the run bar. */
  async stop(): Promise<void> {
    if (this.stopping) return;
    this.stopping = true;
    try {
      await this.deps.command({ kind: 'spot_stop' });
    } finally {
      this.stopping = false;
    }
  }

  /**
   * Feed every state's `spot_run`: a run that turns final while this page watches it (or that
   * this page started) becomes the end notice; a final run already there when the page opened
   * is not announced. Call from an effect on the live state.
   */
  observe(run: SpotRunStatus | null | undefined): void {
    if (this.#ignoredRunId === undefined)
      this.#ignoredRunId = isSpotRunFinal(run) ? run!.run_id : null;
    if (!run) return;
    if (isSpotRunActive(run)) {
      this.#endLaunch();
      this.#ignoredRunId = null;
      if (this.ended) this.ended = null;
      return;
    }
    if (run.run_id === this.#ignoredRunId) return;
    this.#endLaunch();
    if (this.ended?.run_id !== run.run_id) this.ended = run;
  }

  dismissEnd(): void {
    if (this.ended) this.#ignoredRunId = this.ended.run_id;
    this.ended = null;
  }

  /** Another mower: the spot, the preview and the run belong to the old one. */
  reset(): void {
    this.cancel();
    this.#endLaunch();
    this.lastSpot = null;
    this.ended = null;
    this.#ignoredRunId = undefined;
  }
}
