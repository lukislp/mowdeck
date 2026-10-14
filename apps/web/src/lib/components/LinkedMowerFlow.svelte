<!--
  Adding mowers of a linked account (a connection kind with `linked_account`): pick a linked
  Mammotion account or link one, choose the mowers from its device list, give each a display
  name and a site (an existing one, the default site included, or one created in place), then
  add them all with one bulk request. Devices that fail stay selected with their reason.
-->
<script lang="ts">
  import { Button, Field, Panel, ProblemNotice, Select } from '@mowdeck/ui';
  import { tick } from 'svelte';

  import { goto } from '$app/navigation';

  import { asProblem, type ApiProblem } from '$lib/api/problem';
  import LinkedAccountConnect from '$lib/components/LinkedAccountConnect.svelte';
  import LinkedDeviceList from '$lib/components/LinkedDeviceList.svelte';
  import SlowLoading from '$lib/components/SlowLoading.svelte';
  import type { KindSchema } from '$lib/connection/form';
  import { kindDescription, kindTitle } from '$lib/connection/text';
  import { displaySites, type Site } from '$lib/fleet/data';
  import { sortSites } from '$lib/fleet/places';
  import { createSite, listSites } from '$lib/fleet/sites';
  import { formatDateTime, problemMessage } from '$lib/i18n';
  import {
    accountIssue,
    BULK_LIMIT,
    bulkBody,
    bulkOutcome,
    idPart,
    pruneSelection,
    syncDrafts,
    type AccountIssue,
    type LinkedAccount,
    type LinkedAccountDetail,
    type LinkedDevice,
    type MowerDraft,
  } from '$lib/linked/accounts';
  import {
    linkAccount,
    listDevices,
    listLinkedAccounts,
    registerMowers,
    syncAccount,
  } from '$lib/linked/api';
  import { m } from '$lib/paraglide/messages';
  import { notifyPlanProblem } from '$lib/plan/problems';
  import { toasts } from '$lib/stores/ui.svelte';

  interface Props {
    schema: KindSchema;
  }
  let { schema }: Props = $props();

  const NEW_SITE = '__new__';

  let accounts = $state<LinkedAccount[] | null>(null);
  let accountsProblem = $state<ApiProblem | null>(null);
  let accountId = $state<string | null>(null);
  let connecting = $state(false);

  let devices = $state<LinkedDevice[] | null>(null);
  let lastSyncedAt = $state<string | null>(null);
  let devicesProblem = $state<ApiProblem | null>(null);
  let syncing = $state(false);
  let syncIssue = $state<AccountIssue | null>(null);

  let selected = $state<string[]>([]);
  let drafts = $state<Record<string, MowerDraft>>({});
  let failures = $state<Record<string, string>>({});

  let sites = $state<Site[]>([]);
  /** Devices whose site select shows the "new site" input, with the typed name. */
  let newSite = $state<Record<string, string>>({});
  let newSiteError = $state<Record<string, string>>({});
  let creatingSite = $state<string | null>(null);

  let busy = $state(false);
  let problem = $state<ApiProblem | null>(null);

  const forbidden = $derived(accountsProblem?.status === 403);
  const defaultSiteId = $derived(sites.find((s) => s.is_default)?.id ?? null);
  const siteOptions = $derived([
    ...(sites.length > 0
      ? sites.map((s) => ({ value: s.id, label: s.name }))
      : [{ value: '', label: m.sites_default_name() }]),
    { value: NEW_SITE, label: m.linked_site_new_option() },
  ]);
  const account = $derived(accounts?.find((a) => a.id === accountId) ?? null);

  $effect(() => {
    void loadAccounts();
    void loadSites();
  });

  async function loadAccounts(): Promise<void> {
    try {
      const found = await listLinkedAccounts();
      accounts = found;
      accountsProblem = null;
      if (found.length === 0) connecting = true;
      else if (found.length === 1 && !accountId) await chooseAccount(found[0]!.id);
    } catch (error) {
      accountsProblem = asProblem(error);
    }
  }

  async function loadSites(): Promise<void> {
    try {
      sites = sortSites(displaySites(await listSites()));
    } catch {
      sites = []; // the default site is used (site_id left out)
    }
  }

  function showDevices(list: LinkedDevice[], syncedAt: string | null): void {
    devices = list;
    lastSyncedAt = syncedAt;
    setSelected(pruneSelection(selected, list));
  }

  async function chooseAccount(id: string): Promise<void> {
    if (id === accountId && devices) return;
    accountId = id;
    devices = null;
    devicesProblem = null;
    syncIssue = null;
    failures = {};
    setSelected([]);
    try {
      const list = await listDevices(id);
      if (accountId === id) showDevices(list.items, list.last_synced_at);
    } catch (error) {
      devicesProblem = asProblem(error);
    }
  }

  function upsert(next: LinkedAccount): void {
    const list = accounts ?? [];
    accounts = list.some((a) => a.id === next.id)
      ? list.map((a) => (a.id === next.id ? next : a))
      : [...list, next];
  }

  async function connect(email: string, password: string): Promise<void> {
    const linked: LinkedAccountDetail = await linkAccount(email, password);
    upsert(linked);
    connecting = false;
    accountId = linked.id;
    failures = {};
    devicesProblem = null;
    setSelected([]);
    showDevices(linked.devices, linked.last_synced_at);
    toasts.push('ok', m.linked_connected({ email: linked.email }));
    await tick();
    document.getElementById('linked-devices')?.focus();
  }

  async function resync(): Promise<void> {
    if (!accountId) return;
    syncing = true;
    syncIssue = null;
    try {
      const fresh = await syncAccount(accountId);
      upsert(fresh);
      showDevices(fresh.devices, fresh.last_synced_at);
    } catch (error) {
      syncIssue = accountIssue(asProblem(error));
      void listLinkedAccounts()
        .then((found) => (accounts = found))
        .catch(() => undefined);
    } finally {
      syncing = false;
    }
  }

  function setSelected(next: string[]): void {
    selected = next.slice(0, BULK_LIMIT);
    drafts = syncDrafts(selected, drafts, defaultSiteId);
  }

  function without<T>(record: Record<string, T>, key: string): Record<string, T> {
    const next = { ...record };
    delete next[key];
    return next;
  }

  function siteValue(device: string): string {
    if (device in newSite) return NEW_SITE;
    return drafts[device]?.siteId ?? (sites.length > 0 ? (defaultSiteId ?? '') : '');
  }

  function chooseSite(device: string, value: string): void {
    if (value === NEW_SITE) {
      newSite = { ...newSite, [device]: '' };
      return;
    }
    newSite = without(newSite, device);
    drafts = { ...drafts, [device]: { ...drafts[device]!, siteId: value || null } };
  }

  async function addSite(device: string): Promise<void> {
    const name = (newSite[device] ?? '').trim();
    newSiteError = without(newSiteError, device);
    if (!name) {
      newSiteError = { ...newSiteError, [device]: m.conn_error_required() };
      await tick();
      document.getElementById(`linked-site-new-${idPart(device)}`)?.focus();
      return;
    }
    creatingSite = device;
    try {
      const [site] = displaySites([await createSite({ name })]);
      sites = sortSites([...sites, site!]);
      newSite = without(newSite, device);
      drafts = { ...drafts, [device]: { ...drafts[device]!, siteId: site!.id } };
      toasts.push('ok', m.linked_site_created({ name: site!.name }));
      await tick();
      document.getElementById(`linked-site-${idPart(device)}`)?.focus();
    } catch (error) {
      const found = asProblem(error);
      notifyPlanProblem(found);
      newSiteError = {
        ...newSiteError,
        [device]: found.fieldError('name') ?? found.detail ?? problemMessage(found),
      };
    } finally {
      creatingSite = null;
    }
  }

  function rename(device: string, name: string): void {
    drafts = { ...drafts, [device]: { ...drafts[device]!, name } };
  }

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!accountId || selected.length === 0) return;
    problem = null;
    busy = true;
    try {
      const result = await registerMowers(accountId, bulkBody(selected, drafts));
      if (result.created.length > 0) {
        toasts.push('ok', m.linked_added({ count: result.created.length }));
      }
      if (result.failed.length === 0) {
        await goto('/mowers');
        return;
      }
      const outcome = bulkOutcome(result);
      failures = outcome.failures;
      setSelected(outcome.keep);
      // The added ones now show as "already added".
      const list = await listDevices(accountId).catch(() => null);
      if (list) {
        devices = list.items;
        lastSyncedAt = list.last_synced_at;
      }
      await tick();
      document.querySelector<HTMLElement>('[data-testid="linked-failed"]')?.focus();
    } catch (error) {
      problem = asProblem(error);
      notifyPlanProblem(problem);
    } finally {
      busy = false;
    }
  }

  function problemTitle(found: ApiProblem): string {
    return found.slug === 'mower-already-registered'
      ? m.linked_failure_registered()
      : problemMessage(found);
  }

  function deviceOf(name: string): LinkedDevice | undefined {
    return devices?.find((d) => d.device_name === name);
  }
