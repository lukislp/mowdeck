<!--
  API tokens of the account: the list (each token's rights as chips) and the form for a new one.
  Rights come from a preset (view only, Home Assistant basics or full) or a custom selection from
  Identity's permission catalogue; every token is granted on the whole account
  (`lib/settings/token-permissions.ts`).
-->
<script lang="ts">
  import { Button, Field, Icon, Panel, ProblemNotice } from '@mowdeck/ui';

  import { identityApi, unwrap } from '$lib/api/client';
  import { asProblem, type ApiProblem } from '$lib/api/problem';
  import { session } from '$lib/auth/session.svelte';
  import type { components } from '$lib/generated/identity';
  import { formatDateTime, problemMessage } from '$lib/i18n';
  import { m } from '$lib/paraglide/messages';
  import {
    DEFAULT_CHOICE,
    POWERFUL,
    PRESETS,
    VIEW_KEY,
    choiceKeys,
    displayOrder,
    impliedBy,
    needsView,
    permissionDescription,
    permissionShort,
    powerfulWarning,
    requestPermissions,
    toggle,
    tokenBody,
    tokenGrantable,
    type CatalogueEntry,
    type Choice,
  } from '$lib/settings/token-permissions';

  type Token = components['schemas']['ApiTokenOut'];

  const accountId = $derived(session.me?.account_id ?? '');
  let tokens = $state<Token[] | null>(null);
  let catalogue = $state<CatalogueEntry[] | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let name = $state('');
  let choice = $state<Choice>(DEFAULT_CHOICE);
  let custom = $state<string[]>(choiceKeys(DEFAULT_CHOICE, []));
  let secret = $state<string | null>(null);
  let copied = $state(false);
  let busy = $state(false);

  const choices = $derived<{ value: Choice; label: string; hint: string }[]>([
    { value: 'view', label: m.tokens_preset_view(), hint: m.tokens_preset_view_hint() },
    { value: 'basic', label: m.tokens_preset_basic(), hint: m.tokens_preset_basic_hint() },
    { value: 'full', label: m.tokens_preset_full(), hint: m.tokens_preset_full_hint() },
    { value: 'custom', label: m.tokens_preset_custom(), hint: m.tokens_preset_custom_hint() },
  ]);

  const grantable = $derived(catalogue ? tokenGrantable(catalogue) : []);
  const rights = $derived(grantable.filter((entry) => entry.in_model));
  const accountRights = $derived(grantable.filter((entry) => !entry.in_model));
  const entries = $derived(new Map(grantable.map((entry) => [entry.key, entry])));
  const chosen = $derived(choiceKeys(choice, custom));
  const implied = $derived(impliedBy(chosen, grantable));
  const permissions = $derived(requestPermissions(chosen, grantable));
  const warnings = $derived(chosen.filter((key) => POWERFUL.has(key)));
  const viewLocked = $derived(custom.some(needsView));

  async function load(): Promise<void> {
    if (!accountId) return;
    try {
      tokens = await unwrap(
        identityApi.GET('/api/v1/identity/accounts/{account_id}/api-tokens', {
          params: { path: { account_id: accountId } },
        }),
      );
    } catch (error) {
      problem = asProblem(error);
    }
  }

  async function loadCatalogue(): Promise<void> {
    if (!accountId) return;
    try {
      catalogue = await unwrap(
        identityApi.GET('/api/v1/identity/accounts/{account_id}/permissions', {
          params: { path: { account_id: accountId } },
        }),
      );
    } catch (error) {
      problem = asProblem(error);
    }
  }

  $effect(() => {
    void load();
    void loadCatalogue();
  });

  function pick(next: Choice): void {
    // A custom selection starts from the preset the owner looked at last.
    if (next === 'custom' && choice !== 'custom') custom = choiceKeys(choice, custom);
    choice = next;
  }

  async function create(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!name.trim() || permissions.length === 0) return;
    busy = true;
    problem = null;
    try {
      const created = await unwrap(
        identityApi.POST('/api/v1/identity/accounts/{account_id}/api-tokens', {
          params: { path: { account_id: accountId } },
          body: tokenBody(name, accountId, permissions),
        }),
      );
      secret = created.token ?? null;
      copied = false;
      name = '';
      await load();
    } catch (error) {
      problem = asProblem(error);
    } finally {
      busy = false;
    }
  }

  async function revoke(tokenId: string): Promise<void> {
    try {
      await unwrap(
        identityApi.DELETE('/api/v1/identity/accounts/{account_id}/api-tokens/{token_id}', {
          params: { path: { account_id: accountId, token_id: tokenId } },
        }),
      );
      await load();
    } catch (error) {
      problem = asProblem(error);
    }
  }

  async function copy(): Promise<void> {
    if (!secret) return;
    await navigator.clipboard.writeText(secret);
    copied = true;
  }

  /** Account keys the token holds through its own keys (shown as "+N included"). */
  function includedOf(token: Token): string[] {
    if (!catalogue) return [];
    const own = new Set(token.permissions);
    return [...impliedBy(token.permissions, catalogue).keys()].filter((key) => !own.has(key));
  }
