<!--
  Link a vendor cloud account (ADR 0066). The form is rendered from the provider descriptors of
  Fleet's `GET /linked-accounts/providers`: a vendor rail when there are several, the chosen
  vendor's guidance (what MowDeck can do, the risks the provider warns of, where the credentials
  come from, whether a developer account is needed, whether it was ever tested on real hardware)
  and exactly its fields. The
  login at the vendor can take close to a minute, so the pending state says so calmly. A refused
  login lands on the first secret field, "already linked" on the identity, an unreachable cloud
  above the form. `onconnect` does the request and throws the ApiProblem; secrets are cleared
  after every attempt and never kept.
-->
<script lang="ts">
  import { Button, Field, Icon, ProblemNotice, Select } from '@mowdeck/ui';
  import { tick } from 'svelte';

  import { asProblem } from '#lib/api/problem.js';
  import {
    autocompleteOf,
    credentialIssue,
    credentialsBody,
    formErrors,
    initialValues,
    inputId,
    isSecret,
    type CredentialIssue,
    type CredentialValues,
    type ProviderDescriptor,
  } from '#lib/linked/providers.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    providers: ProviderDescriptor[];
    onconnect: (provider: string, credentials: CredentialValues) => Promise<void>;
    oncancel?: () => void;
    /** Prefix of the input ids (two forms on one page stay distinct). */
    idPrefix?: string;
  }
  let { providers, onconnect, oncancel, idPrefix = 'linked' }: Props = $props();

  let chosen = $state<string | null>(null);
  const provider = $derived(providers.find((p) => p.provider === chosen) ?? providers[0] ?? null);
  /** The provider's own warnings (an unofficial API, a possible account block), shown first. */
  const warnings = $derived(provider?.warnings ?? []);
  let values = $state<CredentialValues>({});
  let tried = $state(false);
  let pending = $state(false);
  let issue = $state<CredentialIssue | null>(null);
  let correlation = $state<string | null>(null);

  // A new vendor starts with its own empty form.
  $effect.pre(() => {
    if (provider) {
      values = initialValues(provider);
      tried = false;
      issue = null;
    }
  });

  const errors = $derived.by<Record<string, string>>(() => {
    const local = provider && tried ? formErrors(provider, values) : {};
    return { ...local, ...(issue?.fields ?? {}) };
  });

  async function focusInvalid(): Promise<void> {
    await tick();
    document
      .querySelector<HTMLElement>(`[data-testid="linked-connect"] [aria-invalid="true"]`)
      ?.focus();
  }

  function clearSecrets(): void {
    if (!provider) return;
    for (const field of provider.fields) if (isSecret(field)) values[field.key] = '';
  }

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!provider) return;
    tried = true;
    issue = null;
    correlation = null;
    if (Object.keys(formErrors(provider, values)).length > 0) {
      await focusInvalid();
      return;
    }
    pending = true;
    try {
      await onconnect(provider.provider, credentialsBody(provider, values));
      values = initialValues(provider);
      tried = false;
    } catch (error) {
      const problem = asProblem(error);
      issue = credentialIssue(problem, provider);
      correlation = problem.correlationId;
    } finally {
      clearSecrets();
      pending = false;
    }
    // After `pending`: disabled inputs cannot take the focus.
    if (issue) await focusInvalid();
  }

  function pick(id: string): void {
    if (!pending) chosen = id;
  }

  const STEPS: Record<string, number> = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };

  function onRailKey(event: KeyboardEvent, index: number): void {
    const step = STEPS[event.key];
    if (!step) return;
    event.preventDefault();
    const next = providers[(index + step + providers.length) % providers.length];
    if (!next) return;
    pick(next.provider);
    void tick().then(() =>
      document.getElementById(`${idPrefix}-provider-${next.provider}`)?.focus(),
    );
  }
</script>

