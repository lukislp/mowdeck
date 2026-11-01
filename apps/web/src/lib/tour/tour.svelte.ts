/**
 * The running guided tour (ADR 0073): which step is shown, the element it points at, and the
 * stored outcome. Everything outside (navigation, the DOM, the services, storage) comes in
 * through `TourDeps`, so the state machine runs in unit tests as it does in the app.
 *
 * A step whose target does not appear within `targetTimeoutMs` is passed over in the direction
 * of travel and never shown again in this run: a missing element never blocks the tour.
 */
import {
  firstFrom,
  moveFrom,
  progressOf,
  shouldAutoStart,
  tourSteps,
  type TourContext,
  type TourOutcome,
  type TourStep,
  type TourStepId,
} from './steps';

export interface TourStore {
  /** The stored outcome; null when it cannot be read. */
  load(): Promise<TourOutcome | null>;
  /** Store a new outcome. Never rejects (a failed save keeps the tour usable). */
  save(outcome: Exclude<TourOutcome, 'new'>, context: TourContext): Promise<void>;
}

/** Where a tour that runs in this tab stands, so a reload continues it. */
export interface TourResume {
  read(): TourStepId | null;
  write(step: TourStepId | null): void;
}

export interface TourDeps {
  store: TourStore;
  /** Read the context (mowers, capabilities, modules, demo). Never rejects. */
  context(): Promise<TourContext>;
  /** Path (without query and hash) of the page shown now. */
  currentPath(): string;
  navigate(path: string): Promise<void>;
  /** The visible element with `data-tour="<target>"`, waiting up to `timeoutMs`; null if none. */
  findTarget(target: string, timeoutMs: number): Promise<HTMLElement | null>;
  resume: TourResume;
  /** This device never starts the tour by itself (`mowdeck.tour.autostart` = `off`). */
  optedOut(): boolean;
  /** Status of the setup tour (ADR 0062), for "is this a new user". Null when unknown. */
  setupStatus(): Promise<'new' | 'active' | 'dismissed' | 'completed' | null>;
  targetTimeoutMs?: number;
}

const TARGET_TIMEOUT_MS = 6_000;

