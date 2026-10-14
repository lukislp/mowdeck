/**
 * The plan that applies to the signed-in user's account now (`GET /entitlements/me`), loaded once
 * per session and again on demand (the account page). Null while unknown or when Entitlements is
 * not deployed: then nothing about plans is shown, and the services decide as always.
 */
import { entitlementsApi, unwrap } from '$lib/api/client';

import type { ResolvedPlan } from './types';
import { isRestricted } from './usage';

export type PlanLoader = () => Promise<ResolvedPlan>;

const defaultLoader: PlanLoader = () => unwrap(entitlementsApi.GET('/api/v1/entitlements/me'));

export class AccountPlan {
  current = $state<ResolvedPlan | null>(null);
  /** The last load failed (Entitlements missing or unreachable). */
  failed = $state(false);
  #pending: Promise<ResolvedPlan | null> | null = null;
  #loader: PlanLoader;

  constructor(loader: PlanLoader = defaultLoader) {
    this.#loader = loader;
  }

  /** Grace or expired: only viewing and the emergency stop remain. */
  get restricted(): boolean {
    return isRestricted(this.current?.status);
  }

  load(force = false): Promise<ResolvedPlan | null> {
    if (force) this.#pending = null;
    this.#pending ??= this.#loader().then(
      (plan) => {
        this.current = plan;
        this.failed = false;
        return plan;
      },
      () => {
        this.failed = true;
        return this.current;
      },
    );
    return this.#pending;
  }

  reset(): void {
    this.#pending = null;
    this.current = null;
    this.failed = false;
  }
}

export const accountPlan = new AccountPlan();
