/**
 * Latest state per mower, from REST and the socket. The newest `observed_at` wins, then the
 * newest link heartbeat (REST and the socket can race), so a store entry never goes back in time.
 * Online and offline follow the gateway's link heartbeat, not the age of the values (ADR 0040,
 * `#lib/mower/presence.js`).
 */
import { SvelteMap } from 'svelte/reactivity';

import type { components } from '#lib/generated/telemetry.js';
import { isNewer, isOnline, oldValuesAge } from '#lib/mower/presence.js';

export { OFFLINE_AFTER_MS, VALUES_OLD_AFTER_MS } from '#lib/mower/presence.js';

export type MowerState = components['schemas']['MowerState'];

export interface MowerSnapshot {
  state: MowerState;
  receivedAt: number;
  source: 'rest' | 'live';
}

export class MowerStore {
  #items = new SvelteMap<string, MowerSnapshot>();
  #now: () => number;

  constructor(now: () => number = () => Date.now()) {
    this.#now = now;
  }

  get(id: string): MowerSnapshot | undefined {
    return this.#items.get(id);
  }

  has(id: string): boolean {
    return this.#items.has(id);
  }

  /** Store `state` unless a newer one is already known; returns whether it was taken. */
  upsert(id: string, state: MowerState, source: MowerSnapshot['source']): boolean {
    const current = this.#items.get(id);
    if (current && !isNewer(current.state, state)) {
      return false;
    }
    this.#items.set(id, { state, receivedAt: this.#now(), source });
    return true;
  }

  remove(id: string): void {
    this.#items.delete(id);
  }

  clear(): void {
    this.#items.clear();
  }

  /**
   * True when the gateway holds no live connection to `id` at `now`: the link was reported lost,
   * or the last heartbeat is older than `OFFLINE_AFTER_MS`. False for an unknown mower (no
   * state is not an offline state).
   */
  isOffline(id: string, now: number = this.#now()): boolean {
    const item = this.#items.get(id);
    return item ? !isOnline(item.state, now) : false;
  }

  /**
   * The age of the values of an online mower in milliseconds once they are older than
   * `VALUES_OLD_AFTER_MS` (a docked mower reports rarely); null otherwise.
   */
  oldValuesAge(id: string, now: number = this.#now()): number | null {
    const item = this.#items.get(id);
    return item ? oldValuesAge(item.state, now) : null;
  }
}

export const mowers = new MowerStore();
