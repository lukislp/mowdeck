// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$app/navigation', () => ({ goto: vi.fn() }));

import { problemFromBody } from '#lib/api/problem.js';
import type { Me, Membership } from '#lib/auth/session.svelte.js';
import { demo } from '#lib/demo/state.svelte.js';
import { setLocale } from '#lib/paraglide/runtime.js';
import { toasts } from '#lib/stores/ui.svelte.js';

import AccountDeletionPanel from './AccountDeletionPanel.svelte';
import DeletionBanner from './DeletionBanner.svelte';
import { AccountDeletion, type DeletionDeps } from './deletion.svelte.js';

const ACCOUNT = '00000000-0000-4000-8000-000000000001';
const WHEN = '2026-11-19T13:05:00Z';
const NOW = Date.parse('2026-11-12T13:05:00Z');

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

const OWNER = membership({
  is_owner: true,
  self_deletion: 'account',
  other_members: 0,
  deletion_grace_days: 7,
});

class Fake {
  who = $state<Me | null>(null);
  deps: DeletionDeps;

  constructor(memberships: Membership[]) {
    this.who = {
      subject: '11111111-1111-4111-8111-111111111111',
      kind: 'user',
      account_id: ACCOUNT,
      display_name: 'Anna',
      username: 'anna',
      roles: [],
      permissions: [],
      memberships,
    };
    this.deps = {
      me: () => this.who,
      deleteMe: vi.fn(async () => ({
        operation: 'user' as const,
        account_id: ACCOUNT,
        delete_after: null,
      })),
      restore: vi.fn(async () => undefined),
      reloadMe: vi.fn(async () => undefined),
      leave: vi.fn(async () => undefined),
      billing: vi.fn(async () => false),
    };
  }

  set(memberships: Membership[]): void {
    if (this.who) this.who = { ...this.who, memberships };
  }
}

function mount(fake: Fake) {
  const deletion = new AccountDeletion(fake.deps);
  render(AccountDeletionPanel, { props: { deletion, now: () => NOW } });
  return deletion;
}

async function confirmWith(word: string): Promise<void> {
  await fireEvent.click(screen.getByTestId('deletion-start'));
  await fireEvent.input(screen.getByTestId('deletion-confirm-input'), {
    target: { value: word },
  });
  await fireEvent.click(screen.getByTestId('deletion-confirm'));
}

beforeEach(() => setLocale('de', { reload: false }));
afterEach(() => {
  demo.config = null;
  toasts.items = [];
});

