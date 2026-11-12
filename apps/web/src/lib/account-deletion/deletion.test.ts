// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { describe, expect, it, vi } from 'vitest';

vi.mock('$app/navigation', () => ({ goto: vi.fn() }));

import { ApiProblem, problemFromBody } from '#lib/api/problem.js';
import type { Me, Membership } from '#lib/auth/session.svelte.js';
import { signOut, type SignOutDeps } from '#lib/auth/sign-out.js';

import {
  AccountDeletion,
  DEFAULT_GRACE_DAYS,
  confirmationWord,
  confirms,
  deletionState,
  formatDeletionDate,
  purgeDate,
  type DeletionDeps,
  type SelfDeletionOut,
} from './deletion.svelte.js';

const ACCOUNT = '00000000-0000-4000-8000-000000000001';
const OTHER = '00000000-0000-4000-8000-000000000002';
const WHEN = '2026-11-19T13:05:00Z';

function membership(overrides: Partial<Membership> = {}): Membership {
  return {
    account_id: ACCOUNT,
    account_name: 'Garten',
    account_status: 'active',
    is_owner: false,
    is_admin: false,
    self_deletion: 'user',
    self_deletion_blocked: null,
    other_members: null,
    delete_after: null,
    deletion_grace_days: null,
    ...overrides,
  };
}

function me(memberships: Membership[], overrides: Partial<Me> = {}): Me {
  return {
    subject: '11111111-1111-4111-8111-111111111111',
    kind: 'user',
    account_id: ACCOUNT,
    display_name: 'Anna',
    username: 'anna',
    roles: [],
    permissions: [],
    memberships,
    ...overrides,
  };
}

const OWNER = membership({
  is_owner: true,
  self_deletion: 'account',
  other_members: 0,
  deletion_grace_days: 7,
});

function problem(status: number, type: string, detail = 'nope'): ApiProblem {
  return problemFromBody(status, { type, title: 'Problem', status, detail });
}

function deps(overrides: Partial<DeletionDeps> = {}, who: Me | null = me([membership()])) {
  const calls: string[] = [];
  const base: DeletionDeps = {
    me: () => who,
    deleteMe: vi.fn(async () => {
      calls.push('delete');
      return { operation: 'user' as const, account_id: ACCOUNT, delete_after: null };
    }),
    restore: vi.fn(async () => {
      calls.push('restore');
    }),
    reloadMe: vi.fn(async () => {
      calls.push('reload');
    }),
    leave: vi.fn(async () => {
      calls.push('leave');
    }),
    billing: vi.fn(async () => true),
  };
  return { deps: { ...base, ...overrides }, calls };
}

describe('the state comes from GET /me alone', () => {
  it('is "delete me" for a member, sub-users and admins included', () => {
    expect(deletionState(me([membership()]))).toEqual({ kind: 'user' });
    const admin = membership({ is_admin: true });
    expect(deletionState(me([admin]))).toEqual({ kind: 'user' });
  });

  it('is "delete account" for the owner who is the only member, with the grace period', () => {
    expect(deletionState(me([OWNER]))).toEqual({ kind: 'account', graceDays: 7 });
    const older = { ...OWNER, deletion_grace_days: null };
    expect(deletionState(me([older]))).toEqual({
      kind: 'account',
      graceDays: DEFAULT_GRACE_DAYS,
    });
  });

  it('follows self_deletion, never is_owner', () => {
    // An owner flag with a "user" answer: the server decided, the app shows "delete me".
    const odd = membership({ is_owner: true, self_deletion: 'user' });
    expect(deletionState(me([odd]))).toEqual({ kind: 'user' });
  });

  it('is blocked with the reason the server names', () => {
    const members = { ...OWNER, self_deletion_blocked: 'other_members' as const, other_members: 2 };
    expect(deletionState(me([members]))).toEqual({
      kind: 'blocked',
      reason: 'other_members',
      otherMembers: 2,
    });
    const elsewhere = membership({
      self_deletion: null,
      self_deletion_blocked: 'member_of_another_account',
    });
    expect(deletionState(me([elsewhere]))).toMatchObject({
      kind: 'blocked',
      reason: 'member_of_another_account',
    });
    const owns = membership({ self_deletion: null, self_deletion_blocked: 'owns_another_account' });
    expect(deletionState(me([owns]))).toMatchObject({ reason: 'owns_another_account' });
  });

  it('is blocked without a known reason when the server refuses without one', () => {
    const refused = membership({ self_deletion: null });
    expect(deletionState(me([refused]))).toEqual({
      kind: 'blocked',
      reason: 'unknown',
      otherMembers: null,
    });
    const future = membership({
      self_deletion_blocked: 'something_new' as Membership['self_deletion_blocked'],
    });
    expect(deletionState(me([future]))).toMatchObject({ kind: 'blocked', reason: 'unknown' });
  });

  it('is scheduled while the account is deleting, with the date; the owner may cancel', () => {
    const deleting = { ...OWNER, account_status: 'deleting', delete_after: WHEN };
    expect(deletionState(me([deleting]))).toEqual({
      kind: 'scheduled',
      deleteAfter: WHEN,
      canCancel: true,
    });
    const member = membership({ account_status: 'deleting', delete_after: WHEN });
    expect(deletionState(me([member]))).toMatchObject({ canCancel: false });
  });

  it('reads the membership of the account signed in to', () => {
    const elsewhere = membership({ account_id: OTHER, self_deletion: 'account', is_owner: true });
    expect(deletionState(me([elsewhere, membership()]))).toEqual({ kind: 'user' });
  });

  it('shows nothing without a person signed in to an account', () => {
    expect(deletionState(null)).toEqual({ kind: 'none' });
    expect(deletionState(me([membership()], { kind: 'api_token' }))).toEqual({ kind: 'none' });
    expect(deletionState(me([], { account_id: null }))).toEqual({ kind: 'none' });
    expect(deletionState(me([membership({ account_id: OTHER })]))).toEqual({ kind: 'none' });
  });
});

