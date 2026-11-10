// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The safety acknowledgement as a question a feature asks: `await safety.ask('start_mow')` resolves
 * true when the user confirmed (or the action needs no confirmation, see `rule.ts`) and false when
 * they cancelled. One sheet (`SafetySheet.svelte`, mounted once in the app layout) shows it; the
 * state holds no "never ask again", every start asks anew.
 *
 * The sheet is non-modal, so another start can be asked while one is open (a schedule switch next
 * to an open start sheet). Questions of different kinds queue and each shows its own wording and
 * gets its own answer; only the same kind asked again (a double tap) shares the pending answer.
 * `cancelAll()` answers everything false, used when the user leaves the page, so a start cannot
 * fire later on another screen.
 */
import {
  needsSafetyAcknowledgement,
  safetyVariant,
  type SafetyKind,
  type SafetyVariant,
} from './rule';

interface Question {
  kind: SafetyKind;
  variant: SafetyVariant;
  answer: Promise<boolean>;
  settle: (confirmed: boolean) => void;
}

export class SafetyGate {
  /** The sheet is showing. */
  open = $state(false);
  /** The wording variant of the open sheet. */
  variant = $state<SafetyVariant>('now');
  /** Counts the questions shown, so the sheet starts each one unticked even back to back. */
  shown = $state(0);

  /** The first is on screen, the others wait their turn. */
  #queue: Question[] = [];
  #last: Question | null = null;

  /** Ask for `kind`; `covered`: the flow's own stricter confirmation already said it. */
  ask(kind: SafetyKind, covered = false): Promise<boolean> {
    if (!needsSafetyAcknowledgement(kind, covered)) return Promise.resolve(true);
    const same = this.#queue.find((question) => question.kind === kind);
    if (same) return same.answer;
    let settle!: (confirmed: boolean) => void;
    const answer = new Promise<boolean>((resolve) => {
      settle = resolve;
    });
    this.#queue.push({ kind, variant: safetyVariant(kind), answer, settle });
    this.#show();
    return answer;
  }

  /** The user ticked the box and pressed the confirm button. */
  confirm(): void {
    this.#finishFirst(true);
  }

  /** Cancel, close or Escape: the question on screen; the next one, if any, comes up. */
  cancel(): void {
    this.#finishFirst(false);
  }

  /** Answer every question false (the user left the page). */
  cancelAll(): void {
    const all = this.#queue;
    this.#queue = [];
    this.#show();
    for (const question of all) question.settle(false);
  }

  #finishFirst(confirmed: boolean): void {
    const first = this.#queue.shift();
    this.#show();
    first?.settle(confirmed);
  }

  #show(): void {
    const first = this.#queue[0];
    this.open = first !== undefined;
    if (first) {
      this.variant = first.variant;
      if (first !== this.#last) this.shown += 1;
    }
    this.#last = first ?? null;
  }
}

/** The app's one gate. */
export const safety = new SafetyGate();