</script>

{#snippet chip(key: string)}
  <li class="chip" class:powerful={POWERFUL.has(key)} data-key={key} title={key}>
    {#if POWERFUL.has(key)}<Icon name="bolt" size={12} />{/if}{permissionShort(key)}
  </li>
{/snippet}

{#snippet right(entry: CatalogueEntry)}
  {@const via = implied.get(entry.key)}
  {@const needed = entry.key === VIEW_KEY && viewLocked}
  {@const warning = powerfulWarning(entry.key)}
  <label class="right" class:locked={!!via || needed} data-key={entry.key}>
    <input
      type="checkbox"
      data-testid="token-perm-{entry.key}"
      checked={custom.includes(entry.key) || !!via}
      disabled={!!via || needed}
      onchange={(event) => (custom = toggle(custom, entry.key, event.currentTarget.checked))}
    />
    <span class="right-text">
      <span class="right-head">
        <span class="right-name">{permissionShort(entry.key)}</span>
        <code class="right-key">{entry.key}</code>
        {#if warning}<span class="badge"><Icon name="bolt" size={12} />{m.tokens_powerful()}</span
          >{/if}
      </span>
      <span class="right-desc">{permissionDescription(entry.key, entries.get(entry.key))}</span>
      {#if warning}<span class="right-warn">{warning}</span>{/if}
      {#if via}
        <span class="right-note">{m.tokens_included_via({ keys: via.join(', ') })}</span>
      {:else if needed}
        <span class="right-note">{m.tokens_needed_by()}</span>
      {/if}
    </span>
  </label>
{/snippet}

{#if secret}
  <section class="secret" data-testid="token-secret">
    <h2>{m.tokens_secret_title()}</h2>
    <p>{m.tokens_secret_once()}</p>
    <code>{secret}</code>
    <div class="row">
      <Button icon={copied ? 'check' : 'copy'} onclick={copy}
        >{copied ? m.copied() : m.copy()}</Button
      >
      <Button variant="ghost" onclick={() => (secret = null)}>{m.close()}</Button>
    </div>
  </section>
{/if}

{#if problem}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      detail={problem.detail}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{/if}

{#if tokens && tokens.length === 0}
  <p class="pad quiet">{m.tokens_empty()}</p>
{:else if tokens}
  <ul class="tokens" data-testid="token-list">
    {#each tokens as token (token.id)}
      {@const included = includedOf(token)}
      <li class:revoked={!!token.revoked_at} data-testid="token-row" data-name={token.name}>
        <span class="who">
          <span class="name">{token.name}</span>
          <span class="mono">{token.prefix}…</span>
          <ul class="chips" aria-label={m.tokens_permissions_label()} data-testid="token-chips">
            {#each displayOrder(token.permissions) as key (key)}
              {@render chip(key)}
            {/each}
            {#if included.length}
              <li class="chip more" title={included.join(', ')} data-testid="token-included">
                {m.tokens_included_more({ count: included.length })}
              </li>
            {/if}
          </ul>
        </span>
        <span class="meta mono">
          {m.tokens_created()}
          {formatDateTime(token.created_at)}<br />
          {m.tokens_last_used()}
          {token.last_used_at ? formatDateTime(token.last_used_at) : m.never()}
        </span>
        {#if token.revoked_at}
          <span class="flag">{m.tokens_revoked()}</span>
        {:else}
          <Button variant="danger" onclick={() => revoke(token.id)}>{m.tokens_revoke()}</Button>
        {/if}
      </li>
    {/each}
  </ul>
{/if}

<Panel label={m.tokens_create_title()}>
  <form class="form" onsubmit={create} novalidate>
    <Field id="token-name" label={m.tokens_name()} required maxlength={100} bind:value={name} />

    <fieldset class="presets" data-testid="token-presets">
      <legend class="label">{m.tokens_preset()}</legend>
      {#each choices as option (option.value)}
        <label class="preset" class:chosen={choice === option.value}>
          <input
            type="radio"
            name="token-preset"
            value={option.value}
            data-testid="token-preset-{option.value}"
            checked={choice === option.value}
            onchange={() => pick(option.value)}
          />
          <span class="preset-name">{option.label}</span>
          <span class="preset-hint">{option.hint}</span>
          {#if option.value !== 'custom'}
            <ul class="chips">
              {#each PRESETS[option.value] as key (key)}
                {@render chip(key)}
              {/each}
            </ul>
          {/if}
        </label>
      {/each}
    </fieldset>
    <p class="hint" data-testid="token-preset-difference">
      {m.tokens_preset_difference()}
      {m.tokens_account_wide()}
    </p>

    {#if choice === 'custom'}
      <div class="custom" data-testid="token-custom">
        {#if !catalogue}
          <p class="quiet">{m.tokens_catalogue_loading()}</p>
        {:else}
          <fieldset class="rights">
            <legend class="label">{m.tokens_custom_rights()}</legend>
            {#each rights as entry (entry.key)}
              {@render right(entry)}
            {/each}
          </fieldset>
          {#if accountRights.length}
            <fieldset class="rights">
              <legend class="label">{m.tokens_custom_account()}</legend>
              {#each accountRights as entry (entry.key)}
                {@render right(entry)}
              {/each}
            </fieldset>
          {/if}
        {/if}
      </div>
    {/if}

    {#if warnings.length}
      <div class="warning" role="note" data-testid="token-warning">
        <Icon name="bolt" size={20} />
        <div>
          <strong>{m.tokens_powerful_title()}</strong>
          <ul>
            {#each warnings as key (key)}
              <li><code>{key}</code> {powerfulWarning(key)}</li>
            {/each}
          </ul>
        </div>
      </div>
    {/if}

    {#if permissions.length === 0}
      <p class="none" data-testid="token-none">{m.tokens_none_selected()}</p>
    {/if}

    <div>
      <Button
        type="submit"
        variant="primary"
        icon="key"
        pending={busy}
        disabled={!name.trim() || permissions.length === 0}
        testid="token-create">{m.tokens_create()}</Button
      >
    </div>
  </form>
</Panel>

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .secret {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    margin: var(--md-space-4) var(--md-gutter);
    padding: var(--md-space-4);
    border: var(--md-border-control) solid var(--md-color-ink);
    background: var(--md-color-surface);
  }

  .secret code {
    padding: var(--md-space-3);
    background: var(--md-color-surface-sunken);
    overflow-wrap: anywhere;
    user-select: all;
  }

  .row {
    display: flex;
    gap: var(--md-space-2);
  }

  .tokens {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .tokens > li {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--md-space-2) var(--md-space-3);
    padding: var(--md-space-3) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .tokens > li.revoked {
    background: var(--md-color-bg);
    color: var(--md-color-ink-muted);
  }

  .tokens > li.revoked .chips {
    opacity: 0.6;
  }

  .who {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .name {
    font-weight: var(--md-weight-semibold);
  }

  .mono {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    overflow-wrap: anywhere;
  }

  .meta {
    grid-column: 1;
  }

  .flag {
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: 24px;
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-medium);
    white-space: nowrap;
  }

  .chip.powerful {
    border-color: var(--md-state-paused);
    background: var(--md-state-paused-bg);
    color: var(--md-state-paused);
  }

  .chip.more {
    border-style: dashed;
    color: var(--md-color-ink-muted);
  }

  .form {
    display: grid;
    gap: var(--md-space-4);
    max-width: 720px;
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .presets {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    border: 0;
  }

  .presets legend,
  .rights legend {
    margin-bottom: var(--md-space-2);
    padding: 0;
  }

  .preset {
    position: relative;
    display: grid;
    align-content: start;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
    cursor: pointer;
  }

  .preset.chosen {
    border-color: var(--md-color-ink);
    box-shadow: inset 0 0 0 var(--md-hairline) var(--md-color-ink);
  }

  .preset input {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    opacity: 0;
    cursor: pointer;
  }

  .preset:focus-within {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .preset-name {
    font-weight: var(--md-weight-semibold);
  }

  .preset-hint,
  .hint {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .hint {
    margin: calc(-1 * var(--md-space-2)) 0 0;
  }

  .custom {
    display: grid;
    gap: var(--md-space-4);
  }

  .rights {
    display: grid;
    margin: 0;
    padding: 0;
    border: 0;
  }

  .right {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--md-space-3);
    align-items: start;
    min-height: var(--md-target);
    padding: var(--md-space-3) 0;
    border-top: var(--md-hairline) solid var(--md-color-line);
    cursor: pointer;
  }

  .right.locked {
    cursor: default;
  }

  .right input {
    width: 20px;
    height: 20px;
    margin: 2px 0 0;
    accent-color: var(--md-color-ink);
  }

  .right-text {
    display: grid;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .right-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--md-space-1) var(--md-space-2);
  }

  .right-name {
    font-weight: var(--md-weight-semibold);
  }

  .right-key {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .right-desc {
    font-size: var(--md-font-size-13);
  }

  .right-warn {
    color: var(--md-state-paused);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-medium);
  }

  .right-note {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-style: italic;
  }

  .badge {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid var(--md-state-paused);
    background: var(--md-state-paused-bg);
    color: var(--md-state-paused);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .warning {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--md-space-3);
    padding: var(--md-space-3);
    border-left: var(--md-border-control) solid var(--md-state-paused);
    background: var(--md-state-paused-bg);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-13);
  }

  .warning :global(.md-icon) {
    color: var(--md-state-paused);
  }

  .warning ul {
    margin: var(--md-space-1) 0 0;
    padding: 0;
    list-style: none;
  }

  .warning code {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
  }

  .none {
    margin: 0;
    color: var(--md-state-error);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-medium);
  }
</style>
