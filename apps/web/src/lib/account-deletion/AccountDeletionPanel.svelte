<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  The danger area of Settings -> Profile (ADR 0082, step 4): "Delete me" for a member, "Delete
  account" for the owner who is the only member, the reason when Identity refuses, and the date
  with "Cancel deletion" while the account is scheduled. What applies, and every action, comes
  from `deletion.svelte.ts`; this only renders it. Locked in the public demo.
-->
<script lang="ts">
  import { Button, Field, Icon, Panel, ProblemNotice } from '@mowdeck/ui';
  import { tick } from 'svelte';

  import DemoLock from '#lib/demo/DemoLock.svelte';
  import { demo } from '#lib/demo/state.svelte.js';
  import { problemMessage } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import { sectionHref } from '#lib/settings/sections.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  import {
    accountDeletion,
    confirms,
    formatDeletionDate,
    purgeDate,
    type AccountDeletion,
  } from './deletion.svelte.js';

  interface Props {
    deletion?: AccountDeletion;
    now?: () => number;
  }
  let { deletion = accountDeletion, now = () => Date.now() }: Props = $props();

  const view = $derived(deletion.state);
  const word = $derived(deletion.confirmationWord);
  const fieldId = $props.id();

  let open = $state(false);
  let typed = $state('');

  const date = (value: string | number | Date) => formatDeletionDate(value, getLocale());
  const plannedDate = $derived(
    view.kind === 'account' ? date(purgeDate(view.graceDays, now())) : '',
  );

  $effect(() => {
    if (view.kind === 'account') void deletion.loadBilling();
  });

  async function start(): Promise<void> {
    open = true;
    typed = '';
    deletion.clearProblem();
    await tick();
    document.getElementById(fieldId)?.focus();
  }

  function keep(): void {
    open = false;
    typed = '';
    deletion.clearProblem();
  }

  async function cancel(): Promise<void> {
    if (await deletion.restore()) toasts.push('ok', m.deletion_cancelled());
  }

  async function confirm(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!confirms(typed, word)) return;
    const outcome = await deletion.remove();
    if (outcome === 'scheduled') {
      open = false;
      typed = '';
      const after = deletion.state;
      if (after.kind === 'scheduled' && after.deleteAfter) {
        toasts.push('info', m.deletion_scheduled_toast({ date: date(after.deleteAfter) }));
      }
    }
  }
</script>

