<!--
  One linked vendor account on the settings page (ADR 0034, ADR 0065): the vendor and the
  account's identity, its status (connected, needs attention, provider not enabled), the
  non-secret fields it was linked with, device and mower counts, the last read and (prominently)
  why it failed, "read again" (optionally with new secret fields: a re-authentication, rendered
  from the provider's descriptor and never prefilled) and unlinking with a confirmation; a 409
  names the mowers that still use it. `onsync` and `onremove` do the requests and throw the
  ApiProblem.
-->
<script lang="ts">
  import { Button, Field, Icon, ProblemNotice } from '@mowdeck/ui';

  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { formatDateTime } from '#lib/i18n.js';
  import { accountIssue, inUseMowers, type LinkedAccount } from '#lib/linked/accounts.js';
  import {
    credentialIssue,
    credentialsBody,
    detailFields,
    formErrors,
    inputId,
    secretFields,
    type CredentialIssue,
    type CredentialValues,
    type ProviderDescriptor,
  } from '#lib/linked/providers.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    account: LinkedAccount;
    /** The account's provider; null when it is no longer enabled in the Device-Gateway. */
    provider?: ProviderDescriptor | null;
    onsync: (credentials?: CredentialValues) => Promise<void>;
    onremove: () => Promise<void>;
    /** Without `can_manage_mowers` nothing can be changed. */
    readonly?: boolean;
  }
  let { account, provider = null, onsync, onremove, readonly = false }: Props = $props();

  let syncing = $state(false);
  let reauth = $state(false);
  let secrets = $state<CredentialValues>({});
  let syncIssue = $state<CredentialIssue | null>(null);
  let confirming = $state(false);
  let removing = $state(false);
  let removeProblem = $state<ApiProblem | null>(null);

  const inUse = $derived(removeProblem ? inUseMowers(removeProblem) : []);
  const key = $derived(account.id);
  const identity = $derived(account.identity ?? account.email);
  const status = $derived<'ok' | 'failing' | 'disabled'>(
    !provider ? 'disabled' : account.last_sync_error ? 'failing' : 'ok',
  );
  const statusText = $derived(
    status === 'ok'
      ? m.provider_status_ok()
      : status === 'failing'
        ? m.provider_status_failing()
        : m.provider_status_disabled(),
  );
  const details = $derived(
    provider
      ? detailFields(provider).flatMap((field) => {
          const value = account.public_credentials?.[field.key];
          const label = field.choices?.find((c) => c.value === value)?.label ?? value;
          return label ? [{ key: field.key, label: field.label, value: label }] : [];
        })
      : [],
  );
  const errors = $derived<Record<string, string>>({
    ...(provider && reauth ? formErrors(provider, secrets, { onlySecrets: true }) : {}),
    ...(syncIssue?.fields ?? {}),
  });

  /** Every secret field empty (bound inputs need a string, never undefined). */
  function clearSecrets(): void {
    secrets = Object.fromEntries((provider ? secretFields(provider) : []).map((f) => [f.key, '']));
  }

  async function sync(): Promise<void> {
    if (!provider) return;
    syncIssue = null;
    if (reauth && Object.keys(formErrors(provider, secrets, { onlySecrets: true })).length > 0) {
      return;
    }
    syncing = true;
    try {
      const body = reauth ? credentialsBody(provider, secrets, { onlySecrets: true }) : {};
      await onsync(Object.keys(body).length > 0 ? body : undefined);
      reauth = false;
    } catch (error) {
      syncIssue = credentialIssue(asProblem(error), provider);
      if (!reauth && Object.keys(syncIssue.fields).length > 0) {
        // A refused stored secret: say so above the buttons, the form stays closed.
        syncIssue = { fields: {}, notice: m.linked_login_failed(), detail: null };
      }
    } finally {
      clearSecrets();
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
  class:failing={status === 'failing'}
  class:disabled={status === 'disabled'}
  data-testid="linked-account"
  data-email={identity}
  data-provider={account.provider}
  aria-labelledby="linked-account-{key}-email"
>
  <header>
    <div class="who">
      <span class="provider">{provider?.name ?? account.provider}</span>
      <h3 id="linked-account-{key}-email">{identity}</h3>
    </div>
    <span class="status" data-status={status} data-testid="linked-account-status">
      <span class="dot" aria-hidden="true"></span>{statusText}
    </span>
  </header>

  {#if provider && !provider.tested_on_hardware}
    <p class="untested" data-testid="linked-account-untested">
      <Icon name="alert" size={14} />{m.provider_untested()}
    </p>
  {/if}

  <dl class="stats">
    {#each details as detail (detail.key)}
      <div>
        <dt>{detail.label}</dt>
        <dd>{detail.value}</dd>
      </div>
    {/each}
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
    {#if reauth && provider}
      <fieldset class="reauth" data-testid="linked-account-reauth">
        <legend>{m.linked_new_password()}</legend>
        <p class="note">{m.linked_new_password_hint()}</p>
        <div class="secrets">
          {#each secretFields(provider) as field (field.key)}
            <Field
              id={inputId(`linked-account-${key}`, field)}
              type="password"
              label={field.label}
              autocomplete="new-password"
              disabled={syncing}
              hint={m.provider_keep_hint()}
              maxlength={field.max_length}
              error={errors[field.key] ?? null}
              bind:value={secrets[field.key]}
            />
          {/each}
        </div>
      </fieldset>
    {/if}
    {#if syncIssue?.notice}
      <ProblemNotice title={syncIssue.notice} detail={syncIssue.detail} />
    {/if}
    {#if syncing && provider}
      <p class="pending" role="status">
        {m.linked_connecting({ provider: provider.name })}
        {m.linked_connecting_hint()}
      </p>
    {/if}

    <div class="actions">
      <Button
        icon="signal"
        pending={syncing}
        disabled={!provider}
        onclick={sync}
        testid="linked-account-sync">{m.linked_sync()}</Button
      >
      <Button
        variant="ghost"
        icon="key"
        expanded={reauth}
        disabled={syncing || !provider}
        onclick={() => {
          reauth = !reauth;
          syncIssue = null;
          clearSecrets();
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

  .account.disabled {
    border-left-color: var(--md-state-offline);
  }

  header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--md-space-3);
  }

  .who {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .provider,
  dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .provider {
    font-family: var(--md-font-mono);
  }

  h3 {
    margin: 0;
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  .status {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    gap: var(--md-space-2);
    padding: var(--md-space-1) var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
  }

  .dot {
    width: 8px;
    height: 8px;
    border-radius: var(--md-radius-round);
    background: var(--md-state-mowing);
  }

  .status[data-status='failing'] .dot {
    background: var(--md-state-error);
  }

  .status[data-status='disabled'] .dot {
    background: var(--md-state-offline);
  }

  .untested {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    align-self: flex-start;
    margin: 0;
    padding: var(--md-space-1) var(--md-space-2);
    background: var(--md-state-paused-bg);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
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
    font-weight: var(--md-weight-semibold);
  }

  dd {
    margin: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-16);
    overflow-wrap: anywhere;
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

  .reauth {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
    margin: 0;
    padding: var(--md-space-3);
    border: var(--md-hairline) dashed var(--md-color-line-strong);
  }

  legend {
    padding: 0 var(--md-space-1);
    font-weight: var(--md-weight-semibold);
  }

  .secrets {
    display: grid;
    gap: var(--md-space-3);
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
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    }

    .stats .wide {
      grid-column: auto;
    }
  }
</style>