describe('the danger area', () => {
  it('offers "delete me" to a member: at once, no way back, what stays', async () => {
    const fake = new Fake([membership()]);
    mount(fake);
    expect(screen.getByTestId('deletion-panel')).toHaveTextContent('Mich löschen');
    expect(screen.getByTestId('deletion-area')).toHaveTextContent(
      'sofort und endgültig aus diesem Konto',
    );
    await fireEvent.click(screen.getByTestId('deletion-start'));
    const area = screen.getByTestId('deletion-area');
    expect(area).toHaveTextContent('Dein Name wird daraus entfernt');
    expect(area).toHaveTextContent('Zeitpläne, die dir gehören, pausieren');
    expect(area).toHaveTextContent('Links zum Teilen');
    expect(area).toHaveTextContent('bis zu 24 Stunden');
    expect(area).toHaveTextContent('bis zu 30 Tage');
    expect(area).toHaveTextContent('Sicherungen, die der Betreiber');
    expect(area).toHaveTextContent('Cloud-KI-Anbieter');
  });

  it('deletes a member only after the user name was typed, then leaves', async () => {
    const fake = new Fake([membership()]);
    mount(fake);
    await fireEvent.click(screen.getByTestId('deletion-start'));
    expect(screen.getByTestId('deletion-confirm')).toBeDisabled();
    await fireEvent.input(screen.getByTestId('deletion-confirm-input'), {
      target: { value: 'ann' },
    });
    expect(screen.getByTestId('deletion-confirm')).toBeDisabled();
    await fireEvent.input(screen.getByTestId('deletion-confirm-input'), {
      target: { value: 'anna' },
    });
    await fireEvent.click(screen.getByTestId('deletion-confirm'));
    await waitFor(() => expect(fake.deps.leave).toHaveBeenCalledTimes(1));
    expect(fake.deps.deleteMe).toHaveBeenCalledTimes(1);
  });

  it('can be closed again without deleting anything', async () => {
    const fake = new Fake([membership()]);
    mount(fake);
    await fireEvent.click(screen.getByTestId('deletion-start'));
    await fireEvent.click(screen.getByRole('button', { name: 'Behalten' }));
    expect(screen.queryByTestId('deletion-confirm-form')).toBeNull();
    expect(fake.deps.deleteMe).not.toHaveBeenCalled();
  });

  it('offers "delete account" to the owner with the exact date and the grace period', async () => {
    const fake = new Fake([OWNER]);
    mount(fake);
    expect(screen.getByTestId('deletion-panel')).toHaveTextContent('Konto löschen');
    const grace = screen.getByTestId('deletion-grace');
    expect(grace).toHaveTextContent('Die Löschung beginnt am 19. November 2026');
    expect(grace).toHaveTextContent('vollen Zugriff');
    expect(grace).toHaveTextContent('jederzeit stoppen');
    expect(grace).toHaveTextContent('abbrechen');
    await fireEvent.click(screen.getByTestId('deletion-start'));
    expect(screen.getByTestId('deletion-area')).toHaveTextContent('Herstellerkonten');
    expect(screen.queryByTestId('deletion-billing')).toBeNull();
  });

  it('names the payment provider, and no refund, only where Billing runs', async () => {
    const fake = new Fake([OWNER]);
    fake.deps.billing = vi.fn(async () => true);
    mount(fake);
    await fireEvent.click(screen.getByTestId('deletion-start'));
    const billing = await screen.findByTestId('deletion-billing');
    expect(billing).toHaveTextContent('ohne Erstattung');
    expect(billing).toHaveTextContent(
      'Rechnungen und Zahlungsbelege bleiben beim Zahlungsanbieter',
    );
  });

  it('schedules the account and tells the date', async () => {
    const fake = new Fake([OWNER]);
    fake.deps.deleteMe = vi.fn(async () => {
      fake.set([{ ...OWNER, account_status: 'deleting', delete_after: WHEN }]);
      return { operation: 'account' as const, account_id: ACCOUNT, delete_after: WHEN };
    });
    mount(fake);
    await confirmWith('anna');
    await waitFor(() =>
      expect(screen.getByTestId('deletion-scheduled')).toHaveTextContent(
        'Dieses Konto wird am 19. November 2026',
      ),
    );
    expect(fake.deps.leave).not.toHaveBeenCalled();
    expect(toasts.items.at(-1)?.title).toMatch(/19\. November 2026/);
  });

  it('shows a refusal of the server in place', async () => {
    const fake = new Fake([OWNER]);
    fake.deps.deleteMe = vi.fn(async () => {
      throw problemFromBody(409, {
        type: 'urn:mowdeck:problem:conflict',
        title: 'Conflict',
        status: 409,
        detail: 'the account still has 1 other member(s)',
      });
    });
    mount(fake);
    await confirmWith('anna');
    expect(await screen.findByTestId('deletion-error')).toHaveTextContent('1 other member');
  });

  it('explains a block: the owner removes the other members first', () => {
    const fake = new Fake([{ ...OWNER, self_deletion_blocked: 'other_members', other_members: 2 }]);
    mount(fake);
    expect(screen.getByTestId('deletion-blocked')).toHaveTextContent(
      'Dieses Konto hat noch 2 weitere Mitglieder',
    );
    expect(screen.getByTestId('deletion-to-users')).toHaveAttribute('href', '/settings#users');
    expect(screen.queryByTestId('deletion-start')).toBeNull();
  });

  it('explains the other refusals the server names', () => {
    const fake = new Fake([
      membership({ self_deletion: null, self_deletion_blocked: 'member_of_another_account' }),
    ]);
    mount(fake);
    expect(screen.getByTestId('deletion-blocked')).toHaveTextContent(
      'Verlasse zuerst dieses Konto',
    );
    expect(screen.queryByTestId('deletion-to-users')).toBeNull();
  });

  it('shows the scheduled state with "cancel deletion"', async () => {
    const fake = new Fake([{ ...OWNER, account_status: 'deleting', delete_after: WHEN }]);
    mount(fake);
    await fireEvent.click(screen.getByTestId('deletion-cancel'));
    await waitFor(() => expect(fake.deps.restore).toHaveBeenCalledWith(ACCOUNT));
  });

  it('speaks English in the English app', async () => {
    setLocale('en', { reload: false });
    mount(new Fake([OWNER]));
    expect(screen.getByTestId('deletion-grace')).toHaveTextContent(
      /The deletion starts on (November 19, 2026|19 November 2026)/,
    );
    await fireEvent.click(screen.getByTestId('deletion-start'));
    expect(screen.getByTestId('deletion-confirm')).toHaveTextContent('Delete account on');
  });

  it('is locked in the public demo and says why', () => {
    demo.config = { resetHours: 6 };
    mount(new Fake([OWNER]));
    expect(screen.getByTestId('demo-lock-deletion')).toHaveTextContent(
      'In der Demo nicht verfügbar',
    );
    expect(screen.getByTestId('deletion-demo')).toHaveTextContent(
      'In der öffentlichen Demo lässt sich nichts löschen',
    );
    expect(screen.getByTestId('deletion-start')).toBeDisabled();
  });

  it('shows nothing to an API token', () => {
    const fake = new Fake([membership()]);
    if (fake.who) fake.who = { ...fake.who, kind: 'api_token' };
    mount(fake);
    expect(screen.queryByTestId('deletion-panel')).toBeNull();
  });
});

