<script lang="ts">
  import { Button, Field, Panel, ProblemNotice, SegmentedControl } from '@mowdeck/ui';

  import { identityApi, unwrap } from '$lib/api/client';
  import { asProblem, type ApiProblem } from '$lib/api/problem';
  import { session } from '$lib/auth/session.svelte';
  import type { components } from '$lib/generated/identity';
  import { formatDateTime, problemMessage } from '$lib/i18n';
  import { m } from '$lib/paraglide/messages';

  type Token = components['schemas']['ApiTokenOut'];

  const PRESETS = {
    view: ['mower.view'],
    control: ['mower.view', 'mower.control'],
  } as const;

  const accountId = $derived(session.me?.account_id ?? '');
  let tokens = $state<Token[] | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let name = $state('');
  let preset = $state<keyof typeof PRESETS>('control');
  let secret = $state<string | null>(null);
  let copied = $state(false);
  let busy = $state(false);

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

  $effect(() => {
    void load();
  });

  async function create(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    problem = null;
    try {
      const created = await unwrap(
        identityApi.POST('/api/v1/identity/accounts/{account_id}/api-tokens', {
          params: { path: { account_id: accountId } },
          body: {
            name: name.trim(),
            grants: [
              { level: { type: 'account', id: accountId }, permissions: [...PRESETS[preset]] },
            ],
          },
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
</script>

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
  <ul class="tokens">
    {#each tokens as token (token.id)}
      <li class:revoked={!!token.revoked_at}>
        <span class="who">
          <span class="name">{token.name}</span>
          <span class="mono">{token.prefix}… · {token.permissions.join(', ')}</span>
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
    <div class="preset">
      <span class="label">{m.tokens_preset()}</span>
      <SegmentedControl
        label={m.tokens_preset()}
        value={preset}
        options={[
          { value: 'view', label: m.tokens_preset_view() },
          { value: 'control', label: m.tokens_preset_control() },
        ]}
        onchange={(value) => (preset = value as keyof typeof PRESETS)}
      />
    </div>
    <div>
      <Button type="submit" variant="primary" icon="key" pending={busy} disabled={!name.trim()}
        >{m.tokens_create()}</Button
      >
    </div>
  </form>
</Panel>

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
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

  .tokens li {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--md-space-2) var(--md-space-3);
    padding: var(--md-space-3) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .tokens li.revoked {
    background: var(--md-color-bg);
    color: var(--md-color-ink-muted);
  }

  .who {
    display: flex;
    flex-direction: column;
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

  .form {
    display: grid;
    gap: var(--md-space-4);
    max-width: 560px;
  }

  .preset {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }
</style>
