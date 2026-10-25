<!--
  The signed-in user's link to an external login provider (GitHub, ADR 0051; Apple, ADR 0052):
  "Link with <provider>" (the current password again, then the provider) or the linked account
  with "Unlink". Only an explicitly linked account can sign in with the provider; the password
  always stays. Shown when the installation offers the provider, or while a link exists (it can
  always be removed). Nothing of the installation's provider settings appears here. MowDeck keeps
  no provider token, so unlinking cannot remove the authorization on the provider's side: the
  optional `revoke` snippet points to the provider's own page for that (confirmation and result).
  Inside the iOS/Android app linking happens in the browser (the web view cannot finish the
  round trip); unlinking works everywhere. `GitHubPanel` and `ApplePanel` supply the texts.
-->
<script lang="ts" module>
  import type { LinkError } from '#lib/auth/oauth.js';

  /** The texts of one provider (paraglide message functions). */
  export interface PanelTexts {
    title: () => string;
    intro: () => string;
    notLinked: () => string;
    link: () => string;
    linkHint: () => string;
    linkContinue: () => string;
    wrongPassword: () => string;
    linked: () => string;
    linkedAs: (inputs: { name: string }) => string;
    linkedAt: () => string;
    unlink: () => string;
    unlinkConfirm: () => string;
    unlinked: () => string;
    inAppHint: () => string;
    linkErrors: Record<LinkError, () => string>;
  }
</script>

