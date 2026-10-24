/**
 * The AI assist sheet's state for one mower: which provider a request would use, the proposal
 * on screen (an open one of the mower, one opened by link, or a new request) polled about once a
 * second until it is final, and the decisions (approve, reject, retry). One instance per mower
 * page; `close()` stops the polling.
 */
import { asProblem, type ApiProblem } from '#lib/api/problem.js';

import {
  VISION_PROBLEMS,
  visionCalls,
  type ApprovalOption,
  type Proposal,
  type ProviderSettings,
  type VisionApi,
  type VisionImage,
  type VisionTask,
} from './api';

export const POLL_MS = 1_000;

/** What the sheet shows. */
export type AssistView =
  /** Looking for an open proposal of the mower. */
  | 'loading'
  /** Nothing open: a request can start. */
  | 'ready'
  /** The model looks at the picture. */
  | 'analysing'
  /** A maneuver waits for approval. */
  | 'proposed'
  /** The mower drives the steps (or the service checks the result). */
  | 'executing'
  /** The proposal is final (success, failure, rejection, expiry). */
  | 'outcome';

export type AssistAction = 'start' | 'approve' | 'reject' | 'retry';

export interface AssistTimers {
  setTimeout(handler: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

const browserTimers: AssistTimers = {
  setTimeout: (handler, ms) => globalThis.setTimeout(handler, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** The view of a proposal (null: none on screen). */
export function viewOf(proposal: Proposal | null): Exclude<AssistView, 'loading'> {
  if (!proposal) return 'ready';
  if (proposal.final) return 'outcome';
  switch (proposal.state) {
    case 'analysing':
      return 'analysing';
    case 'proposed':
      return 'proposed';
    case 'executing':
    case 'verifying':
      return 'executing';
    default:
      return 'outcome';
  }
}

export class AssistStore {
  mowerId = $state<string | null>(null);
  proposal = $state<Proposal | null>(null);
  provider = $state<ProviderSettings | null>(null);
  /** The provider settings could not be loaded (the sheet still works). */
  providerProblem = $state<ApiProblem | null>(null);
  /** The user's preference: send both front camera frames when both are live (default on). */
  stereo = $state(true);
  /** The last request or decision failed. */
  problem = $state<ApiProblem | null>(null);
  loading = $state(false);
  busy = $state<AssistAction | null>(null);

  view = $derived.by((): AssistView => (this.loading ? 'loading' : viewOf(this.proposal)));
  /** No provider would answer: link to the AI provider settings. */
  providerMissing = $derived(
    this.problem?.slug === VISION_PROBLEMS.providerMissing || this.provider?.effective === 'none',
  );
  canRetry = $derived(
    !!this.proposal?.final && this.proposal.can_retry && this.proposal.rounds_left > 0,
  );

  #api: VisionApi;
  #timers: AssistTimers;
  #timer: unknown = null;
  /** Bumped by open/close so answers for an earlier mower or proposal are dropped. */
  #generation = 0;

  constructor(api: VisionApi = visionCalls(), timers: AssistTimers = browserTimers) {
    this.#api = api;
    this.#timers = timers;
  }

  /**
   * Show `proposalId` (a deep link), else the mower's open proposal, else nothing (ready). Also
   * loads the provider the request would use.
   */
  async open(mowerId: string, proposalId: string | null = null): Promise<void> {
    this.#stopPolling();
    const generation = ++this.#generation;
    this.mowerId = mowerId;
    this.proposal = null;
    this.problem = null;
    this.loading = true;
    void this.loadProvider();
    void this.loadPreferences();
    try {
      let found: Proposal | null = null;
      if (proposalId) {
        found = await this.#api.proposal(proposalId);
        if (found.mower_id !== mowerId) found = null;
      }
      found ??= await this.#openProposal(mowerId);
      if (generation !== this.#generation) return;
      this.#show(found);
    } catch (error) {
      if (generation === this.#generation) this.problem = asProblem(error);
    } finally {
      if (generation === this.#generation) this.loading = false;
    }
  }

  async loadProvider(): Promise<void> {
    try {
      this.provider = await this.#api.provider();
      this.providerProblem = null;
    } catch (error) {
      this.provider = null;
      this.providerProblem = asProblem(error);
    }
  }

  /** The stereo preference (best effort: the default stays on when it cannot be read). */
  async loadPreferences(): Promise<void> {
    try {
      this.stereo = (await this.#api.preferences()).stereo_frames;
    } catch {
      // keep the default
    }
  }

  /**
   * Leave a finished proposal for a new request (the sheet shows "ready" again), e.g. after an
   * automatic analysis that got no camera photo.
   */
  fresh(): void {
    if (this.busy || (this.proposal && !this.proposal.final)) return;
    this.#stopPolling();
    this.#generation += 1;
    this.proposal = null;
    this.problem = null;
  }

  /** Ask for a maneuver; on a conflict (a proposal is open) show that one instead. */
  async start(task: VisionTask, images: VisionImage[] = []): Promise<void> {
    const mowerId = this.mowerId;
    if (!mowerId || this.busy) return;
    const generation = this.#generation;
    this.busy = 'start';
    this.problem = null;
    try {
      const created = await this.#api.recover(mowerId, task, images);
      if (generation === this.#generation) this.#show(created);
    } catch (error) {
      const problem = asProblem(error);
      if (generation !== this.#generation) return;
      if (problem.status === 409 && problem.slug !== VISION_PROBLEMS.providerMissing) {
        const open = await this.#openProposal(mowerId).catch(() => null);
        if (open && generation === this.#generation) {
          this.#show(open);
          return;
        }
      }
      this.problem = problem;
    } finally {
      this.busy = null;
    }
  }

  /** Approve the proposal; `option` picks one when it offers two (default: its `action`). */
  approve(option?: ApprovalOption): Promise<void> {
    return this.#decide('approve', (id) =>
      option ? this.#api.approve(id, option) : this.#api.approve(id),
    );
  }

  /** Reject a proposal, or cancel the running maneuver. */
  reject(): Promise<void> {
    return this.#decide('reject', (id) => this.#api.reject(id));
  }

  /** Another round with fresh snapshots (allowed while `canRetry`). */
  retry(images: VisionImage[] = []): Promise<void> {
    return this.#decide('retry', (id) => this.#api.retry(id, images));
  }

  /** Stop polling and forget the proposal (the sheet closed). */
  close(): void {
    this.#generation += 1;
    this.#stopPolling();
    this.proposal = null;
    this.problem = null;
    this.busy = null;
    this.loading = false;
  }

  async #decide(action: AssistAction, call: (id: string) => Promise<Proposal>): Promise<void> {
    const current = this.proposal;
    if (!current || this.busy) return;
    const generation = this.#generation;
    this.busy = action;
    this.problem = null;
    try {
      const next = await call(current.id);
      if (generation === this.#generation) this.#show(next);
    } catch (error) {
      if (generation !== this.#generation) return;
      this.problem = asProblem(error);
      // The proposal may have moved on (expired, decided elsewhere): show its current state.
      await this.#refresh(current.id, generation);
    } finally {
      this.busy = null;
    }
  }

  async #openProposal(mowerId: string): Promise<Proposal | null> {
    const [newest] = await this.#api.proposals(mowerId, 1);
    return newest && !newest.final ? newest : null;
  }

  #show(proposal: Proposal | null): void {
    this.#stopPolling();
    this.proposal = proposal;
    if (proposal && !proposal.final) this.#schedule(proposal.id, this.#generation);
  }

  #schedule(proposalId: string, generation: number): void {
    this.#timer = this.#timers.setTimeout(() => {
      this.#timer = null;
      void this.#refresh(proposalId, generation);
    }, POLL_MS);
  }

  async #refresh(proposalId: string, generation: number): Promise<void> {
    try {
      const next = await this.#api.proposal(proposalId);
      if (generation !== this.#generation || this.proposal?.id !== proposalId) return;
      this.#show(next);
    } catch (error) {
      if (generation !== this.#generation) return;
      const problem = asProblem(error);
      // Gone or no longer visible: stop; anything else (network, 5xx): keep trying.
      if (problem.status === 404 || problem.status === 403) {
        this.problem = problem;
        return;
      }
      if (this.#timer === null && this.proposal && !this.proposal.final) {
        this.#schedule(proposalId, generation);
      }
    }
  }

  #stopPolling(): void {
    if (this.#timer !== null) this.#timers.clearTimeout(this.#timer);
    this.#timer = null;
  }
}
