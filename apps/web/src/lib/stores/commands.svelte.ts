/**
 * Commands on their way to a mower. `send` posts the command (202 Accepted) and marks it pending;
 * the socket's `command_result` resolves it. Without a result within 8 s (or with the socket
 * down) the store polls the command's `status_url` every 2 s until it is final or expired.
 */
import { SvelteMap } from 'svelte/reactivity';

export type CommandKind =
  | 'start_mow'
  | 'pause_mow'
  | 'resume_mow'
  | 'return_to_dock'
  | 'emergency_stop'
  | 'set_blade_height'
  | 'set_speed'
  | 'set_light'
  | 'firmware_update'
  | 'spot_mow'
  | 'spot_stop';

export type CommandOutcome =
  'pending' | 'accepted' | 'executed' | 'rejected' | 'failed' | 'expired';

export const FINAL_OUTCOMES: ReadonlySet<CommandOutcome> = new Set([
  'executed',
  'rejected',
  'failed',
  'expired',
]);

export interface TrackedCommand {
  id: string;
  mowerId: string;
  kind: CommandKind;
  status: CommandOutcome;
  reason: string | null;
  expiresAt: number;
}

export interface CommandApi {
  /** POST the command; returns the 202 body. */
  post(
    mowerId: string,
    command: { kind: CommandKind } & Record<string, unknown>,
  ): Promise<{ command_id: string; expires_at: string }>;
  /** GET the command record. */
  status(commandId: string): Promise<{ state: string; reason?: string | null }>;
}

export const RESULT_WAIT_MS = 8_000;
export const POLL_INTERVAL_MS = 2_000;

export class CommandStore {
  #items = new SvelteMap<string, TrackedCommand>();
  #api: CommandApi;
  #now: () => number;
  #listeners = new Set<(command: TrackedCommand) => void>();
  #early = new Map<string, { status: CommandOutcome; reason: string | null }>();

  constructor(api: CommandApi, now: () => number = () => Date.now()) {
    this.#api = api;
    this.#now = now;
  }

  /** Called when a command reaches a final outcome (toasts). */
  onFinal(listener: (command: TrackedCommand) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  get(id: string): TrackedCommand | undefined {
    return this.#items.get(id);
  }

  /** True while a command of `kind` for the mower is not final (double tap guard). */
  pending(mowerId: string, kind: CommandKind): boolean {
    for (const item of this.#items.values()) {
      if (item.mowerId === mowerId && item.kind === kind && !FINAL_OUTCOMES.has(item.status)) {
        return true;
      }
    }
    return false;
  }

  /** The newest command of `kind` for the mower, if any. */
  latest(mowerId: string, kind: CommandKind): TrackedCommand | undefined {
    let found: TrackedCommand | undefined;
    for (const item of this.#items.values()) {
      if (item.mowerId === mowerId && item.kind === kind) found = item;
    }
    return found;
  }

  async send(
    mowerId: string,
    command: { kind: CommandKind } & Record<string, unknown>,
  ): Promise<TrackedCommand> {
    const accepted = await this.#api.post(mowerId, command);
    const tracked: TrackedCommand = {
      id: accepted.command_id,
      mowerId,
      kind: command.kind,
      status: 'pending',
      reason: null,
      expiresAt: Date.parse(accepted.expires_at),
    };
    this.#items.set(tracked.id, tracked);
    const early = this.#early.get(tracked.id);
    if (early) {
      this.#early.delete(tracked.id);
      this.resolve(tracked.id, early.status, early.reason);
    } else {
      setTimeout(() => void this.#poll(tracked.id), RESULT_WAIT_MS);
    }
    return this.#items.get(tracked.id) ?? tracked;
  }

  /** A result from the socket (or a poll). Results may arrive before the 202 response. */
  resolve(commandId: string, status: string, reason: string | null = null): void {
    const outcome = normalise(status);
    const current = this.#items.get(commandId);
    if (!current) {
      this.#early.set(commandId, { status: outcome, reason });
      return;
    }
    if (FINAL_OUTCOMES.has(current.status)) return;
    const next = { ...current, status: outcome, reason };
    this.#items.set(commandId, next);
    if (FINAL_OUTCOMES.has(outcome)) for (const listener of this.#listeners) listener(next);
  }

  async #poll(commandId: string): Promise<void> {
    const current = this.#items.get(commandId);
    if (!current || FINAL_OUTCOMES.has(current.status)) return;
    if (this.#now() > current.expiresAt + POLL_INTERVAL_MS) {
      this.resolve(commandId, 'expired');
      return;
    }
    try {
      const record = await this.#api.status(commandId);
      this.resolve(commandId, record.state, record.reason ?? null);
    } catch {
      // try again on the next tick
    }
    const after = this.#items.get(commandId);
    if (after && !FINAL_OUTCOMES.has(after.status)) {
      setTimeout(() => void this.#poll(commandId), POLL_INTERVAL_MS);
    }
  }

  clear(): void {
    this.#items.clear();
    this.#early.clear();
  }
}

function normalise(status: string): CommandOutcome {
  switch (status) {
    case 'accepted':
    case 'executed':
    case 'rejected':
    case 'failed':
    case 'expired':
      return status;
    default:
      return 'pending';
  }
}
