/**
 * Changing the mower's own settings with an optimistic UI: a change shows at once, goes out as
 * `set_setting`, and its command result (over the socket, or the command store's polling) decides
 * what stays. Executed and reported back: the mower's value shows with "saved". Rejected, failed
 * or expired: the mower's value shows again with the reason. Executed but never reported within
 * `CONFIRM_MS`: the mower's value shows with "not confirmed". Everything is derived from the
 * attempt per key, the command store and the reported state, so nothing has to be reconciled.
 */
import { SvelteMap } from 'svelte/reactivity';

import { sameValue, type SettingSpec, type SettingValue } from './device-settings';

/** How long "saved" stays next to a row after the change went out. */
export const SAVED_SHOW_MS = 10_000;
/** How long an executed change may wait for the mower to report the new value. */
export const CONFIRM_MS = 30_000;

export type RowState = 'idle' | 'sending' | 'saved' | 'failed' | 'unconfirmed';

export interface RowView {
  /** The value to show: the change on its way, else the reported one (undefined: not reported). */
  value: SettingValue | undefined;
  state: RowState;
  /** The mower's or the server's reason of a failed change. */
  reason: string | null;
}

export interface SettingsCommandApi {
  /** Send `set_setting`; resolves with the command id, rejects with the problem. */
  send(command: { kind: 'set_setting'; key: string; value: SettingValue }): Promise<string>;
  /** The command's outcome so far (undefined while unknown). */
  outcome(commandId: string): { status: string; reason: string | null } | undefined;
  /** Words of a refused request (the problem of a failed POST). */
  problemText(error: unknown): string;
}

interface Attempt {
  value: SettingValue;
  commandId: string | null;
  /** The POST itself failed (no command): its reason. */
  refused: string | null;
  at: number;
}

const FAILED = new Set(['rejected', 'failed', 'expired']);

export class DeviceSettingsFlow {
  #attempts = new SvelteMap<string, Attempt>();
  #api: SettingsCommandApi;
  #now: () => number;

  constructor(api: SettingsCommandApi, now: () => number = () => Date.now()) {
    this.#api = api;
    this.#now = now;
  }

  /** Send a change of `spec` to `value` (a value equal to the shown one is not sent). */
  async change(
    spec: SettingSpec,
    value: SettingValue,
    shown: SettingValue | undefined,
  ): Promise<void> {
    if (sameValue(value, shown)) return;
    const attempt: Attempt = { value, commandId: null, refused: null, at: this.#now() };
    this.#attempts.set(spec.key, attempt);
    try {
      const commandId = await this.#api.send({ kind: 'set_setting', key: spec.key, value });
      // A newer change of the same key wins over this answer.
      if (this.#attempts.get(spec.key) === attempt)
        this.#attempts.set(spec.key, { ...attempt, commandId });
    } catch (error) {
      if (this.#attempts.get(spec.key) === attempt)
        this.#attempts.set(spec.key, { ...attempt, refused: this.#api.problemText(error) });
    }
  }

  /** True while a change of `key` is on its way (its control waits). */
  busy(key: string, reported: SettingValue | undefined, now: number): boolean {
    return this.view(key, reported, now).state === 'sending';
  }

  view(key: string, reported: SettingValue | undefined, now: number): RowView {
    const attempt = this.#attempts.get(key);
    if (!attempt) return { value: reported, state: 'idle', reason: null };
    if (attempt.refused !== null)
      return { value: reported, state: 'failed', reason: attempt.refused };
    if (attempt.commandId === null) return { value: attempt.value, state: 'sending', reason: null };
    const outcome = this.#api.outcome(attempt.commandId);
    if (outcome && FAILED.has(outcome.status)) {
      return { value: reported, state: 'failed', reason: outcome.reason };
    }
    if (outcome?.status === 'executed' && sameValue(reported, attempt.value)) {
      const state = now - attempt.at < SAVED_SHOW_MS ? 'saved' : 'idle';
      return { value: reported, state, reason: null };
    }
    if (outcome?.status === 'executed' && now - attempt.at > CONFIRM_MS) {
      return { value: reported, state: 'unconfirmed', reason: null };
    }
    return { value: attempt.value, state: 'sending', reason: null };
  }

  reset(): void {
    this.#attempts.clear();
  }
}
