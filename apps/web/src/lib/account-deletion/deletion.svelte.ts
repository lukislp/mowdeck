// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * Deleting your account or yourself inside the app (ADR 0082, step 4).
 *
 * Identity decides, the app shows: `GET /me` says per membership what `DELETE /me` does
 * (`self_deletion`: `user` deletes the caller alone and at once, `account` schedules the
 * deletion of the whole account), why it would be refused now (`self_deletion_blocked`), how
 * many members block an owner (`other_members`), the grace period of a request
 * (`deletion_grace_days`) and, while the account is `deleting`, when it goes (`delete_after`).
 * {@link deletionState} turns that into the one state the danger area and the banner render;
 * it never guesses from `is_owner`.
 *
 * {@link AccountDeletion} runs the two actions:
 *
 * - `remove()` calls `DELETE /me` without a password: the API answers `401 reauth-required`
 *   unless the user confirmed recently, and the API client opens the step-up sheet and repeats
 *   the call (ADR 0057). The answer's `operation` says what happened. `user`: the person is gone,
 *   the server has dropped the refresh cookie, and the app leaves exactly like a sign-out
 *   (`signOut('deleted')`: the app shell's hand-over first, then the session, then the login
 *   page with a neutral notice). `account`: the owner stays signed in with every permission; `/me`
 *   is read again, so the banner appears on every page.
 * - `restore()` cancels a scheduled deletion (`POST /accounts/{id}/restore`) and reads `/me`
 *   again, so the banner goes.
 */
import { identityApi, unwrap } from '#lib/api/client.js';
import { asProblem, type ApiProblem } from '#lib/api/problem.js';
import { session, type Me, type Membership } from '#lib/auth/session.svelte.js';
import { signOut } from '#lib/auth/sign-out.js';
import { REAUTH_TYPE } from '#lib/auth/stepup.svelte.js';
import { probeBilling } from '#lib/billing/billing.js';
import type { components } from '#lib/generated/identity.js';

export type SelfDeletionOut = components['schemas']['SelfDeletionOut'];

/** Why the deletion is refused now; `unknown` for a code this app does not know yet. */
export type BlockReason =
  'other_members' | 'owns_another_account' | 'member_of_another_account' | 'unknown';

/** What the danger area shows. */
export type DeletionState =
  /** Not a person signed in to an account (an API token, no membership): nothing to show. */
  | { kind: 'none' }
  /** A member without the owner relation: "Delete me", at once and for good. */
  | { kind: 'user' }
  /** The owner and the only member: "Delete account", after `graceDays` days. */
  | { kind: 'account'; graceDays: number }
  /** Refused, with the reason (and, for an owner with members, how many). */
  | { kind: 'blocked'; reason: BlockReason; otherMembers: number | null }
  /** The account is scheduled for deletion: the date and "Cancel deletion". */
  | { kind: 'scheduled'; deleteAfter: string | null; canCancel: boolean };

/** The grace period Identity uses when it does not say (`IDENTITY_DELETION_GRACE_DAYS`). */
export const DEFAULT_GRACE_DAYS = 7;

const KNOWN_BLOCKS = new Set<BlockReason>([
  'other_members',
  'owns_another_account',
  'member_of_another_account',
]);

/** The membership of the account the user is signed in to. */
export function currentMembership(me: Me | null): Membership | null {
  if (!me?.account_id) return null;
  return me.memberships?.find((m) => m.account_id === me.account_id) ?? null;
}

/** The danger area's state, from `GET /me` alone (see the module documentation). */
export function deletionState(me: Me | null): DeletionState {
  if (!me || me.kind === 'api_token') return { kind: 'none' };
  const membership = currentMembership(me);
  if (!membership) return { kind: 'none' };
  if (membership.account_status === 'deleting') {
    return {
      kind: 'scheduled',
      deleteAfter: membership.delete_after ?? null,
      canCancel: membership.is_owner,
    };
  }
  const blocked = membership.self_deletion_blocked;
  if (blocked || !membership.self_deletion) {
    const reason: BlockReason = blocked && KNOWN_BLOCKS.has(blocked) ? blocked : 'unknown';
    return { kind: 'blocked', reason, otherMembers: membership.other_members ?? null };
  }
  if (membership.self_deletion === 'user') return { kind: 'user' };
  return { kind: 'account', graceDays: membership.deletion_grace_days ?? DEFAULT_GRACE_DAYS };
}

/** When a deletion requested `now` would start: `graceDays` later (the dialog's date). */
export function purgeDate(graceDays: number, now: number): Date {
  return new Date(now + graceDays * 24 * 3_600_000);
}

