/**
 * The calibration card's flow, without any markup: choose a method, "Kalibrieren" captures the
 * current pair and runs the method, the result waits as a candidate (with the captured pair for
 * the before/after preview) until the user applies or discards it. Applying turns the result
 * into the {@link PanoramaCalibration} the panel stores; nothing is stored before. Discarding
 * (or starting over) aborts a running method through its signal, so a pending AI call is
 * cancelled and a late answer never becomes a candidate.
 *
 * The `manual` method's "Auto" ({@link CalibrationFlow.autoOverlap}) lives here too: the overlap
 * estimate from the two live pictures with its outcome for the card. The DOM grabs (the frames,
 * the small greyscale copies) are injected, so the flow is testable without a document.
 */
import { estimateOverlap, flipGray, type GrayImage } from '../panorama';
import type { CapturedPair } from './capture';
import type { CalibrationMethods } from './methods';
import { calibrationOf } from './storage';
import {
  CalibrationError,
  type CalibrationFailure,
  type CalibrationOptions,
  type CalibrationResult,
  type ChoosableMethod,
  type PanoramaCalibration,
} from './types';

export type FlowPhase = 'idle' | 'running' | 'result' | 'error';

/** The outcome of the `manual` method's "Auto" overlap estimate. */
export interface AutoOverlap {
  /** `limit`: the best match lies at the edge of the searched range. */
  kind: 'done' | 'limit' | 'failed';
  /** The estimated overlap in percent of the frame width (0 for `failed`). */
  percent: number;
  /** The correlation at that overlap (0 for `failed`). */
  score: number;
}

export interface FlowDeps {
  methods: CalibrationMethods;
  /** Grab both live pictures (null when one has none). */
  capture(): CapturedPair | null;
  /** Small greyscale copies of both live pictures for the overlap estimate (null without one). */
  grab(): readonly [GrayImage | null, GrayImage | null];
  /** The current mirror switch and slider settings. */
  options(): Omit<CalibrationOptions, 'signal'>;
}

export class CalibrationFlow {
  method = $state<ChoosableMethod>('manual');
  phase = $state<FlowPhase>('idle');
  candidate = $state<CalibrationResult | null>(null);
  pair = $state.raw<CapturedPair | null>(null);
  failure = $state<CalibrationFailure | null>(null);
  /** The message of the error behind `failure` (what the console shows, for a `failed`). */
  detail = $state<string | null>(null);
  /** What the last "Auto" overlap estimate found (null until one ran). */
  auto = $state<AutoOverlap | null>(null);
  #run = 0;
  #abort: AbortController | null = null;

  constructor(private readonly deps: FlowDeps) {}

  /** Switch the method; a pending result or error belongs to the old one and goes away. */
  choose(method: ChoosableMethod): void {
    if (method === this.method) return;
    this.discard();
    this.method = method;
  }

  /** Capture the pair and estimate with the chosen method. */
  async run(): Promise<void> {
    this.#abort?.abort();
    const abort = new AbortController();
    this.#abort = abort;
    const run = ++this.#run;
    this.candidate = null;
    this.failure = null;
    this.detail = null;
    const pair = this.deps.capture();
    if (!pair) {
      this.pair = null;
      this.phase = 'error';
      this.failure = 'no_picture';
      return;
    }
    this.pair = pair;
    this.phase = 'running';
    try {
      const result = await this.deps.methods[this.method].estimate(pair.left, pair.right, {
        ...this.deps.options(),
        signal: abort.signal,
      });
      if (run !== this.#run) return;
      this.candidate = result;
      this.phase = 'result';
    } catch (error) {
      if (run !== this.#run) return;
      this.phase = 'error';
      this.failure = error instanceof CalibrationError ? error.reason : 'failed';
      this.detail = error instanceof Error ? error.message : String(error);
      console.error('[mowdeck] camera calibration failed', error);
    }
  }

  /**
   * "Auto" of the `manual` method: estimate the overlap from the two current pictures (the
   * cross-correlation of their facing edges at the sliders' vertical offset, the right one
   * flipped back for a mirrored camera). Returns the overlap as a fraction of the frame width for
   * the sliders, or null when nothing matched; {@link auto} says what happened.
   */
  autoOverlap(): number | null {
    const [left, grabbed] = this.deps.grab();
    const options = this.deps.options();
    const right = grabbed && options.mirror ? flipGray(grabbed) : grabbed;
    const estimate =
      left && right ? estimateOverlap(left, right, { offset: options.manual.offset }) : null;
    if (!estimate) {
      this.auto = { kind: 'failed', percent: 0, score: 0 };
      return null;
    }
    this.auto = {
      kind: estimate.atLimit ? 'limit' : 'done',
      percent: Math.round(estimate.overlap * 100),
      score: estimate.score,
    };
    return estimate.overlap;
  }

  /** The candidate as the calibration to store (null without one); the flow goes idle. */
  apply(): PanoramaCalibration | null {
    const result = this.candidate;
    const pair = this.pair;
    if (!result || !pair) return null;
    this.reset();
    return calibrationOf(result, pair.at);
  }

  /** Forget the candidate (or the error, or the "Auto" outcome); a running method is aborted. */
  discard(): void {
    this.#abort?.abort();
    this.#run += 1;
    this.reset();
  }

  private reset(): void {
    this.candidate = null;
    this.pair = null;
    this.failure = null;
    this.detail = null;
    this.auto = null;
    this.phase = 'idle';
  }
}