<!-- A failed "Cancel deletion" is shown by the banner, which is on this page too. -->
{#snippet failure()}
  {#if deletion.problem && deletion.failed === 'remove'}
    <div data-testid="deletion-error">
      <ProblemNotice
        title={problemMessage(deletion.problem)}
        detail={deletion.problem.detail}
        reference={deletion.problem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    </div>
  {/if}
{/snippet}

{#snippet confirmForm(action: string)}
  <form class="confirm" onsubmit={confirm} novalidate data-testid="deletion-confirm-form">
    <Field
      id={fieldId}
      label={m.deletion_confirm_label({ word })}
      hint={m.deletion_confirm_hint()}
      autocomplete="off"
      testid="deletion-confirm-input"
      bind:value={typed}
    />
    {@render failure()}
    <div class="actions">
      <Button
        type="submit"
        variant="danger"
        icon="trash"
        pending={deletion.busy === 'remove'}
        disabled={!confirms(typed, word)}
        testid="deletion-confirm">{action}</Button
      >
      <Button variant="ghost" disabled={deletion.busy === 'remove'} onclick={keep}
        >{m.deletion_keep()}</Button
      >
    </div>
  </form>
{/snippet}

{#if view.kind !== 'none'}
  <div id="delete">
    <Panel
      label={view.kind === 'user' ? m.deletion_title_me() : m.deletion_title_account()}
      testid="deletion-panel"
    >
      {#if demo.active}
        <p class="text" data-testid="deletion-demo">{m.deletion_demo()}</p>
      {/if}
      <DemoLock testid="demo-lock-deletion">
        <div class="area" data-kind={view.kind} data-testid="deletion-area">
          {#if view.kind === 'user'}
            <p class="lead">{m.deletion_me_intro()}</p>
            {#if open}
              <h3 class="label">{m.deletion_what_happens()}</h3>
              <ul class="list">
                <li>{m.deletion_me_deleted()}</li>
                <li>{m.deletion_me_stays()}</li>
                <li>{m.deletion_me_schedules()}</li>
                <li>{m.deletion_me_links()}</li>
                <li>{m.deletion_me_access()}</li>
                <li>{m.deletion_third_push()}</li>
                <li>{m.deletion_third_ai()}</li>
              </ul>
              <h3 class="label">{m.deletion_what_remains()}</h3>
              <ul class="list">
                <li>{m.deletion_me_remains()}</li>
                <li>{m.deletion_backups()}</li>
              </ul>
              {@render confirmForm(m.deletion_confirm_me())}
            {:else}
              <div>
                <Button variant="danger" icon="trash" testid="deletion-start" onclick={start}
                  >{m.deletion_start_me()}</Button
                >
              </div>
            {/if}
          {:else if view.kind === 'account'}
            <p class="lead">{m.deletion_account_intro()}</p>
            <p class="text" data-testid="deletion-grace">
              {m.deletion_account_grace({ date: plannedDate })}
            </p>
            {#if open}
              <h3 class="label">{m.deletion_what_happens()}</h3>
              <ul class="list">
                <li>{m.deletion_account_locked()}</li>
                {#if deletion.billing}
                  <li data-testid="deletion-billing">{m.deletion_account_billing()}</li>
                {/if}
                <li>{m.deletion_third_vendor()}</li>
                <li>{m.deletion_third_push()}</li>
                <li>{m.deletion_third_ai()}</li>
              </ul>
              <h3 class="label">{m.deletion_what_remains()}</h3>
              <ul class="list">
                <li>{m.deletion_account_remains()}</li>
                <li>{m.deletion_backups()}</li>
              </ul>
              {@render confirmForm(m.deletion_confirm_account({ date: plannedDate }))}
            {:else}
              <div>
                <Button variant="danger" icon="trash" testid="deletion-start" onclick={start}
                  >{m.deletion_start_account()}</Button
                >
              </div>
            {/if}
          {:else if view.kind === 'blocked'}
            <p class="blocked" data-testid="deletion-blocked">
              <Icon name="alert" size={16} />
              <span>
                {#if view.reason === 'other_members'}
                  {m.deletion_blocked_members({ count: view.otherMembers ?? 1 })}
                {:else if view.reason === 'owns_another_account'}
                  {m.deletion_blocked_owns_other()}
                {:else if view.reason === 'member_of_another_account'}
                  {m.deletion_blocked_member_other()}
                {:else}
                  {m.deletion_blocked_unknown()}
                {/if}
              </span>
            </p>
            {#if view.reason === 'other_members'}
              <div>
                <Button
                  variant="secondary"
                  icon="user"
                  href={sectionHref('users')}
                  testid="deletion-to-users">{m.deletion_blocked_users()}</Button
                >
              </div>
            {/if}
          {:else if view.kind === 'scheduled'}
            <p class="lead" data-testid="deletion-scheduled">
              {view.deleteAfter
                ? m.deletion_scheduled({ date: date(view.deleteAfter) })
                : m.deletion_scheduled_unknown()}
            </p>
            {#if view.canCancel}
              <p class="text">{m.deletion_scheduled_hint()}</p>
              <div>
                <Button
                  variant="primary"
                  icon="back"
                  pending={deletion.busy === 'restore'}
                  testid="deletion-cancel"
                  onclick={cancel}>{m.deletion_cancel()}</Button
                >
              </div>
            {:else}
              <p class="text">{m.deletion_scheduled_member()}</p>
            {/if}
          {/if}
        </div>
      </DemoLock>
    </Panel>
  </div>
{/if}

<style>
  .area {
    display: grid;
    gap: var(--md-space-3);
    max-width: 640px;
    padding-inline-start: var(--md-space-3);
    border-inline-start: 3px solid var(--md-state-error);
  }

  .area[data-kind='blocked'] {
    border-inline-start-color: var(--md-color-line-strong);
  }

  .lead,
  .text,
  .list,
  .blocked {
    margin: 0;
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .lead {
    color: var(--md-color-ink);
    font-weight: var(--md-weight-medium);
  }

  .text,
  .list {
    color: var(--md-color-ink-muted);
  }

  .list {
    display: grid;
    gap: var(--md-space-2);
    padding-inline-start: var(--md-space-4);
  }

  .label {
    margin: var(--md-space-2) 0 0;
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .blocked {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-2);
    color: var(--md-color-ink);
  }

  .confirm {
    display: grid;
    gap: var(--md-space-3);
    max-width: 480px;
    margin-top: var(--md-space-2);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }
</style>