/** "19. November 2026 um 14:05" / "19 November 2026 at 14:05": the date of the purge. */
export function formatDeletionDate(value: string | number | Date, locale: string): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: 'long', timeStyle: 'short' }).format(
    new Date(value),
  );
}

/** The word the user types to confirm: their user name (the display name as a fallback). */
export function confirmationWord(me: Me | null): string {
  return (me?.username || me?.display_name || '').trim();
}

/** Whether the typed text confirms (case and outer spaces do not matter). */
export function confirms(typed: string, word: string): boolean {
  return word !== '' && typed.trim().toLowerCase() === word.toLowerCase();
}

/** How a `remove()` ended. */
export type RemoveOutcome = 'left' | 'scheduled' | 'cancelled' | 'failed';

export interface DeletionDeps {
  me: () => Me | null;
  deleteMe: () => Promise<SelfDeletionOut>;
  restore: (accountId: string) => Promise<void>;
  reloadMe: () => Promise<unknown>;
  /** Leave like a sign-out, with the login page's "deleted" notice. */
  leave: () => Promise<void>;
  /** Whether this installation runs Billing (the dialog then names the payment provider). */
  billing: () => Promise<boolean>;
}

const browserDeps: DeletionDeps = {
  me: () => session.me,
  deleteMe: async () =>
    (await unwrap(identityApi.DELETE('/api/v1/identity/me', {}))) as SelfDeletionOut,
  restore: async (accountId) => {
    await unwrap(
      identityApi.POST('/api/v1/identity/accounts/{account_id}/restore', {
        params: { path: { account_id: accountId } },
      }),
    );
  },
  reloadMe: () => session.loadMe(),
  leave: () => signOut('deleted'),
  billing: async () => (await probeBilling()) !== null,
};

export class AccountDeletion {
  /** The action that is running, for the pending state of its button. */
  busy = $state<'remove' | 'restore' | null>(null);
  /** The last failure, shown in place. */
  problem = $state<ApiProblem | null>(null);
  /** Which action {@link problem} belongs to (the banner shows only its own). */
  failed = $state<'remove' | 'restore' | null>(null);
  /** Whether the installation runs Billing; null until {@link loadBilling} answered. */
  billing = $state<boolean | null>(null);

  #deps: DeletionDeps;

  constructor(deps: DeletionDeps = browserDeps) {
    this.#deps = deps;
  }

  /** The state for the signed-in user (reactive through the session). */
  get state(): DeletionState {
    return deletionState(this.#deps.me());
  }

  /** The word the user types to confirm (their user name). */
  get confirmationWord(): string {
    return confirmationWord(this.#deps.me());
  }

  /** Ask once whether Billing runs (never fails: an unknown answer counts as no Billing). */
  async loadBilling(): Promise<void> {
    if (this.billing !== null) return;
    this.billing = await this.#deps.billing().catch(() => false);
  }

  /** Forget a shown failure (the dialog was closed). */
  clearProblem(): void {
    this.problem = null;
    this.failed = null;
  }

  /** "Delete me" or "Delete account": the server applies the operation of the caller's role. */
  async remove(): Promise<RemoveOutcome> {
    if (this.busy) return 'failed';
    this.busy = 'remove';
    this.clearProblem();
    try {
      const done = await this.#deps.deleteMe();
      if (done.operation === 'user') {
        await this.#deps.leave();
        return 'left';
      }
      await this.#deps.reloadMe().catch(() => undefined);
      return 'scheduled';
    } catch (error) {
      const problem = asProblem(error);
      // The step-up sheet was dismissed: nothing happened, nothing to report.
      if (problem.type === REAUTH_TYPE) return 'cancelled';
      this.problem = problem;
      this.failed = 'remove';
      // A 409 means `/me` was out of date (a member joined, the account was scheduled in
      // another tab): read it again so the area shows what applies now.
      if (problem.status === 409) await this.#deps.reloadMe().catch(() => undefined);
      return 'failed';
    } finally {
      this.busy = null;
    }
  }

  /** "Cancel deletion": the account is active again, the banner goes. */
  async restore(): Promise<boolean> {
    const accountId = this.#deps.me()?.account_id;
    if (this.busy || !accountId) return false;
    this.busy = 'restore';
    this.clearProblem();
    try {
      await this.#deps.restore(accountId);
      await this.#deps.reloadMe().catch(() => undefined);
      return true;
    } catch (error) {
      this.problem = asProblem(error);
      this.failed = 'restore';
      return false;
    } finally {
      this.busy = null;
    }
  }
}

/** The deletion of this tab, shared by the settings' danger area and the banner. */
export const accountDeletion = new AccountDeletion();
