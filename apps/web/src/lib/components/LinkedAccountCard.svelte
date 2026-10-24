<!--
  One linked account on the settings page: e-mail and provider, device and mower counts, the
  last read and (prominently) why it failed, "read again" (optionally with a new, write-only
  password) and unlinking with a confirmation; a 409 names the mowers that still use it.
  `onsync` and `onremove` do the requests and throw the ApiProblem.
-->
<script lang="ts">
  import { Button, Field, Icon, ProblemNotice } from '@mowdeck/ui';

  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { formatDateTime } from '#lib/i18n.js';
  import {
    accountIssue,
    inUseMowers,
    type AccountIssue,
    type LinkedAccount,
  } from '#lib/linked/accounts.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    account: LinkedAccount;
    onsync: (password?: string) => Promise<void>;
    onremove: () => Promise<void>;
    /** Without `can_manage_mowers` nothing can be changed. */
    readonly?: boolean;
  }
  let { account, onsync, onremove, readonly = false }: Props = $props();

  let syncing = $state(false);
  let withPassword = $state(false);
  let password = $state('');
  let syncIssue = $state<AccountIssue | null>(null);
  let confirming = $state(false);
  let removing = $state(false);
  let removeProblem = $state<ApiProblem | null>(null);

  const inUse = $derived(removeProblem ? inUseMowers(removeProblem) : []);
  const key = $derived(account.id);

  async function sync(): Promise<void> {
    syncing = true;
    syncIssue = null;
    try {
      await onsync(withPassword && password ? password : undefined);
      withPassword = false;
    } catch (error) {
      syncIssue = accountIssue(asProblem(error));
    } finally {
      password = '';
      syncing = false;
    }
  }

  async function remove(): Promise<void> {
    if (!confirming) {
      confirming = true;
      removeProblem = null;
      return;
    }
    removing = true;
    try {
      await onremove();
    } catch (error) {
      removeProblem = asProblem(error);
      confirming = false;
    } finally {
      removing = false;
    }
  }
</script>

<article
  class="account"
  class:failing={!!account.last_sync_error}
  data-testid="linked-account"
  data-email={account.email}
  aria-labelledby="linked-account-{key}-email"
>
  <header>
    <span class="provider">{account.provider}</span>
    <h3 id="linked-account-{key}-email">{account.email}</h3>
  </header>

  <dl class="stats">
    <div>
      <dt>{m.linked_devices_count()}</dt>
      <dd data-testid="linked-account-devices">{account.device_count}</dd>
    </div>
    <div>
      <dt>{m.linked_mowers_count()}</dt>
      <dd data-testid="linked-account-mowers">{account.mower_count}</dd>
    </div>
    <div class="wide">
      <dt>{m.linked_last_sync()}</dt>
      <dd>{account.last_synced_at ? formatDateTime(account.last_synced_at) : m.never()}</dd>
    </div>
  </dl>

  {#if account.last_sync_error}
    <div class="error" role="alert" data-testid="linked-account-error">
      <Icon name="alert" size={18} />
      <span class="what">{m.linked_sync_error()}</span>
      <span class="why">{account.last_sync_error}</span>
    </div>
  {/if}

  {#if !readonly}
    {#if withPassword}
      <Field
        id="linked-account-{key}-password"
        type="password"
        label={m.linked_new_password()}
        autocomplete="new-password"
        disabled={syncing}
        hint={m.linked_new_password_hint()}
        error={syncIssue?.field === 'password' ? syncIssue.message : null}
        bind:value={password}
      />
    {/if}
    {#if syncIssue && syncIssue.field !== 'password'}
      <ProblemNotice title={syncIssue.message} detail={syncIssue.detail} />
    {:else if syncIssue && !withPassword}
      <ProblemNotice title={syncIssue.message} />
    {/if}
    {#if syncing}
      <p class="pending" role="status">{m.linked_connecting()} {m.linked_connecting_hint()}</p>
    {/if}

    <div class="actions">
      <Button icon="signal" pending={syncing} onclick={sync} testid="linked-account-sync"
        >{m.linked_sync()}</Button
      >
      <Button
        variant="ghost"
        icon="key"
        expanded={withPassword}
        disabled={syncing}
        onclick={() => {
          withPassword = !withPassword;
          password = '';
        }}
        testid="linked-account-password-toggle">{m.linked_new_password_toggle()}</Button
      >
      <span class="spacer"></span>
      <Button
        variant="danger"
        icon="trash"
        pending={removing}
        onclick={remove}
        testid={confirming ? 'linked-account-delete-confirm' : 'linked-account-delete'}
        >{confirming ? m.linked_delete_confirm() : m.linked_delete()}</Button
      >
      {#if confirming && !removing}
        <Button variant="ghost" onclick={() => (confirming = false)}>{m.cancel()}</Button>
      {/if}
    </div>
    {#if confirming}
      <p class="note">{m.linked_delete_body()}</p>
    {/if}
    {#if removeProblem}
      {#if inUse.length > 0}
        <div class="in-use" role="alert" data-testid="linked-account-in-use">
          <span>{m.linked_in_use()}</span>
          <ul>
            {#each inUse as mower (mower.id)}
              <li><a href="/mowers/{mower.id}/settings">{mower.name}</a></li>
            {/each}
          </ul>
        </div>
      {:else}
        <ProblemNotice
          title={accountIssue(removeProblem).message}
          detail={removeProblem.detail}
          reference={removeProblem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      {/if}
    {/if}
  {/if}
</article>

<style>
  .account {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
    padding: var(--md-space-4) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    border-left: 3px solid var(--md-color-ink);
    background: var(--md-color-surface);
  }

  .account.failing {
    border-left-color: var(--md-state-error);
  }

  header {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .provider {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  h3 {
    margin: 0;
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  .stats {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--md-hairline);
    margin: 0;
    border: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-line);
  }

  .stats div {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    padding: var(--md-space-2) var(--md-space-3);
    background: var(--md-color-surface-sunken);
  }

  .stats .wide {
    grid-column: 1 / -1;
  }

  dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  dd {
    margin: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-16);
  }

  .error {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--md-space-1) var(--md-space-2);
    padding: var(--md-space-3);
    border-inline-start: 4px solid var(--md-state-error);
    background: var(--md-state-error-bg);
  }

  .what {
    font-weight: var(--md-weight-semibold);
  }

  .why {
    grid-column: 2;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
    overflow-wrap: anywhere;
  }

  .pending,
  .note {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
  }

  .spacer {
    flex: 1;
  }

  .in-use {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    padding: var(--md-space-3);
    border-inline-start: 4px solid var(--md-state-error);
    background: var(--md-state-error-bg);
  }

  .in-use ul {
    margin: 0;
    padding-left: var(--md-space-4);
  }

  .in-use a {
    color: var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
  }

  @media (min-width: 768px) {
    .stats {
      grid-template-columns: 1fr 1fr 2fr;
    }

    .stats .wide {
      grid-column: auto;
    }
  }
</style>
