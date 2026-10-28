<!--
  API access (ADR 0064): the user's own personal access tokens for scripts. On top the way in
  (base URL of this installation, links to the scripting guide and the API reference), then the
  secret of a token just created with a ready quick start, the active tokens with their scopes and
  a lifetime bar, the revoked and expired ones folded away, and the form for a new one (curated
  scopes, a lifetime of 30 days to a year). Creating asks for a step-up through the API client
  (`#lib/auth/stepup.svelte.js`); the secret is never kept beyond this page.
-->
<script lang="ts">
  import { Button, Field, Icon, Panel, ProblemNotice } from '@mowdeck/ui';
  import { tick } from 'svelte';

  import { identityApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { formatDate, formatDateTime, problemMessage } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import {
    API_REFERENCE_URL,
    DEFAULT_LIFETIME,
    LIFETIMES,
    MAX_LIFETIME,
    SCOPES,
    SCRIPTING_GUIDE_URL,
    VIEW_SCOPE,
    apiBase,
    daysLeft,
    expiresSoon,
    isActive,
    lifeLeft,
    personalTokenBody,
    quickStart,
    scopeHint,
    scopeName,
    toggleScope,
    viewLocked,
    type PersonalToken,
    type Scope,
  } from '#lib/settings/personal-tokens.js';

  const PATH = '/api/v1/identity/me/api-tokens';
  const origin = typeof window === 'undefined' ? '' : window.location.origin;
  const base = apiBase(origin);
  const example = quickStart(origin);

  let tokens = $state<PersonalToken[] | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let now = $state(Date.now());
  let name = $state('');
  let scopes = $state<Scope[]>([VIEW_SCOPE]);
  let lifetime = $state<number>(DEFAULT_LIFETIME);
  let busy = $state(false);
  /** The one-time secret of the token created last; gone when dismissed or the page is left. */
  let created = $state<{ name: string; value: string } | null>(null);
  let copied = $state<'' | 'secret' | 'example' | 'base'>('');
  let note = $state('');
  let secretBox = $state<HTMLElement | null>(null);

  const sorted = $derived(
    [...(tokens ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at)),
  );
  const active = $derived(sorted.filter((token) => isActive(token, now)));
  const inactive = $derived(sorted.filter((token) => !isActive(token, now)));
  const locked = $derived(viewLocked(scopes));
  const until = $derived(formatDate(now + lifetime * 86_400_000));

  async function load(): Promise<void> {
    try {
      tokens = await unwrap(identityApi.GET(PATH));
      now = Date.now();
    } catch (error) {
      problem = asProblem(error);
    }
  }

  $effect(() => {
    void load();
  });

  async function create(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!name.trim() || scopes.length === 0) return;
    busy = true;
    problem = null;
    try {
      const answer = await unwrap(
        identityApi.POST(PATH, { body: personalTokenBody(name, scopes, lifetime) }),
      );
      created = answer.token ? { name: answer.name, value: answer.token } : null;
      copied = '';
      name = '';
      await load();
      if (created) {
        await tick();
        const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? true;
        secretBox?.scrollIntoView?.({ block: 'start', behavior: reduce ? 'auto' : 'smooth' });
        secretBox?.focus({ preventScroll: true });
      }
    } catch (error) {
      problem = asProblem(error);
    } finally {
      busy = false;
    }
  }

  async function revoke(token: PersonalToken): Promise<void> {
    problem = null;
    note = '';
    try {
      await unwrap(
        identityApi.DELETE(`${PATH}/{token_id}`, { params: { path: { token_id: token.id } } }),
      );
      note = m.pat_revoked_note({ name: token.name });
      await load();
    } catch (error) {
      problem = asProblem(error);
    }
  }

  function dismiss(): void {
    created = null;
    copied = '';
    document.getElementById('pat-name')?.focus();
  }

  async function copy(text: string, what: 'secret' | 'example' | 'base'): Promise<void> {
    try {
      // Missing outside a secure context, e.g. the app opened over plain HTTP in the LAN.
      if (!navigator.clipboard) throw new Error('no clipboard');
      await navigator.clipboard.writeText(text);
      copied = what;
    } catch {
      copied = '';
    }
  }

  function expiryLabel(token: PersonalToken): string {
    if (Date.parse(token.expires_at) <= now) {
      return m.pat_expired({ date: formatDate(token.expires_at) });
    }
    const days = daysLeft(token.expires_at, now);
    if (days <= 0) return m.pat_expires_today();
    if (days === 1) return m.pat_expires_in_one();
    if (days < 60) return m.pat_expires_in({ days });
    return m.pat_expires_on({ date: formatDate(token.expires_at) });
  }
