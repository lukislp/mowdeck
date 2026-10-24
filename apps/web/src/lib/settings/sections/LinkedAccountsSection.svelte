<!--
  Linked Mammotion accounts (Fleet's `/linked-accounts`): every account with its counts, last
  read and last error, "read again" (optionally with a new password), unlinking (refused while
  mowers use it) and the form to link another one. Needs `can_manage_mowers` on the account;
  a 403 shows a calm notice instead of an error.
-->
<script lang="ts">
  import { Panel, ProblemNotice } from '@mowdeck/ui';

  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import LinkedAccountCard from '#lib/components/LinkedAccountCard.svelte';
  import LinkedAccountConnect from '#lib/components/LinkedAccountConnect.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { problemMessage } from '#lib/i18n.js';
  import type { LinkedAccount } from '#lib/linked/accounts.js';
  import {
    linkAccount,
    listLinkedAccounts,
    removeLinkedAccount,
    syncAccount,
  } from '#lib/linked/api.js';
  import { m } from '#lib/paraglide/messages.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  let accounts = $state<LinkedAccount[] | null>(null);
  let problem = $state<ApiProblem | null>(null);

  const forbidden = $derived(problem?.status === 403);

  async function load(): Promise<void> {
    try {
      accounts = await listLinkedAccounts();
      problem = null;
    } catch (error) {
      problem = asProblem(error);
    }
  }

  $effect(() => {
    void load();
  });

  /** Replace one account with a fresher copy (the detail answers carry the counts too). */
  function put(next: LinkedAccount): void {
    const list = accounts ?? [];
    accounts = list.some((a) => a.id === next.id)
      ? list.map((a) => (a.id === next.id ? next : a))
      : [...list, next];
  }

  async function connect(email: string, password: string): Promise<void> {
    const linked = await linkAccount(email, password);
    put(linked);
    toasts.push('ok', m.linked_connected({ email: linked.email }));
  }

  async function sync(account: LinkedAccount, password?: string): Promise<void> {
    try {
      put(await syncAccount(account.id, password));
      toasts.push('ok', m.linked_synced({ email: account.email }));
    } catch (error) {
      // The failed read is recorded on the account (`last_sync_error`): show it.
      void load();
      throw error;
    }
  }

  async function remove(account: LinkedAccount): Promise<void> {
    await removeLinkedAccount(account.id);
    accounts = (accounts ?? []).filter((a) => a.id !== account.id);
    toasts.push('ok', m.linked_deleted({ email: account.email }));
  }
</script>

<p class="pad intro">{m.linked_intro()}</p>

{#if forbidden}
  <div class="pad" data-testid="linked-forbidden">
    <ProblemNotice tone="info" title={m.linked_no_permission()} />
  </div>
{:else if problem && !accounts}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      detail={problem.detail}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !accounts}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else}
  <section class="list" data-testid="linked-accounts" aria-label={m.linked_title()}>
    {#if accounts.length === 0}
      <p class="pad quiet" data-testid="linked-empty">{m.linked_empty()}</p>
    {:else}
      {#each accounts as account (account.id)}
        <LinkedAccountCard
          {account}
          onsync={(password) => sync(account, password)}
          onremove={() => remove(account)}
        />
      {/each}
    {/if}
  </section>

  <Panel label={m.linked_connect_title()} testid="linked-connect-panel">
    <div class="connect">
      <LinkedAccountConnect onconnect={connect} />
    </div>
  </Panel>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .intro {
    max-width: 720px;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .list {
    max-width: 720px;
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .connect {
    max-width: 640px;
    padding-bottom: var(--md-space-6);
  }
</style>
