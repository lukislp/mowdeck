// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The safety acknowledgement as a question a feature asks: `await safety.ask('start_mow')` resolves
 * true when the user confirmed (or the action needs no confirmation, see `rule.ts`) and false when
 * they cancelled. One sheet (`SafetySheet.svelte`, mounted once in the app layout) shows it; the
 * state holds no "never ask again", every start asks anew. Two asks at the same time share the one
 * sheet and its answer.
 */
import {
  needsSafetyAcknowledgement,
  safetyVariant,
  type SafetyKind,
  type SafetyVariant,
} from './rule';

export class SafetyGate {
  /** The sheet is showing. */
  open = $state(false);
  /** The wording variant of the open sheet. */
  variant = $state<SafetyVariant>('now');

  #pending: Promise<boolean> | null = null;
  #settle: ((confirmed: boolean) => void) | null = null;

  /** Ask for `kind`; `covered`: the flow's own stricter confirmation already said it. */
  ask(kind: SafetyKind, covered = false): Promise<boolean> {
    if (!needsSafetyAcknowledgement(kind, covered)) return Promise.resolve(true);
    if (this.#pending) return this.#pending;
    this.variant = safetyVariant(kind);
    this.open = true;
    this.#pending = new Promise<boolean>((resolve) => {
      this.#settle = resolve;
    });
    return this.#pending;
  }

  /** The user ticked the box and pressed the confirm button. */
  confirm(): void {
    this.#finish(true);
  }

  /** Cancel, close or Escape. */
  cancel(): void {
    this.#finish(false);
  }

  #finish(confirmed: boolean): void {
    const settle = this.#settle;
    this.open = false;
    this.#pending = null;
    this.#settle = null;
    settle?.(confirmed);
  }
}

/** The app's one gate. */
export const safety = new SafetyGate();