</script>

{#snippet scopeChips(keys: readonly string[])}
  <ul class="chips" aria-label={m.pat_scopes()}>
    {#each keys as key (key)}
      <li class="chip" data-key={key} title={key}>{scopeName(key)}</li>
    {/each}
  </ul>
{/snippet}

<div class="pad intro">
  <p class="lead">{m.pat_intro()}</p>
  <div class="way-in" data-testid="pat-way-in">
    <div class="base">
      <span class="label">{m.pat_base_url()}</span>
      <span class="base-row">
        <code data-testid="pat-base-url">{base}</code>
        <button
          type="button"
          class="icon-button"
          aria-label={m.pat_copy_base()}
          title={m.pat_copy_base()}
          onclick={() => copy(base, 'base')}
        >
          <Icon name={copied === 'base' ? 'check' : 'copy'} size={16} />
        </button>
      </span>
      <span class="hint">{m.pat_base_url_hint()}</span>
    </div>
    <nav class="docs" aria-label={m.pat_docs_reference()}>
      <a href={SCRIPTING_GUIDE_URL} rel="external noopener" target="_blank" data-testid="pat-guide">
        <Icon name="route" size={16} /><span>{m.pat_docs_guide()}</span><span aria-hidden="true"
          >↗</span
        >
      </a>
      <a
        href={API_REFERENCE_URL}
        rel="external noopener"
        target="_blank"
        data-testid="pat-reference"
      >
        <Icon name="list" size={16} /><span>{m.pat_docs_reference()}</span><span aria-hidden="true"
          >↗</span
        >
      </a>
    </nav>
  </div>
</div>

{#if created}
  <section
    class="secret"
    data-testid="pat-secret"
    tabindex="-1"
    aria-labelledby="pat-secret-title"
    aria-describedby="pat-secret-once"
    bind:this={secretBox}
  >
    <header class="secret-head">
      <Icon name="key" size={20} />
      <h3 id="pat-secret-title">
        {m.pat_secret_title()} <span class="secret-name">{created.name}</span>
      </h3>
    </header>
    <p id="pat-secret-once" class="secret-once">
      <Icon name="alert" size={16} />{m.pat_secret_once()}
    </p>
    <div class="secret-value">
      <code data-testid="pat-secret-value">{created.value}</code>
      <Button
        variant="primary"
        icon={copied === 'secret' ? 'check' : 'copy'}
        onclick={() => copy(created?.value ?? '', 'secret')}
        testid="pat-secret-copy">{copied === 'secret' ? m.copied() : m.copy()}</Button
      >
    </div>
    <div class="quick">
      <span class="label">{m.pat_quickstart()}</span>
      <pre data-testid="pat-quickstart"><code>{example}</code></pre>
      <div>
        <Button
          variant="ghost"
          icon={copied === 'example' ? 'check' : 'copy'}
          onclick={() => copy(example, 'example')}>{m.pat_quickstart_copy()}</Button
        >
        <Button variant="ghost" icon="close" onclick={dismiss} testid="pat-secret-dismiss"
          >{m.close()}</Button
        >
      </div>
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
  <p class="pad quiet" data-testid="pat-empty">{m.pat_empty()}</p>
{:else if tokens}
  {#if active.length}
    <ul class="tokens" data-testid="pat-list" aria-label={m.pat_list_title()}>
      {#each active as token (token.id)}
        {@const left = lifeLeft(token, now)}
        <li data-testid="pat-row" data-name={token.name} class:soon={expiresSoon(token, now)}>
          <span class="who">
            <span class="name">{token.name}</span>
            <span class="mono">{token.prefix}…</span>
            {@render scopeChips(token.scopes)}
          </span>
          <span class="life">
            <span class="meter" aria-hidden="true"
              ><span style:inline-size="{left * 100}%"></span></span
            >
            <span class="mono" data-testid="pat-expiry">{expiryLabel(token)}</span>
            <span class="mono">
              {m.pat_last_used()}
              {token.last_used_at ? formatDateTime(token.last_used_at) : m.never()}
            </span>
          </span>
          <Button
            variant="danger"
            testid="pat-revoke"
            label={m.pat_revoke_named({ name: token.name })}
            onclick={() => revoke(token)}>{m.pat_revoke()}</Button
          >
        </li>
      {/each}
    </ul>
  {:else}
    <p class="pad quiet" data-testid="pat-no-active">{m.pat_no_active()}</p>
  {/if}
  {#if inactive.length}
    <details class="inactive" data-testid="pat-inactive">
      <summary>
        <Icon name="chevron" size={16} />{m.pat_inactive_group({ count: inactive.length })}
      </summary>
      <ul>
        {#each inactive as token (token.id)}
          <li data-testid="pat-inactive-row" data-name={token.name}>
            <span class="name">{token.name}</span>
            <span class="mono">{token.prefix}…</span>
            <span class="mono when">
              {#if token.revoked_at}
                {m.pat_revoked_on()} {formatDateTime(token.revoked_at)}
              {:else}
                {expiryLabel(token)}
              {/if}
            </span>
          </li>
        {/each}
      </ul>
    </details>
  {/if}
{/if}

<p class="md-visually-hidden" role="status" data-testid="pat-status">{note}</p>

<Panel label={m.pat_create_title()}>
  <form class="form" onsubmit={create} novalidate>
    <Field
      id="pat-name"
      label={m.pat_name()}
      placeholder={m.pat_name_hint()}
      required
      maxlength={100}
      bind:value={name}
    />

    <fieldset class="group" data-testid="pat-scopes">
      <legend class="label">{m.pat_scopes()}</legend>
      <div class="scopes">
        {#each SCOPES as key, index (key)}
          {@const needed = key === VIEW_SCOPE && locked}
          <label class="scope" class:on={scopes.includes(key)} class:locked={needed}>
            <input
              type="checkbox"
              data-testid="pat-scope-{key}"
              checked={scopes.includes(key)}
              disabled={needed}
              onchange={(event) => (scopes = toggleScope(scopes, key, event.currentTarget.checked))}
            />
            <span class="scope-num mono" aria-hidden="true"
              >{String(index + 1).padStart(2, '0')}</span
            >
            <span class="scope-name">{scopeName(key)}</span>
            <code class="scope-key">{key}</code>
            <span class="scope-hint">{scopeHint(key)}</span>
            {#if needed}<span class="scope-note">{m.pat_scope_needed()}</span>{/if}
          </label>
        {/each}
      </div>
      <p class="hint never"><Icon name="shield" size={16} />{m.pat_scope_never()}</p>
    </fieldset>

    <fieldset class="group" data-testid="pat-lifetime">
      <legend class="label">{m.pat_lifetime()}</legend>
      <div class="segments">
        {#each LIFETIMES as days (days)}
          <label class="segment" class:on={lifetime === days}>
            <input
              type="radio"
              name="pat-lifetime"
              value={days}
              data-testid="pat-lifetime-{days}"
              checked={lifetime === days}
              onchange={() => (lifetime = days)}
            />
            {days === MAX_LIFETIME ? m.pat_lifetime_year() : m.pat_lifetime_days({ days })}
          </label>
        {/each}
      </div>
      <p class="hint" data-testid="pat-until">{m.pat_lifetime_until({ date: until })}</p>
    </fieldset>

    {#if scopes.length === 0}
      <p class="none">{m.pat_none_selected()}</p>
    {/if}

    <div class="submit">
      <Button
        type="submit"
        variant="primary"
        icon="key"
        pending={busy}
        disabled={!name.trim() || scopes.length === 0}
        testid="pat-create">{m.pat_create()}</Button
      >
      <span class="hint"><Icon name="shield" size={16} />{m.pat_stepup_note()}</span>
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

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .hint {
    display: inline-flex;
    align-items: flex-start;
    gap: var(--md-space-1);
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .mono {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    overflow-wrap: anywhere;
  }

  .name {
    font-weight: var(--md-weight-semibold);
  }

  /* The way in: a terminal-like strip with the base URL next to the two documents. */
  .intro {
    display: grid;
    gap: var(--md-space-3);
  }

  .lead {
    max-width: 68ch;
    margin: 0;
    line-height: var(--md-line-24);
  }

  .way-in {
    display: grid;
    gap: var(--md-space-3);
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-inline-start: var(--md-space-1) solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
  }

  @media (min-width: 900px) {
    .way-in {
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: start;
    }
  }

  .base {
    display: grid;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .base-row {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    min-width: 0;
  }

  .base-row code {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  .icon-button {
    display: grid;
    flex: none;
    place-items: center;
    width: var(--md-target);
    height: var(--md-target);
    padding: 0;
    border: 0;
    background: none;
    color: var(--md-color-ink-muted);
    cursor: pointer;
  }

  .icon-button:hover {
    color: var(--md-color-ink);
  }

  .icon-button:focus-visible,
  .docs a:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .docs {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .docs a {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
    text-decoration: none;
  }

  .docs a:hover {
    border-color: var(--md-color-ink);
  }

  /* The one-time secret: ink frame and heavy top bar, like the account's API tokens. */
  .secret {
    display: grid;
    gap: var(--md-space-3);
    margin: var(--md-space-2) var(--md-gutter) var(--md-space-4);
    padding: var(--md-space-4);
    border: var(--md-border-control) solid var(--md-color-ink);
    border-top-width: var(--md-space-2);
    background: var(--md-color-surface);
    scroll-margin-top: calc(var(--md-target) + var(--md-space-2));
  }

  .secret:focus {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .secret-head {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
  }

  .secret-head h3 {
    margin: 0;
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-semibold);
  }

  .secret-name {
    color: var(--md-color-ink-muted);
    font-weight: var(--md-weight-medium);
    overflow-wrap: anywhere;
  }

  .secret-once {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-2);
    margin: 0;
    color: var(--md-state-paused);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
  }

  .secret-value {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--md-space-2);
  }

  .secret-value code {
    display: flex;
    align-items: center;
    min-height: var(--md-target);
    padding: var(--md-space-2) var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface-sunken);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
    overflow-wrap: anywhere;
    user-select: all;
  }

  .quick {
    display: grid;
    gap: var(--md-space-2);
  }

  .quick pre {
    margin: 0;
    padding: var(--md-space-3);
    overflow-x: auto;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface-sunken);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-20);
  }

  /* Active tokens: name and scopes, then the lifetime as a draining bar. */
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

  .who,
  .life {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .life {
    grid-column: 1;
    max-width: 360px;
  }

  .meter {
    display: block;
    block-size: 4px;
    background: var(--md-color-line);
  }

  .meter span {
    display: block;
    block-size: 100%;
    background: var(--md-color-ink);
  }

  .soon .meter span {
    background: var(--md-state-paused);
  }

  .soon [data-testid='pat-expiry'] {
    color: var(--md-state-paused);
    font-weight: var(--md-weight-semibold);
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
    min-height: 24px;
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-medium);
    white-space: nowrap;
  }

  .inactive {
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .inactive summary {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: 0 var(--md-gutter);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
    list-style: none;
  }

  .inactive summary::-webkit-details-marker {
    display: none;
  }

  .inactive summary:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .inactive[open] summary :global(svg) {
    transform: rotate(90deg);
  }

  .inactive ul {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .inactive li {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0 var(--md-space-3);
    padding: var(--md-space-2) var(--md-gutter);
    border-top: var(--md-hairline) solid var(--md-color-line);
    color: var(--md-color-ink-muted);
  }

  .inactive .name {
    text-decoration: line-through;
    text-decoration-color: var(--md-color-line-strong);
  }

  .when {
    margin-inline-start: auto;
  }

  /* The form: numbered scope tiles and a segmented lifetime. */
  .form {
    display: grid;
    gap: var(--md-space-4);
    max-width: 720px;
  }

  .group {
    display: grid;
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    border: 0;
  }

  .group legend {
    margin-bottom: var(--md-space-2);
    padding: 0;
  }

  .scopes {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: var(--md-space-2);
  }

  .scope {
    position: relative;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-content: start;
    gap: var(--md-space-1) var(--md-space-2);
    padding: var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
    cursor: pointer;
  }

  .scope.on {
    border-color: var(--md-color-ink);
    box-shadow: inset 0 var(--md-space-1) 0 var(--md-color-ink);
  }

  .scope.locked {
    cursor: default;
  }

  .scope input {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    opacity: 0;
    cursor: inherit;
  }

  .scope:focus-within {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .scope-num {
    grid-row: span 2;
    padding-top: 2px;
  }

  .scope-name {
    font-weight: var(--md-weight-semibold);
  }

  .scope-key {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .scope-hint,
  .scope-note {
    grid-column: 2;
    font-size: var(--md-font-size-13);
  }

  .scope-note {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-style: italic;
  }

  .never {
    margin-top: var(--md-space-1);
  }

  .segments {
    display: inline-flex;
    flex-wrap: wrap;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    inline-size: fit-content;
    max-inline-size: 100%;
  }

  .segment {
    position: relative;
    display: inline-flex;
    align-items: center;
    min-height: var(--md-target);
    padding: 0 var(--md-space-4);
    border-inline-start: var(--md-hairline) solid var(--md-color-line-strong);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
    font-variant-numeric: tabular-nums;
    cursor: pointer;
  }

  .segment:first-child {
    border-inline-start: 0;
  }

  .segment.on {
    background: var(--md-color-ink);
    color: var(--md-color-surface);
  }

  .segment input {
    position: absolute;
    inset: 0;
    margin: 0;
    opacity: 0;
    cursor: pointer;
  }

  .segment:focus-within {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .submit {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-3);
  }

  .none {
    margin: 0;
    color: var(--md-state-error);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-medium);
  }
</style>