<script lang="ts">
  import { Button, Field, Panel, ProblemNotice } from '@mowdeck/ui';
  import { onMount, type Snippet } from 'svelte';

  import { replaceState } from '$app/navigation';
  import { page } from '$app/state';

  import { identityApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { linkProvider, linkResultOf, providerEnabled, withoutResult } from '#lib/auth/oauth.js';
  import type { components } from '#lib/generated/identity.js';
  import { formatDateTime, problemMessage } from '#lib/i18n.js';
  import { nativeShell } from '#lib/native/runtime.js';
  import { m } from '#lib/paraglide/messages.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  type Link = components['schemas']['ExternalIdentityOut'];

  let {
    provider,
    label,
    texts,
    mark,
    revoke,
  }: {
    /** The provider key (`github`): API path, result parameter and test ids. */
    provider: string;
    /** The provider's name, shown when the link has no label. */
    label: string;
    texts: PanelTexts;
    /** The provider's logo. */
    mark: Snippet;
    /** The note about the authorization that stays at the provider, with the given test id. */
    revoke?: Snippet<[string]>;
  } = $props();

  let enabled = $state(false);
  let links = $state<Link[] | null>(null);
  let loadProblem = $state<ApiProblem | null>(null);
  const link = $derived(links?.find((l) => l.provider === provider) ?? null);

  let linking = $state(false);
  let password = $state('');
  let busy = $state(false);
  let problem = $state<ApiProblem | null>(null);
  let notice = $state<string | null>(null);
  let confirming = $state(false);
  let working = $state(false);
  /** Unlinked in this visit: the hint about the provider's side stays after the toast. */
  let unlinked = $state(false);

  const inApp = $derived(nativeShell.active);

  async function load(): Promise<void> {
    try {
      links = await unwrap(identityApi.GET('/api/v1/identity/me/external-identities'));
      loadProblem = null;
    } catch (error) {
      loadProblem = asProblem(error);
    }
  }

  onMount(() => {
    const back = linkResultOf(provider, page.url.searchParams);
    if (back) {
      if ('linked' in back) toasts.push('ok', texts.linked());
      else notice = texts.linkErrors[back.error]();
      replaceState(withoutResult(page.url), page.state);
    }
    void providerEnabled(provider).then((on) => (enabled = on));
    void load();
  });

  function openLink(): void {
    linking = true;
    password = '';
    problem = null;
    notice = null;
    unlinked = false;
  }

  async function startLink(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    problem = null;
    try {
      await linkProvider(() =>
        unwrap(
          identityApi.POST('/api/v1/identity/me/external-identities/{provider}/link', {
            params: { path: { provider } },
            body: { password },
          }),
        ),
      );
      // The browser leaves for the provider; it comes back to the settings.
    } catch (error) {
      problem = asProblem(error);
      busy = false;
    }
  }

  async function unlink(): Promise<void> {
    working = true;
    try {
      await unwrap(
        identityApi.DELETE('/api/v1/identity/me/external-identities/{provider}', {
          params: { path: { provider } },
        }),
      );
      toasts.push('ok', texts.unlinked());
      confirming = false;
      unlinked = true;
      await load();
    } catch (error) {
      const found = asProblem(error);
      toasts.push('error', problemMessage(found), found.detail ?? undefined);
    } finally {
      working = false;
    }
  }
</script>

{#if enabled || link || unlinked}
  <Panel label={texts.title()} testid="{provider}-panel">
    <p class="intro">{texts.intro()}</p>

    {#if loadProblem}
      <ProblemNotice
        title={problemMessage(loadProblem)}
        reference={loadProblem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {:else if link}
      <div class="account" data-testid="{provider}-linked">
        <span class="glyph">{@render mark()}</span>
        <span class="body">
          <span class="name">{texts.linkedAs({ name: link.display_name ?? label })}</span>
          <dl class="meta">
            <div>
              <dt>{texts.linkedAt()}</dt>
              <dd>{formatDateTime(link.linked_at)}</dd>
            </div>
            <div>
              <dt>{m.passkeys_last_used()}</dt>
              <dd>{link.last_used_at ? formatDateTime(link.last_used_at) : m.never()}</dd>
            </div>
          </dl>
        </span>
        <span class="tools">
          {#if confirming}
            <span class="confirm" role="alert">
              <span>{texts.unlinkConfirm()}</span>
              {@render revoke?.(`${provider}-unlink-revoke-hint`)}
              <span class="row">
                <Button
                  variant="danger"
                  icon="close"
                  pending={working}
                  testid="{provider}-unlink-confirm"
                  onclick={unlink}>{texts.unlink()}</Button
                >
                <Button variant="ghost" onclick={() => (confirming = false)}>{m.cancel()}</Button>
              </span>
            </span>
          {:else}
            <Button
              variant="ghost"
              icon="close"
              testid="{provider}-unlink"
              onclick={() => (confirming = true)}>{texts.unlink()}</Button
            >
          {/if}
        </span>
      </div>
    {:else if links}
      <p class="empty" data-testid="{provider}-not-linked">{texts.notLinked()}</p>
      {#if unlinked}
        <div class="note">{@render revoke?.(`${provider}-revoke-hint`)}</div>
      {/if}
    {/if}

    {#if notice}
      <ProblemNotice tone="info" title={notice} />
    {/if}

    {#if links && !link && enabled}
      {#if inApp}
        <p class="hint" data-testid="{provider}-in-app">{texts.inAppHint()}</p>
      {:else if linking}
        <form class="add" onsubmit={startLink} novalidate data-testid="{provider}-link-form">
          <p class="hint">{texts.linkHint()}</p>
          <Field
            id="{provider}-password"
            type="password"
            label={m.password_current()}
            autocomplete="current-password"
            required
            bind:value={password}
          />
          {#if problem}
            <ProblemNotice
              title={problem.status === 401 ? texts.wrongPassword() : problemMessage(problem)}
              reference={problem.status === 401 ? null : problem.correlationId}
              referenceLabel={m.problem_reference()}
            />
          {/if}
          <div class="row">
            <Button
              type="submit"
              variant="primary"
              pending={busy}
              disabled={!password}
              testid="{provider}-link-continue"
              ><span class="provider">{@render mark()}{texts.linkContinue()}</span></Button
            >
            <Button variant="ghost" onclick={() => (linking = false)}>{m.cancel()}</Button>
          </div>
        </form>
      {:else}
        <div class="row">
          <Button testid="{provider}-link" onclick={openLink}
            ><span class="provider">{@render mark()}{texts.link()}</span></Button
          >
        </div>
      {/if}
    {/if}
  </Panel>
{/if}

<style>
  .intro,
  .hint,
  .empty {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .intro {
    max-width: 60ch;
  }

  /* The linked account as a tag on the instrument panel, like a passkey. */
  .account {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--md-space-3);
    margin: var(--md-space-3) 0;
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
  }

  .glyph {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    border: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-round);
    background: var(--md-color-surface-sunken);
  }

  .body {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .name {
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-4);
    margin: 0;
  }

  .meta div {
    display: flex;
    gap: var(--md-space-1);
  }

  .meta dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .meta dd {
    margin: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  .tools {
    display: flex;
    flex-wrap: wrap;
    grid-column: 1 / -1;
    gap: var(--md-space-2);
    justify-content: flex-end;
  }

  .confirm {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    width: 100%;
    font-size: var(--md-font-size-14);
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .provider {
    display: inline-flex;
    gap: var(--md-space-2);
    align-items: center;
  }

  .add {
    display: grid;
    gap: var(--md-space-4);
    max-width: 480px;
    margin-top: var(--md-space-3);
  }

  .hint,
  .row,
  .note {
    margin-top: var(--md-space-3);
  }

  .add .hint,
  .add .row {
    margin-top: 0;
  }

  @media (min-width: 720px) {
    .account {
      grid-template-columns: auto minmax(0, 1fr) auto;
      align-items: center;
    }

    .tools {
      grid-column: auto;
    }
  }
</style>