function pathOf(route: string): string {
  return route.split(/[?#]/, 1)[0] ?? route;
}

export class Tour {
  running = $state(false);
  steps = $state.raw<TourStep[]>([]);
  index = $state(0);
  missed = $state.raw<ReadonlySet<string>>(new Set());
  /** The element the current step points at; null while it is looked for or for a centred step. */
  target = $state.raw<HTMLElement | null>(null);
  /** The current step's page and target are being brought up. */
  seeking = $state(false);
  /** The stored outcome (null: unknown or not loaded yet). */
  outcome = $state<TourOutcome | null>(null);
  context = $state.raw<TourContext | null>(null);

  readonly #deps: TourDeps;
  /** Bumped by every show and by the end: an older show that resolves late does nothing. */
  #seq = 0;
  #autoChecked = false;
  #resumeChecked = false;

  constructor(deps: TourDeps) {
    this.#deps = deps;
  }

  get step(): TourStep | null {
    return this.running ? (this.steps[this.index] ?? null) : null;
  }

  get progress(): { current: number; total: number } {
    return progressOf(this.steps, this.missed, this.index);
  }

  /** The current step is the last one still shown. */
  get last(): boolean {
    return moveFrom(this.steps, this.missed, this.index, 1) === 'end';
  }

  get first(): boolean {
    return moveFrom(this.steps, this.missed, this.index, -1) === this.index;
  }

  /** Read the stored outcome (Settings shows it). */
  async loadOutcome(): Promise<TourOutcome | null> {
    this.outcome = await this.#deps.store.load();
    return this.outcome;
  }

  /**
   * Start the tour (from its first step, or `at`). `context` skips reading it again (the
   * automatic start has it already).
   */
  async start(options: { at?: TourStepId; context?: TourContext } = {}): Promise<void> {
    const seq = ++this.#seq;
    const context = options.context ?? (await this.#deps.context());
    if (seq !== this.#seq) return;
    this.context = context;
    this.steps = tourSteps(context);
    this.missed = new Set();
    this.running = true;
    if (this.outcome === null) await this.loadOutcome();
    if (this.outcome === 'new') {
      this.outcome = 'seen';
      void this.#deps.store.save('seen', context);
    }
    const at = options.at ? this.steps.findIndex((step) => step.id === options.at) : 0;
    await this.#show(firstFrom(this.steps, this.missed, Math.max(at, 0)), 1);
  }

  next(): Promise<void> {
    if (!this.running) return Promise.resolve();
    return this.#show(moveFrom(this.steps, this.missed, this.index, 1), 1);
  }

  back(): Promise<void> {
    if (!this.running || this.first) return Promise.resolve();
    return this.#show(moveFrom(this.steps, this.missed, this.index, -1), -1);
  }

  /** Leave the tour before its end (Esc, "Skip"). */
  skip(): void {
    this.#end('skipped');
  }

  /** The last step's "Done". */
  finish(): void {
    this.#end('done');
  }

  /**
   * On the first visit of the mower list after the sign-in: start the tour if this user (or, in
   * the demo, this browser) has never seen it. Checked once per session.
   */
  async maybeAutoStart(path: string, demo: boolean): Promise<boolean> {
    if (this.running || this.#autoChecked || path !== '/mowers') return false;
    this.#autoChecked = true;
    if (this.#deps.optedOut()) return false;
    const outcome = await this.loadOutcome();
    if (outcome !== 'new') return false;
    const [setup, context] = await Promise.all([
      demo ? Promise.resolve(null) : this.#deps.setupStatus(),
      this.#deps.context(),
    ]);
    const start = shouldAutoStart({
      path: this.#deps.currentPath(),
      outcome: this.outcome,
      demo,
      optedOut: false,
      setup,
      mowers: context.mowers,
    });
    if (start && !this.running) await this.start({ context });
    return start;
  }

  /** A reload in the middle of a tour: continue at the step it showed. */
  async resume(): Promise<boolean> {
    if (this.#resumeChecked || this.running) return false;
    this.#resumeChecked = true;
    const at = this.#deps.resume.read();
    if (!at) return false;
    // A resumed tour counts as the automatic start of this session.
    this.#autoChecked = true;
    await this.start({ at });
    return true;
  }

  /** The current target went away (the page re-rendered or the user navigated): look again. */
  retarget(): Promise<void> {
    if (!this.running || this.seeking) return Promise.resolve();
    return this.#show(this.index, 1);
  }

  /** Signed out: forget the session's state (the stored outcome stays on the server). */
  reset(): void {
    this.#seq++;
    this.running = false;
    this.steps = [];
    this.index = 0;
    this.missed = new Set();
    this.target = null;
    this.seeking = false;
    this.outcome = null;
    this.context = null;
    this.#autoChecked = false;
    this.#resumeChecked = false;
  }

  async #show(index: number | 'end', direction: 1 | -1): Promise<void> {
    if (index === 'end') {
      this.#end('done');
      return;
    }
    const seq = ++this.#seq;
    const step = this.steps[index]!;
    this.index = index;
    this.target = null;
    this.seeking = true;
    this.#deps.resume.write(step.id);
    if (step.route && pathOf(step.route) !== this.#deps.currentPath()) {
      try {
        await this.#deps.navigate(step.route);
      } catch {
        // A refused navigation leaves the user where they are; the target decides below.
      }
      if (seq !== this.#seq) return;
    }
    if (!step.target) {
      this.seeking = false;
      return;
    }
    const element = await this.#deps.findTarget(
      step.target,
      this.#deps.targetTimeoutMs ?? TARGET_TIMEOUT_MS,
    );
    if (seq !== this.#seq) return;
    if (element) {
      this.target = element;
      this.seeking = false;
      return;
    }
    // Missing: never block, pass over it in the direction of travel (or forward, when there
    // is nothing left behind).
    this.missed = new Set([...this.missed, step.id]);
    let following = moveFrom(this.steps, this.missed, index, direction);
    if (following === index) following = moveFrom(this.steps, this.missed, index, 1);
    await this.#show(following, direction);
  }

  #end(outcome: 'done' | 'skipped'): void {
    if (!this.running) return;
    this.#seq++;
    const context = this.context;
    this.running = false;
    this.target = null;
    this.seeking = false;
    this.#deps.resume.write(null);
    // Finishing a tour again never turns a "done" into "skipped".
    if (outcome === 'skipped' && this.outcome === 'done') return;
    this.outcome = outcome;
    if (context) void this.#deps.store.save(outcome, context);
  }
}