describe('dates and the typed confirmation', () => {
  it('names the date the grace period ends', () => {
    const now = Date.parse('2026-11-12T13:05:00Z');
    expect(purgeDate(7, now).toISOString()).toBe('2026-11-19T13:05:00.000Z');
    expect(purgeDate(0, now).getTime()).toBe(now);
  });

  it('formats the date in German and English', () => {
    expect(formatDeletionDate(WHEN, 'de')).toMatch(/19\. November 2026/);
    expect(formatDeletionDate(WHEN, 'en')).toMatch(/(November 19, 2026|19 November 2026)/);
  });

  it('asks for the user name, case and outer spaces aside', () => {
    expect(confirmationWord(me([]))).toBe('anna');
    expect(confirmationWord(me([], { username: null }))).toBe('Anna');
    expect(confirmationWord(null)).toBe('');
    expect(confirms(' Anna ', 'anna')).toBe(true);
    expect(confirms('ann', 'anna')).toBe(false);
    expect(confirms('', '')).toBe(false);
  });
});

describe('delete me', () => {
  it('deletes, then leaves like a sign-out', async () => {
    const { deps: d, calls } = deps();
    const deletion = new AccountDeletion(d);
    expect(await deletion.remove()).toBe('left');
    expect(calls).toEqual(['delete', 'leave']);
    expect(deletion.busy).toBeNull();
    expect(deletion.problem).toBeNull();
  });

  it('leaves through the shell hand-over first, then the session, then the login page', async () => {
    const order: string[] = [];
    const signOutDeps: SignOutDeps = {
      handOver: async () => {
        order.push('shell');
      },
      forgetVisitor: () => order.push('visitor'),
      logout: async (reason) => {
        order.push(`logout:${reason}`);
      },
      toLogin: async () => {
        order.push('login');
      },
    };
    const { deps: d } = deps({ leave: () => signOut('deleted', signOutDeps) });
    await new AccountDeletion(d).remove();
    expect(order).toEqual(['shell', 'visitor', 'logout:deleted', 'login']);
  });

  it('still ends the session when the shell hand-over fails (the user is gone already)', async () => {
    const order: string[] = [];
    await signOut('deleted', {
      handOver: async () => {
        throw new Error('401');
      },
      forgetVisitor: () => order.push('visitor'),
      logout: async () => {
        order.push('logout');
      },
      toLogin: async () => {
        order.push('login');
      },
    });
    expect(order).toEqual(['visitor', 'logout', 'login']);
  });

  it('a plain sign-out has no reason', async () => {
    const logout = vi.fn(async () => undefined);
    await signOut(undefined, {
      handOver: async () => undefined,
      forgetVisitor: () => undefined,
      logout,
      toLogin: async () => undefined,
    });
    expect(logout).toHaveBeenCalledWith(null);
  });
});