{#if !provider}
  <div data-testid="linked-no-providers">
    <ProblemNotice tone="info" title={m.provider_none()} />
  </div>
{:else}
  <form class="connect" data-testid="linked-connect" onsubmit={submit} novalidate>
    {#if providers.length > 1}
      <div class="rail" role="radiogroup" aria-label={m.provider_pick_label()}>
        {#each providers as option, index (option.provider)}
          {@const selected = option.provider === provider.provider}
          <button
            type="button"
            role="radio"
            id="{idPrefix}-provider-{option.provider}"
            class="tile"
            class:selected
            aria-checked={selected}
            tabindex={selected ? 0 : -1}
            disabled={pending}
            data-testid="provider-option-{option.provider}"
            onclick={() => pick(option.provider)}
            onkeydown={(event) => onRailKey(event, index)}
          >
            <span class="tile-name">{option.name}</span>
            {#if !option.tested_on_hardware}
              <span class="flag" title={m.provider_untested()}
                ><Icon name="alert" size={14} /><span class="sr">{m.provider_untested()}</span
                ></span
              >
            {/if}
          </button>
        {/each}
      </div>
    {/if}

    <section class="guide" data-testid="provider-guide" aria-label={provider.name}>
      <header>
        <span class="vendor">{provider.provider}</span>
        <h3>{provider.name}</h3>
      </header>
      {#if provider.summary}<p class="summary">{provider.summary}</p>{/if}
      {#if !provider.tested_on_hardware || provider.needs_developer_account || warnings.length > 0}
        <ul class="badges">
          {#each warnings as warning, index (index)}
            <li class="badge warn" data-testid="provider-warning">
              <Icon name="alert" size={16} />
              <span>{warning}</span>
            </li>
          {/each}
          {#if !provider.tested_on_hardware}
            <li class="badge warn" data-testid="provider-untested">
              <Icon name="alert" size={16} />
              <span><strong>{m.provider_untested()}.</strong> {m.provider_untested_hint()}</span>
            </li>
          {/if}
          {#if provider.needs_developer_account}
            <li class="badge" data-testid="provider-dev-account">
              <Icon name="key" size={16} />
              <span
                ><strong>{m.provider_dev_account()}.</strong> {m.provider_dev_account_hint()}</span
              >
            </li>
          {/if}
        </ul>
      {/if}
      {#if (provider.setup_steps ?? []).length > 0}
        <div class="steps">
          <span class="vendor">{m.provider_steps_title()}</span>
          <ol>
            {#each provider.setup_steps ?? [] as step, index (index)}
              <li>
                <span class="n" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span><span
                  >{step}</span
                >
              </li>
            {/each}
          </ol>
        </div>
      {/if}
      {#if (provider.links ?? []).length > 0}
        <ul class="links">
          {#each provider.links ?? [] as link (link.url)}
            <li>
              <a href={link.url} target="_blank" rel="noopener noreferrer external"
                >{link.label}<span aria-hidden="true"> ↗</span></a
              >
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <div class="fields">
      {#each provider.fields as field (field.key)}
        {#if field.kind === 'choice'}
          <Select
            id={inputId(idPrefix, field)}
            label={field.label}
            hint={field.help ?? undefined}
            options={(field.choices ?? []).map((c) => ({ value: c.value, label: c.label }))}
            bind:value={values[field.key]}
          />
        {:else}
          <Field
            id={inputId(idPrefix, field)}
            type={isSecret(field) ? 'password' : field.kind === 'email' ? 'email' : 'text'}
            label={field.label}
            autocomplete={autocompleteOf(field)}
            required={field.required !== false}
            disabled={pending}
            placeholder={field.placeholder ?? undefined}
            hint={field.help ?? undefined}
            mono={field.kind === 'text'}
            maxlength={field.max_length}
            error={errors[field.key] ?? null}
            bind:value={values[field.key]}
          />
        {/if}
      {/each}
    </div>

    {#if issue?.notice}
      <div data-testid="linked-connect-problem">
        <ProblemNotice
          title={issue.notice}
          detail={issue.detail}
          reference={correlation}
          referenceLabel={m.problem_reference()}
        />
      </div>
    {/if}
    {#if pending}
      <div class="pending" role="status" data-testid="linked-connect-pending">
        <span class="pulse" aria-hidden="true"></span>
        <span class="what">{m.linked_connecting({ provider: provider.name })}</span>
        <span class="calm">{m.linked_connecting_hint()}</span>
      </div>
    {/if}
    <div class="actions">
      <Button type="submit" variant="primary" icon="link" {pending} testid="linked-connect-submit"
        >{m.linked_connect()}</Button
      >
      {#if oncancel && !pending}
        <Button variant="ghost" onclick={oncancel}>{m.cancel()}</Button>
      {/if}
    </div>
  </form>
{/if}

<style>
  .connect {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-4);
  }

  /* The vendor rail: hairline tiles like the instrument panel's readouts. */
  .rail {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: var(--md-hairline);
    border: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-line);
  }

  .tile {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
    min-height: 48px;
    padding: var(--md-space-3);
    border: 0;
    border-left: 3px solid transparent;
    background: var(--md-color-surface-sunken);
    color: var(--md-color-ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .tile:hover:not(:disabled) {
    background: var(--md-color-surface);
  }

  .tile.selected {
    border-left-color: var(--md-color-ink);
    background: var(--md-color-surface);
  }

  .tile:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .tile-name {
    font-weight: var(--md-weight-semibold);
  }

  .flag {
    display: inline-flex;
    padding: 2px;
    background: var(--md-state-paused-bg);
    color: var(--md-state-paused);
  }

  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .guide {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
    padding: var(--md-space-4);
    border: var(--md-hairline) solid var(--md-color-line);
    border-left: 3px solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
  }

  .guide header,
  .steps {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
  }

  .vendor {
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
  }

  .summary {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .badges,
  .links,
  ol {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .badges,
  ol {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
  }

  .badge {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--md-space-2);
    align-items: start;
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 4px solid var(--md-color-line-strong);
    background: var(--md-color-surface);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .badge.warn {
    border-inline-start-color: var(--md-state-paused);
    background: var(--md-state-paused-bg);
  }

  ol li {
    display: grid;
    grid-template-columns: 2.5ch 1fr;
    gap: var(--md-space-2);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .n {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
  }

  .links {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2) var(--md-space-4);
  }

  .links a {
    color: var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
    text-underline-offset: 3px;
  }

  .fields {
    display: grid;
    gap: var(--md-space-4);
  }

  .pending {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: center;
    gap: var(--md-space-1) var(--md-space-3);
    padding: var(--md-space-3);
    border: var(--md-hairline) dashed var(--md-color-line-strong);
    background: var(--md-color-surface-sunken);
  }

  .pulse {
    grid-row: span 2;
    width: var(--md-space-3);
    height: var(--md-space-3);
    border-radius: var(--md-radius-round);
    background: var(--md-color-ink);
    animation: breathe 1.6s var(--md-ease) infinite;
  }

  .what {
    font-weight: var(--md-weight-semibold);
  }

  .calm {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  @keyframes breathe {
    0%,
    100% {
      opacity: 0.25;
      transform: scale(0.8);
    }

    50% {
      opacity: 1;
      transform: scale(1);
    }
  }

  @media (min-width: 768px) {
    .fields {
      grid-template-columns: 1fr 1fr;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .pulse {
      animation: none;
    }
  }
</style>
