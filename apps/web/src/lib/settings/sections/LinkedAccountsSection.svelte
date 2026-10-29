<!--
  Linked vendor cloud accounts (Fleet's `/linked-accounts`, ADR 0034 and ADR 0065): every account
  with its vendor, status, counts, last read and last error, "read again" (optionally with new
  secrets), unlinking (refused while mowers use it) and the form to link another one, rendered
  from the providers the Device-Gateway has enabled (disabled ones are not offered). Needs
  `can_manage_mowers` on the account; a 403 shows a calm notice instead of an error.
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
    listProviders,
    removeLinkedAccount,
    syncAccount,
  } from '#lib/linked/api.js';
  import { byId, type CredentialValues, type ProviderDescriptor } from '#lib/linked/providers.js';
  import { m } from '#lib/paraglide/messages.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  let accounts = $state<LinkedAccount[] | null>(null);
  let providers = $state<ProviderDescriptor[]>([]);
  let problem = $state<ApiProblem | null>(null);
  let providerProblem = $state<ApiProblem | null>(null);

  const forbidden = $derived(problem?.status === 403);
  const providerOf = $derived(byId(providers));

  async function load(): Promise<void> {
    const [listed, offered] = await Promise.allSettled([listLinkedAccounts(), listProviders()]);
    if (listed.status === 'fulfilled') {
      accounts = listed.value;
      problem = null;
    } else {
      problem = asProblem(listed.reason);
    }
    if (offered.status === 'fulfilled') {
      providers = offered.value;
      providerProblem = null;
    } else {
      providerProblem = asProblem(offered.reason);
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

  async function connect(provider: string, credentials: CredentialValues): Promise<void> {
    const linked = await linkAccount(provider, credentials);
    put(linked);
    toasts.push('ok', m.linked_connected({ email: linked.identity }));
  }

  async function sync(account: LinkedAccount, credentials?: CredentialValues): Promise<void> {
    try {
      put(await syncAccount(account.id, credentials));
      toasts.push('ok', m.linked_synced({ email: account.identity }));
    } catch (error) {
      // The failed read is recorded on the account (`last_sync_error`): show it.
      void load();
      throw error;
    }
  }

  async function remove(account: LinkedAccount): Promise<void> {
    await removeLinkedAccount(account.id);
    accounts = (accounts ?? []).filter((a) => a.id !== account.id);
    toasts.push('ok', m.linked_deleted({ email: account.identity }));
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
          provider={providerOf.get(account.provider) ?? null}
          onsync={(credentials) => sync(account, credentials)}
          onremove={() => remove(account)}
        />
      {/each}
    {/if}
  </section>

  <Panel label={m.linked_connect_title()} testid="linked-connect-panel">
    <div class="connect">
      {#if providerProblem}
        <ProblemNotice
          title={problemMessage(providerProblem)}
          detail={providerProblem.detail}
          reference={providerProblem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      {:else}
        <LinkedAccountConnect {providers} onconnect={connect} />
      {/if}
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