describe('delete account', () => {
  it('schedules, stays signed in and reads /me again for the banner', async () => {
    const { deps: d, calls } = deps(
      {
        deleteMe: vi.fn(async () => {
          calls.push('delete');
          return { operation: 'account' as const, account_id: ACCOUNT, delete_after: WHEN };
        }),
      },
      me([OWNER]),
    );
    expect(await new AccountDeletion(d).remove()).toBe('scheduled');
    expect(calls).toEqual(['delete', 'reload']);
  });

  it('reports nothing when the step-up sheet was dismissed', async () => {
    const { deps: d, calls } = deps({
      deleteMe: vi.fn(async () => {
        throw problem(401, 'urn:mowdeck:problem:reauth-required');
      }),
    });
    const deletion = new AccountDeletion(d);
    expect(await deletion.remove()).toBe('cancelled');
    expect(deletion.problem).toBeNull();
    expect(calls).toEqual([]);
  });

  it('shows a refusal and reads /me again, so the area shows what applies now', async () => {
    const { deps: d, calls } = deps({
      deleteMe: vi.fn(async () => {
        throw problem(409, 'urn:mowdeck:problem:conflict', 'the account still has 1 other member');
      }),
    });
    const deletion = new AccountDeletion(d);
    expect(await deletion.remove()).toBe('failed');
    expect(deletion.problem?.status).toBe(409);
    expect(deletion.failed).toBe('remove');
    expect(calls).toEqual(['reload']);
  });

  it('shows any other failure without leaving', async () => {
    const { deps: d, calls } = deps({
      deleteMe: vi.fn(async () => {
        throw ApiProblem.network();
      }),
    });
    const deletion = new AccountDeletion(d);
    expect(await deletion.remove()).toBe('failed');
    expect(deletion.problem?.status).toBe(0);
    expect(calls).toEqual([]);
  });

  it('runs one action at a time', async () => {
    let release: () => void = () => undefined;
    const { deps: d } = deps({
      deleteMe: vi.fn(
        () =>
          new Promise<SelfDeletionOut>((resolve) => {
            release = () =>
              resolve({ operation: 'account' as const, account_id: ACCOUNT, delete_after: WHEN });
          }),
      ),
    });
    const deletion = new AccountDeletion(d);
    const first = deletion.remove();
    expect(deletion.busy).toBe('remove');
    expect(await deletion.remove()).toBe('failed');
    expect(await deletion.restore()).toBe(false);
    release();
    expect(await first).toBe('scheduled');
    expect(d.deleteMe).toHaveBeenCalledTimes(1);
  });

  it('asks once whether Billing runs, and an error counts as no Billing', async () => {
    const { deps: d } = deps();
    const deletion = new AccountDeletion(d);
    await deletion.loadBilling();
    await deletion.loadBilling();
    expect(deletion.billing).toBe(true);
    expect(d.billing).toHaveBeenCalledTimes(1);
    const failing = new AccountDeletion(
      deps({
        billing: async () => {
          throw new Error('down');
        },
      }).deps,
    );
    await failing.loadBilling();
    expect(failing.billing).toBe(false);
  });
});

describe('cancel deletion', () => {
  const deleting = me([{ ...OWNER, account_status: 'deleting', delete_after: WHEN }]);

  it('restores the account and reads /me again, so the banner goes', async () => {
    const { deps: d, calls } = deps({}, deleting);
    const deletion = new AccountDeletion(d);
    expect(await deletion.restore()).toBe(true);
    expect(d.restore).toHaveBeenCalledWith(ACCOUNT);
    expect(calls).toEqual(['restore', 'reload']);
  });

  it('shows a failure as the restore problem', async () => {
    const { deps: d } = deps(
      {
        restore: vi.fn(async () => {
          throw problem(409, 'urn:mowdeck:problem:conflict', 'the purge has started');
        }),
      },
      deleting,
    );
    const deletion = new AccountDeletion(d);
    expect(await deletion.restore()).toBe(false);
    expect(deletion.failed).toBe('restore');
    expect(deletion.problem?.detail).toBe('the purge has started');
  });

  it('does nothing without an account', async () => {
    const { deps: d } = deps({}, null);
    expect(await new AccountDeletion(d).restore()).toBe(false);
    expect(d.restore).not.toHaveBeenCalled();
  });
});
