<!--
  "Linked accounts" in the account settings (ADR 0051 GitHub, ADR 0052 Apple): the signed-in
  user's links to external login providers. Linked providers are a compact list (mark, name,
  date, "Unlink" with a confirmation); the providers the installation offers and the user has not
  linked yet are buttons side by side. A button asks for the current password again, then the
  browser goes to the provider. Only an explicitly linked account can sign in with the provider;
  the password always stays. Providers the installation does not offer do not appear (only
  `GET /login-methods` flags reach the browser, never any provider setting); with none offered
  and none linked the section is hidden, and a link can always be removed. MowDeck keeps no
  provider token, so unlinking cannot remove the authorization on the provider's side: the
  confirmation and the result point to the provider's own page. Inside the iOS/Android app
  linking happens in the browser (the web view cannot finish the round trip); unlinking works
  everywhere. The providers come from `linked-providers.ts`.
-->
<script lang="ts">
  import { Button, Field, Panel, ProblemNotice } from '@mowdeck/ui';
  import { onMount } from 'svelte';

  import { replaceState } from '$app/navigation';
  import { page } from '$app/state';

  import { identityApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { linkProvider, linkResultOf, providerEnabled, withoutResult } from '#lib/auth/oauth.js';
  import type { components } from '#lib/generated/identity.js';
  import { formatDate, problemMessage } from '#lib/i18n.js';
  import { nativeShell } from '#lib/native/runtime.js';
  import { m } from '#lib/paraglide/messages.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  import { LINK_PROVIDERS, type LinkProvider } from './linked-providers.js';

  type Link = components['schemas']['ExternalIdentityOut'];

  /** The providers the installation offers (`GET /login-methods`). */
  let offered = $state<string[]>([]);
  let links = $state<Link[] | null>(null);
  let loadProblem = $state<ApiProblem | null>(null);
  let loaded = $state(false);

  /** The provider whose password form is open. */
  let linkingId = $state<string | null>(null);
  let password = $state('');
  let busy = $state(false);
  let problem = $state<ApiProblem | null>(null);
  let notice = $state<string | null>(null);
  /** The provider whose unlink confirmation is open. */
  let confirmingId = $state<string | null>(null);
  let working = $state(false);
  /** Unlinked in this visit: the hint about the provider's side stays after the toast. */
  let unlinkedIds = $state<string[]>([]);

  const inApp = $derived(nativeShell.active);
  const linkOf = (id: string): Link | null => links?.find((l) => l.provider === id) ?? null;
  const linked = $derived(LINK_PROVIDERS.filter((p) => linkOf(p.id)));
  const justUnlinked = $derived(
    LINK_PROVIDERS.filter((p) => unlinkedIds.includes(p.id) && !linkOf(p.id)),
  );
  /** Offered and not linked: these get a button. */
  const available = $derived(
    LINK_PROVIDERS.filter((p) => offered.includes(p.id) && links && !linkOf(p.id)),
  );
  const linking = $derived(available.find((p) => p.id === linkingId) ?? null);
  const visible = $derived(
    loaded &&
      (offered.length > 0 || linked.length > 0 || justUnlinked.length > 0 || loadProblem !== null),
  );

  async function load(): Promise<void> {
    try {
      links = await unwrap(identityApi.GET('/api/v1/identity/me/external-identities'));
      loadProblem = null;
    } catch (error) {
      loadProblem = asProblem(error);
    }
  }

  onMount(() => {
    for (const provider of LINK_PROVIDERS) {
      const back = linkResultOf(provider.id, page.url.searchParams);
      if (!back) continue;
      if ('linked' in back) toasts.push('ok', provider.texts.linked());
      else notice = provider.texts.linkErrors[back.error]();
      replaceState(withoutResult(page.url), page.state);
    }
    const flags = Promise.all(
      LINK_PROVIDERS.map(async (p) => ((await providerEnabled(p.id)) ? p.id : null)),
    ).then((ids) => {
      offered = ids.filter((id): id is string => id !== null);
    });
    void Promise.all([flags, load()]).then(() => (loaded = true));
  });

  function openLink(provider: LinkProvider): void {
    linkingId = provider.id;
    password = '';
    problem = null;
    notice = null;
  }

  async function startLink(event: SubmitEvent, provider: LinkProvider): Promise<void> {
    event.preventDefault();
    busy = true;
    problem = null;
    try {
      await linkProvider(() =>
        unwrap(
          identityApi.POST('/api/v1/identity/me/external-identities/{provider}/link', {
            params: { path: { provider: provider.id } },
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

  async function unlink(provider: LinkProvider): Promise<void> {
    working = true;
    try {
      await unwrap(
        identityApi.DELETE('/api/v1/identity/me/external-identities/{provider}', {
          params: { path: { provider: provider.id } },
        }),
      );
      toasts.push('ok', provider.texts.unlinked());
      confirmingId = null;
      unlinkedIds = [...unlinkedIds, provider.id];
      await load();
    } catch (error) {
      const found = asProblem(error);
      toasts.push('error', problemMessage(found), found.detail ?? undefined);
    } finally {
      working = false;
    }
  }

  const PLACE = '\u0000';
  /** The note about the authorization that stays at the provider, split around its place. */
  function revokeParts(provider: LinkProvider): [string, string] {
    const [before = '', after = ''] = provider.revoke.hint({ place: PLACE }).split(PLACE);
    return [before, after];
  }
</script>

{#snippet revokeNote(provider: LinkProvider, testid: string)}
  {@const [before, after] = revokeParts(provider)}
  <p class="revoke" data-testid={testid}>
    {before}{#if provider.revoke.href}<a
        href={provider.revoke.href}
        target="_blank"
        rel="noopener noreferrer">{provider.revoke.place()}</a
      >{:else}<strong>{provider.revoke.place()}</strong>{/if}{after}
  </p>
{/snippet}

{#if visible}
  <Panel label={m.login_links_title()} testid="login-links-panel">
    <p class="intro">{m.login_links_intro()}</p>

    {#if loadProblem}
      <div class="gap">
        <ProblemNotice
          title={problemMessage(loadProblem)}
          reference={loadProblem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      </div>
    {/if}

    {#if linked.length > 0}
      <ul class="accounts">
        {#each linked as provider (provider.id)}
          {@const link = linkOf(provider.id)}
          {@const Mark = provider.mark}
          <li class="account" data-testid="{provider.id}-linked">
            <span class="glyph"><Mark size={20} /></span>
            <span class="body">
              <span class="name">
                {provider.label}{#if provider.showsAccountName && link?.display_name}<span
                    class="account-name">&nbsp;· {link.display_name}</span
                  >{/if}
              </span>
              <span class="date">
                {provider.texts.linkedAt()}
                {link ? formatDate(link.linked_at) : ''}
              </span>
            </span>
            {#if confirmingId !== provider.id}
              <span class="tools">
                <Button
                  variant="ghost"
                  icon="close"
                  label={provider.texts.unlink()}
                  testid="{provider.id}-unlink"
                  onclick={() => (confirmingId = provider.id)}>{m.login_links_unlink()}</Button
                >
              </span>
            {:else}
              <span class="confirm" role="alert">
                <span>{provider.texts.unlinkConfirm()}</span>
                {@render revokeNote(provider, `${provider.id}-unlink-revoke-hint`)}
                <span class="row">
                  <Button
                    variant="danger"
                    icon="close"
                    pending={working}
                    testid="{provider.id}-unlink-confirm"
                    onclick={() => unlink(provider)}>{provider.texts.unlink()}</Button
                  >
                  <Button variant="ghost" onclick={() => (confirmingId = null)}>{m.cancel()}</Button
                  >
                </span>
              </span>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}

    {#each justUnlinked as provider (provider.id)}
      <div class="gap">{@render revokeNote(provider, `${provider.id}-revoke-hint`)}</div>
    {/each}

    {#if notice}
      <div class="gap"><ProblemNotice tone="info" title={notice} /></div>
    {/if}

    {#if available.length > 0}
      {#if inApp}
        <p class="hint" data-testid="login-links-in-app">{m.login_links_in_app_hint()}</p>
      {:else if linking}
        {@const Mark = linking.mark}
        <form
          class="add"
          onsubmit={(event) => startLink(event, linking)}
          novalidate
          data-testid="{linking.id}-link-form"
        >
          <p class="hint">{linking.texts.linkHint()}</p>
          <Field
            id="{linking.id}-password"
            type="password"
            label={m.password_current()}
            autocomplete="current-password"
            required
            bind:value={password}
          />
          {#if problem}
            <ProblemNotice
              title={problem.status === 401
                ? linking.texts.wrongPassword()
                : problemMessage(problem)}
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
              testid="{linking.id}-link-continue"
              ><span class="provider"><Mark size={20} />{linking.texts.linkContinue()}</span
              ></Button
            >
            <Button variant="ghost" onclick={() => (linkingId = null)}>{m.cancel()}</Button>
          </div>
        </form>
      {:else}
        <div class="connect" role="group" aria-label={m.login_links_connect()}>
          {#each available as provider (provider.id)}
            {@const Mark = provider.mark}
            <Button
              variant="primary"
              label={provider.texts.link()}
              testid="{provider.id}-link"
              onclick={() => openLink(provider)}
              ><span class="provider"><Mark size={20} />{provider.label}</span></Button
            >
          {/each}
        </div>
      {/if}
    {/if}
  </Panel>
{/if}

<style>
  .intro,
  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .intro {
    max-width: 60ch;
  }

  .hint,
  .gap,
  .connect {
    margin-top: var(--md-space-3);
  }

  /* The linked accounts as one compact list, like tags on the instrument panel. */
  .accounts {
    display: grid;
    gap: var(--md-space-2);
    margin: var(--md-space-3) 0 0;
    padding: 0;
    list-style: none;
  }

  .account {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: var(--md-space-1) var(--md-space-3);
    align-items: center;
    padding: var(--md-space-2) var(--md-space-3);
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
    min-width: 0;
  }

  .name {
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  /* Wraps below the provider name as a whole, and breaks only when the login is longer than the row. */
  .account-name {
    display: inline-block;
    overflow-wrap: anywhere;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-regular);
  }

  .date {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .confirm {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    grid-column: 1 / -1;
    font-size: var(--md-font-size-14);
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  /* The connect buttons side by side; they wrap on a narrow phone. */
  .connect {
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

  .revoke {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .revoke a,
  .revoke strong {
    color: var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
  }

  .revoke a {
    text-decoration: underline;
    text-underline-offset: 2px;
  }
</style>
