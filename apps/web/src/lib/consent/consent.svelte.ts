// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * Explicit consent before camera pictures or text go to a cloud AI provider (ADR 0081).
 *
 * The rules live in the Vision service: it classifies the provider in effect (cloud, local,
 * simulated), keeps the user's consent and refuses a request to a cloud model without a
 * covering one (403 `vision-consent-required`, the assistant passes it on as
 * `assistant-consent-required`). This module is the one client side piece: the three calls, the
 * state of the dialog (`ConsentSheet.svelte`, mounted once in the root layout) and
 * {@link ConsentState.guard}, which the features use around the request that sends data. They
 * hold no consent logic of their own. In the demo and with a local model the service says no
 * consent is required, so nothing is shown.
 */
import type { Client } from 'openapi-fetch';

import { unwrap, visionApi } from '#lib/api/client.js';
import { asProblem, type ApiProblem } from '#lib/api/problem.js';
import type { components, paths } from '#lib/generated/vision.js';

export type ConsentStatus = components['schemas']['ConsentOut'];

/** What the feature that asks is going to send, so the dialog names exactly that. */
export type ConsentScope =
  /** Camera pictures, a map sketch with the area names and the mower's status. */
  | 'assist'
  /** The typed text with the mower and area names. */
  | 'chat'
  /** Settings: the provider or the automatic analysis, which can send both. */
  | 'any';

/** Problem types (after the URN prefix) that mean "ask for consent". */
export const CONSENT_PROBLEMS: readonly string[] = [
  'vision-consent-required',
  'assistant-consent-required',
];

/** What {@link ConsentState.guard} returns when the user declined. */
export const DECLINED = Symbol('consent-declined');

export function isConsentProblem(error: unknown): boolean {
  return asProblem(error).status === 403 && CONSENT_PROBLEMS.includes(asProblem(error).slug);
}

export interface ConsentApi {
  status(): Promise<ConsentStatus>;
  grant(): Promise<ConsentStatus>;
  withdraw(): Promise<ConsentStatus>;
}

/** The calls on `client` (default: the app's authenticated Vision client). */
export function consentCalls(client: Client<paths> = visionApi): ConsentApi {
  return {
    status: () => unwrap(client.GET('/api/v1/vision/me/consent')),
    grant: () => unwrap(client.PUT('/api/v1/vision/me/consent')),
    withdraw: () => unwrap(client.DELETE('/api/v1/vision/me/consent')),
  };
}

/** Whether a request to the provider in effect may go out as far as this status says. */
export function consentGiven(status: ConsentStatus): boolean {
  return !status.required || status.covers;
}

export class ConsentState {
  /** The last status read (null: not loaded yet). */
  status = $state<ConsentStatus | null>(null);
  /** The dialog is showing. */
  open = $state(false);
  scope = $state<ConsentScope>('any');
  /** Granting or withdrawing is running. */
  busy = $state(false);
  /** The last failed grant or withdrawal, for the dialog. */
  problem = $state<ApiProblem | null>(null);

  #api: ConsentApi;
  #pending: Promise<boolean> | null = null;
  #settle: ((agreed: boolean) => void) | null = null;

  constructor(api: ConsentApi = consentCalls()) {
    this.#api = api;
  }

  /** Read the status again (null when it cannot be read: the service then decides at request
   * time, so an unreachable status never blocks a feature on its own). */
  async refresh(): Promise<ConsentStatus | null> {
    try {
      this.status = await this.#api.status();
    } catch {
      return null;
    }
    return this.status;
  }

  /**
   * True when the request may go out: no consent is required for the provider in effect, or the
   * user has agreed. Otherwise the dialog opens; the answer is true after "agree", false after
   * "not now". Concurrent callers share one dialog.
   */
  async ensure(scope: ConsentScope = 'any'): Promise<boolean> {
    if (this.#pending) return this.#pending;
    const status = await this.refresh();
    if (!status || consentGiven(status)) return true;
    // Another caller may have opened the dialog while the status loaded.
    if (this.#pending) return this.#pending;
    this.scope = scope;
    this.problem = null;
    this.open = true;
    this.#pending = new Promise<boolean>((resolve) => {
      this.#settle = resolve;
    });
    return this.#pending;
  }

  /**
   * Run `run` (the request that sends data) once consent is given. Asks first; when the service
   * still answers with the consent problem (the provider or model changed meanwhile), asks again
   * and repeats the request once. Returns {@link DECLINED} when the user said "not now": the
   * feature stays unused, no error.
   */
  async guard<T>(scope: ConsentScope, run: () => Promise<T>): Promise<T | typeof DECLINED> {
    if (!(await this.ensure(scope))) return DECLINED;
    try {
      return await run();
    } catch (error) {
      if (!isConsentProblem(error)) throw error;
      if (!(await this.ensure(scope))) return DECLINED;
      return run();
    }
  }

  /** The dialog's "agree": record the consent for the destination shown. */
  async agree(): Promise<void> {
    if (!this.#pending || this.busy) return;
    this.busy = true;
    this.problem = null;
    try {
      this.status = await this.#api.grant();
      this.#close(true);
    } catch (error) {
      this.problem = asProblem(error);
    } finally {
      this.busy = false;
    }
  }

  /** The dialog's "not now" (also Escape and the close button). */
  decline(): void {
    this.#close(false);
  }

  /** Withdraw from the settings; the service switches automatic analysis off with it. */
  async withdraw(): Promise<ConsentStatus> {
    this.status = await this.#api.withdraw();
    return this.status;
  }

  #close(agreed: boolean): void {
    this.open = false;
    const settle = this.#settle;
    this.#settle = null;
    this.#pending = null;
    settle?.(agreed);
  }
}

/** The one instance of the app. */
export const consent = new ConsentState();

/** The part of {@link ConsentState} the features use (so tests can hand in a stand-in). */
export type ConsentGuard = Pick<ConsentState, 'guard' | 'ensure'>;
