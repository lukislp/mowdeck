/**
 * State of the setup tour (ADR 0062): the user's choices from Identity (`/me/onboarding`, so
 * they follow the user from the browser to the iPhone app) and the facts read from the real
 * state of the services. Writes are optimistic: the view changes at once, a failed save keeps
 * the change for this page load and reports it through `onSaveFailed`.
 */
import {
  NO_FACTS,
  UNTOUCHED,
  hintVisible,
  isComplete,
  nextOpen,
  offer,
  stepViews,
  type Context,
  type Facts,
  type HintKey,
  type Progress,
  type StepKey,
  type StepView,
  type TourStatus,
} from './steps';

export interface ProgressApi {
  load(): Promise<Progress>;
  save(progress: Progress): Promise<Progress>;
}

export interface OnboardingDeps {
  api: ProgressApi;
  /** Read every fact; each one that cannot be read is null. Never rejects. */
  facts(): Promise<Facts>;
  /** What the user may do and the installation and device offer. Never rejects. */
  context(): Promise<Context>;
  onSaveFailed?: (error: unknown) => void;
}

export const NO_CONTEXT: Context = {
  canManage: false,
  cloud: false,
  schedules: false,
  notifications: false,
  passkeys: false,
  signInLinks: false,
  appExtras: false,
};

export class Onboarding {
  progress = $state.raw<Progress>(UNTOUCHED);
  /** The choices are loaded (or failed to load: then the tour offers nothing). */
  loaded = $state(false);
  failed = $state(false);
  facts = $state.raw<Facts>(NO_FACTS);
  context = $state.raw<Context>(NO_CONTEXT);
  factsLoaded = $state(false);

  readonly #deps: OnboardingDeps;
  #loading: Promise<void> | null = null;
  #refreshing: Promise<void> | null = null;

  constructor(deps: OnboardingDeps) {
    this.#deps = deps;
  }

  get status(): TourStatus {
    return this.progress.status;
  }

  get steps(): StepView[] {
    return stepViews(this.facts, this.context, this.progress.skipped);
  }

  get next(): StepView | null {
    return nextOpen(this.steps);
  }

  get complete(): boolean {
    return this.factsLoaded && isComplete(this.steps);
  }

  /** What the mower list shows (`none` until the choices and the facts are known). */
  get offer(): 'tour' | 'hint' | 'none' {
    if (!this.loaded || this.failed) return 'none';
    if (this.progress.status !== 'active' && !this.factsLoaded) return 'none';
    return offer(this.progress.status, this.facts, this.context);
  }

  hintVisible(key: HintKey): boolean {
    return this.loaded && !this.failed && hintVisible(this.progress, key);
  }

  /** Load the choices once per session. */
  load(): Promise<void> {
    this.#loading ??= this.#deps.api
      .load()
      .then((progress) => {
        this.progress = progress;
        this.failed = false;
      })
      .catch(() => {
        // Identity unreachable: offer nothing (never push a tour on a set-up account).
        this.failed = true;
        this.#loading = null;
      })
      .finally(() => (this.loaded = true));
    return this.#loading;
  }

  /** Re-read the facts and the context (a page with the tour opened or came back into view). */
  refresh(): Promise<void> {
    this.#refreshing ??= Promise.all([this.#deps.facts(), this.#deps.context()])
      .then(([facts, context]) => {
        this.facts = facts;
        this.context = context;
        this.factsLoaded = true;
      })
      .finally(() => (this.#refreshing = null));
    return this.#refreshing;
  }

  /** Start (or resume) the tour. */
  start(): Promise<void> {
    return this.#save({ ...this.progress, status: 'active' });
  }

  skip(key: StepKey): Promise<void> {
    const skipped = this.progress.skipped.includes(key)
      ? this.progress.skipped
      : [...this.progress.skipped, key];
    return this.#save({ ...this.progress, status: 'active', skipped });
  }

  /** Take a skipped step back into the tour. */
  unskip(key: StepKey): Promise<void> {
    const skipped = this.progress.skipped.filter((value) => value !== key);
    return this.#save({ ...this.progress, status: 'active', skipped });
  }

  /** Close the tour without finishing; it opens again only on request. */
  dismiss(): Promise<void> {
    return this.#save({ ...this.progress, status: 'dismissed' });
  }

  finish(): Promise<void> {
    return this.#save({ ...this.progress, status: 'completed' });
  }

  dismissHint(key: HintKey): Promise<void> {
    if (this.progress.hints.includes(key)) return Promise.resolve();
    return this.#save({ ...this.progress, hints: [...this.progress.hints, key] });
  }

  /**
   * Replace the stored hint keys (the guided tour keeps its outcome there, ADR 0073). A setup
   * tour that never started (`new`) is stored as `whenNew`: what the mower list would make of
   * the account (`active` for a new account that may add mowers and has none). Nothing is saved
   * while the choices are not loaded, so a failed load never overwrites them.
   */
  setHints(hints: readonly string[], whenNew: 'active' | 'dismissed'): Promise<void> {
    if (!this.loaded || this.failed) return Promise.resolve();
    const status = this.progress.status === 'new' ? whenNew : this.progress.status;
    return this.#save({ ...this.progress, status, hints: [...hints] });
  }

  /** Signed out: forget everything of the user. */
  reset(): void {
    this.progress = UNTOUCHED;
    this.loaded = false;
    this.failed = false;
    this.facts = NO_FACTS;
    this.context = NO_CONTEXT;
    this.factsLoaded = false;
    this.#loading = null;
    this.#refreshing = null;
  }

  async #save(next: Progress): Promise<void> {
    // `new` is only the absence of a stored row; a hint dismissed before the tour ever started
    // is stored with the tour dismissed (an account set up before the tour existed).
    const progress: Progress = next.status === 'new' ? { ...next, status: 'dismissed' } : next;
    this.progress = progress;
    try {
      const saved = await this.#deps.api.save(progress);
      // A newer change may have been made meanwhile; keep the newest local state.
      if (this.progress === progress) this.progress = saved;
    } catch (error) {
      this.#deps.onSaveFailed?.(error);
    }
  }
}