describe('the banner', () => {
  function banner(fake: Fake) {
    const deletion = new AccountDeletion(fake.deps);
    render(DeletionBanner, { props: { deletion } });
    return deletion;
  }

  it('is shown while the account is scheduled, with the date in German and English', () => {
    const fake = new Fake([{ ...OWNER, account_status: 'deleting', delete_after: WHEN }]);
    banner(fake);
    expect(screen.getByTestId('deletion-banner')).toHaveTextContent(
      'Dieses Konto wird am 19. November 2026',
    );
    cleanup();
    setLocale('en', { reload: false });
    banner(fake);
    expect(screen.getByTestId('deletion-banner')).toHaveTextContent(
      /This account will be deleted on (November 19, 2026|19 November 2026)/,
    );
  });

  it('is not shown otherwise and cannot be dismissed', () => {
    banner(new Fake([OWNER]));
    expect(screen.queryByTestId('deletion-banner')).toBeNull();
  });

  it('goes when the deletion is cancelled', async () => {
    const fake = new Fake([{ ...OWNER, account_status: 'deleting', delete_after: WHEN }]);
    fake.deps.restore = vi.fn(async () => fake.set([OWNER]));
    banner(fake);
    expect(screen.queryByRole('button', { name: /schließen|close/i })).toBeNull();
    await fireEvent.click(screen.getByTestId('deletion-banner-cancel'));
    await waitFor(() => expect(screen.queryByTestId('deletion-banner')).toBeNull());
    expect(toasts.items.at(-1)?.title).toBe('Löschung abgebrochen. Das Konto bleibt bestehen.');
  });

  it('shows a failed cancel in place', async () => {
    const fake = new Fake([{ ...OWNER, account_status: 'deleting', delete_after: WHEN }]);
    fake.deps.restore = vi.fn(async () => {
      throw problemFromBody(409, {
        type: 'urn:mowdeck:problem:conflict',
        title: 'Conflict',
        status: 409,
        detail: 'the purge has started',
      });
    });
    banner(fake);
    await fireEvent.click(screen.getByTestId('deletion-banner-cancel'));
    expect(await screen.findByText('the purge has started')).toBeInTheDocument();
    expect(screen.getByTestId('deletion-banner')).toBeInTheDocument();
  });

  it('has no cancel button for a member', () => {
    const fake = new Fake([membership({ account_status: 'deleting', delete_after: WHEN })]);
    banner(fake);
    expect(screen.queryByTestId('deletion-banner-cancel')).toBeNull();
  });
});