</script>

<div class="flow" data-testid="linked-flow">
  <div class="plate">
    <span class="code">{schema.kind}</span>
    <span class="title">{kindTitle(schema)}</span>
    <p class="about">{kindDescription(schema)}</p>
  </div>

  <Panel label={m.linked_pick_title()} testid="linked-accounts-panel">
    {#if forbidden}
      <div data-testid="linked-forbidden">
        <ProblemNotice tone="info" title={m.linked_no_permission()} />
      </div>
    {:else if accountsProblem}
      <ProblemNotice
        title={problemMessage(accountsProblem)}
        detail={accountsProblem.detail}
        reference={accountsProblem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {:else if !accounts}
      <p class="quiet" role="status"><SlowLoading /></p>
    {:else}
      <div class="stack">
        {#if accounts.length > 0}
          <fieldset class="accounts">
            <legend>{m.linked_pick_label()}</legend>
            {#each accounts as option (option.id)}
              <label
                class="account"
                class:chosen={option.id === accountId}
                data-testid="linked-account-option"
                data-email={option.email}
              >
                <input
                  type="radio"
                  name="linked-account"
                  value={option.id}
                  checked={option.id === accountId}
                  onchange={() => void chooseAccount(option.id)}
                />
                <span class="who">
                  <span class="email">{option.email}</span>
                  <span class="facts">
                    <span
                      >{m.linked_last_sync()}
                      {option.last_synced_at
                        ? formatDateTime(option.last_synced_at)
                        : m.linked_never_synced()}</span
                    >
                    <span>{m.linked_devices_count()} {option.device_count}</span>
                  </span>
                  {#if option.last_sync_error}
                    <span class="warn">{m.linked_sync_error()}: {option.last_sync_error}</span>
                  {/if}
                </span>
              </label>
            {/each}
          </fieldset>
        {/if}

        {#if connecting}
          <div class="connect">
            <h3>{m.linked_connect_title()}</h3>
            <LinkedAccountConnect
              onconnect={connect}
              oncancel={accounts.length > 0 ? () => (connecting = false) : undefined}
            />
          </div>
        {:else}
          <div>
            <Button
              variant="ghost"
              icon="plus"
              onclick={() => (connecting = true)}
              testid="linked-connect-new">{m.linked_connect_another()}</Button
            >
          </div>
        {/if}
      </div>
    {/if}
  </Panel>

  {#if accountId && !connecting}
    <Panel label={m.linked_devices_title()} testid="linked-devices-panel">
      <div class="stack" id="linked-devices" tabindex="-1">
        {#if account?.last_sync_error && !syncIssue}
          <ProblemNotice title={m.linked_sync_error()} detail={account.last_sync_error} />
        {/if}
        {#if syncIssue}
          <div data-testid="linked-sync-problem">
            <ProblemNotice title={syncIssue.message} detail={syncIssue.detail} />
          </div>
        {/if}
        {#if devicesProblem}
          <ProblemNotice
            title={problemMessage(devicesProblem)}
            detail={devicesProblem.detail}
            reference={devicesProblem.correlationId}
            referenceLabel={m.problem_reference()}
          />
        {:else if !devices}
          <p class="quiet" role="status"><SlowLoading /></p>
        {:else}
          <LinkedDeviceList
            {devices}
            bind:selected={() => selected, setSelected}
            {lastSyncedAt}
            {syncing}
            onsync={resync}
            {failures}
            disabled={busy}
          />
        {/if}
      </div>
    </Panel>

    {#if devices}
      <form class="drafts-form" onsubmit={submit} novalidate>
        <Panel label={m.linked_drafts_title()} testid="linked-drafts">
          {#if selected.length === 0}
            <p class="quiet">{m.linked_drafts_empty()}</p>
          {:else}
            <ol class="drafts">
              {#each selected as device (device)}
                {@const key = idPart(device)}
                {@const info = deviceOf(device)}
                <li data-testid="linked-draft-{key}">
                  <span class="device">
                    <span class="mono">{device}</span>
                    {#if info?.model}<span class="model">{info.model}</span>{/if}
                  </span>
                  <div class="draft-fields">
                    <Field
                      id="linked-name-{key}"
                      label={m.linked_draft_name()}
                      maxlength={100}
                      disabled={busy}
                      bind:value={
                        () => drafts[device]?.name ?? '', (value) => rename(device, value)
                      }
                    />
                    <Select
                      id="linked-site-{key}"
                      label={m.linked_draft_site()}
                      options={siteOptions}
                      bind:value={() => siteValue(device), (value) => chooseSite(device, value)}
                    />
                  </div>
                  {#if device in newSite}
                    <div class="new-site">
                      <Field
                        id="linked-site-new-{key}"
                        label={m.linked_site_new_name()}
                        maxlength={100}
                        required
                        error={newSiteError[device] ?? null}
                        bind:value={newSite[device]}
                      />
                      <Button
                        icon="site"
                        pending={creatingSite === device}
                        onclick={() => void addSite(device)}
                        testid="linked-site-create-{key}">{m.linked_site_create()}</Button
                      >
                    </div>
                  {/if}
                </li>
              {/each}
            </ol>
          {/if}
        </Panel>

        <div class="actions">
          {#if Object.keys(failures).length > 0}
            <div class="failed" role="alert" tabindex="-1" data-testid="linked-failed">
              <span class="failed-title">{m.linked_failed_title()}</span>
              <ul>
                {#each Object.entries(failures) as [device, message] (device)}
                  <li><span class="mono">{device}</span> {message}</li>
                {/each}
              </ul>
            </div>
          {/if}
          {#if problem}
            <ProblemNotice
              title={problemTitle(problem)}
              detail={problem.detail}
              reference={problem.correlationId}
              referenceLabel={m.problem_reference()}
            />
          {/if}
          <Button
            type="submit"
            variant="primary"
            icon="plus"
            pending={busy}
            disabled={selected.length === 0 || creatingSite !== null}
            testid="linked-submit"
            >{m.linked_submit()}
            {#if selected.length > 0}<span class="mono">· {selected.length}</span>{/if}</Button
          >
        </div>
      </form>
    {/if}
  {/if}
</div>

<style>
  .flow {
    display: flex;
    flex-direction: column;
  }

  .plate {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: baseline;
    gap: var(--md-space-1) var(--md-space-3);
    margin: 0 var(--md-gutter) var(--md-space-4);
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line);
    border-left: 3px solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
  }

  .code {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .title {
    font-weight: var(--md-weight-semibold);
  }

  .about {
    grid-column: 1 / -1;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .stack {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-4);
  }

  #linked-devices:focus-visible {
    outline: var(--md-border-control) solid var(--md-color-focus);
    outline-offset: var(--md-space-1);
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .accounts {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    border: 0;
  }

  legend,
  h3 {
    margin: 0 0 var(--md-space-2);
    padding: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .account {
    display: grid;
    grid-template-columns: var(--md-target) minmax(0, 1fr);
    align-items: center;
    min-height: var(--md-target);
    padding: var(--md-space-2) var(--md-space-3) var(--md-space-2) 0;
    border: var(--md-hairline) solid var(--md-color-line);
    border-left: 3px solid var(--md-color-line);
    background: var(--md-color-surface);
    cursor: pointer;
  }

  .account.chosen {
    border-color: var(--md-color-line-strong);
    border-left-color: var(--md-color-ink);
  }

  .account input {
    justify-self: center;
    width: var(--md-space-6);
    height: var(--md-space-6);
    margin: 0;
    accent-color: var(--md-color-ink);
  }

  .account input:focus-visible {
    outline: var(--md-border-control) solid var(--md-color-focus);
    outline-offset: var(--md-space-1);
  }

  .who {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .email {
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  .facts {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .warn {
    color: var(--md-state-error);
    font-size: var(--md-font-size-13);
  }

  .connect {
    padding: var(--md-space-3);
    border: var(--md-hairline) dashed var(--md-color-line-strong);
  }

  .drafts {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-4);
    margin: 0;
    padding: 0;
    list-style: none;
    counter-reset: draft;
  }

  .drafts li {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
    padding-left: var(--md-space-3);
    border-left: 3px solid var(--md-color-ink);
    counter-increment: draft;
  }

  .device {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--md-space-2);
  }

  .device::before {
    content: counter(draft, decimal-leading-zero);
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .mono {
    font-family: var(--md-font-mono);
  }

  .device .mono {
    font-weight: var(--md-weight-semibold);
  }

  .model {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .draft-fields {
    display: grid;
    gap: var(--md-space-3);
  }

  .new-site {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--md-space-2);
    padding: var(--md-space-3);
    background: var(--md-color-surface-sunken);
  }

  .new-site > :global(:first-child) {
    flex: 1 1 200px;
  }

  .actions {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-3);
    padding: var(--md-space-4) var(--md-gutter) var(--md-space-8);
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .failed {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    align-self: stretch;
    padding: var(--md-space-3);
    border-inline-start: 4px solid var(--md-state-error);
    background: var(--md-state-error-bg);
  }

  .failed:focus-visible {
    outline: var(--md-border-control) solid var(--md-color-focus);
  }

  .failed-title {
    font-weight: var(--md-weight-semibold);
  }

  .failed ul {
    margin: 0;
    padding-left: var(--md-space-4);
    font-size: var(--md-font-size-14);
  }

  @media (min-width: 768px) {
    .draft-fields {
      grid-template-columns: 1fr 1fr;
    }
  }
</style>
