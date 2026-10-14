/**
 * Latest state per mower, from REST and the socket. The newest `observed_at` wins (REST and the
 * socket can race), so a store entry never goes back in time.
 */
import { SvelteMap } from 'svelte/reactivity';

import type { components } from '$lib/generated/telemetry';

export type MowerState = components['schemas']['MowerState'];

export interface MowerSnapshot {
  state: MowerState;
  receivedAt: number;
  source: 'rest' | 'live';
}

/**
 * A state older than this counts as stale. `observed_at` is when the mower last reported: a
 * cloud mower is asked for a report every 20 s (adapter option `refresh_seconds`) and the
 * gateway repeats the newest state every 10 s, so a healthy cloud mower's age peaks around
 * 30 s; 45 s leaves room for a slow answer without flagging it.
 */
export const STALE_AFTER_MS = 45_000;

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
    if (current && Date.parse(current.state.observed_at) > Date.parse(state.observed_at)) {
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

  /** True when the state of `id` is older than {@link STALE_AFTER_MS} at `now`. */
  isStale(id: string, now: number = this.#now()): boolean {
    const item = this.#items.get(id);
    return item ? now - Date.parse(item.state.observed_at) > STALE_AFTER_MS : false;
  }
}

export const mowers = new MowerStore();
