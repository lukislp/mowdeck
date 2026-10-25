<!--
  The signed-in user's GitHub link (ADR 0051): "Link with GitHub" (the current password again,
  then GitHub) or the linked account with "Unlink". Only an explicitly linked account can sign in
  with GitHub; the password always stays. Shown when the installation offers GitHub, or while a
  link exists (it can always be removed). Nothing of the installation's GitHub settings appears
  here. MowDeck keeps no GitHub token, so unlinking cannot remove the authorization on GitHub's
  side (ADR 0051, amendment 2026-10-25): the confirmation and the result point to GitHub's own
  page for that. Inside the iOS/Android app linking happens in the browser (the web view cannot finish the
  round trip); unlinking works everywhere.
-->
<script lang="ts">
  import { Button, Field, Panel, ProblemNotice } from '@mowdeck/ui';
  import { onMount } from 'svelte';

  import { replaceState } from '$app/navigation';
  import { page } from '$app/state';

  import { identityApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import {
    githubEnabled,
    linkGitHub,
    linkResult,
    PROVIDER,
    withoutResult,
    type LinkError,
  } from '#lib/auth/github.js';
  import type { components } from '#lib/generated/identity.js';
  import { formatDateTime, problemMessage } from '#lib/i18n.js';
  import { nativeShell } from '#lib/native/runtime.js';
  import { m } from '#lib/paraglide/messages.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  import GitHubMark from './GitHubMark.svelte';

  type Link = components['schemas']['ExternalIdentityOut'];

  /** Where a GitHub user removes an OAuth App's authorization (not the installation's settings). */
  const GITHUB_APPLICATIONS = 'https://github.com/settings/applications';
  const PLACE = '\u0000';
  const revokeHint = $derived(m.github_revoke_hint({ place: PLACE }).split(PLACE));

  let enabled = $state(false);
  let links = $state<Link[] | null>(null);
  let loadProblem = $state<ApiProblem | null>(null);
  const link = $derived(links?.find((l) => l.provider === PROVIDER) ?? null);

  let linking = $state(false);
  let password = $state('');
  let busy = $state(false);
  let problem = $state<ApiProblem | null>(null);
  let notice = $state<string | null>(null);
  let confirming = $state(false);
  let working = $state(false);
  /** Unlinked in this visit: the hint about GitHub's side stays after the toast. */
  let unlinked = $state(false);

  const inApp = $derived(nativeShell.active);

  const linkErrors: Record<LinkError, () => string> = {
    cancelled: m.github_link_cancelled,
    expired: m.github_link_expired,
    denied: m.github_link_denied,
    unavailable: m.github_link_unavailable,
    taken: m.github_link_taken,
    already: m.github_link_already,
  };

  async function load(): Promise<void> {
    try {
      links = await unwrap(identityApi.GET('/api/v1/identity/me/external-identities'));
      loadProblem = null;
    } catch (error) {
      loadProblem = asProblem(error);
    }
  }

  onMount(() => {
    const back = linkResult(page.url.searchParams);
    if (back) {
      if ('linked' in back) toasts.push('ok', m.github_linked());
      else notice = linkErrors[back.error]();
      replaceState(withoutResult(page.url), page.state);
    }
    void githubEnabled().then((on) => (enabled = on));
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
      await linkGitHub(() =>
        unwrap(
          identityApi.POST('/api/v1/identity/me/external-identities/{provider}/link', {
            params: { path: { provider: PROVIDER } },
            body: { password },
          }),
        ),
      );
      // The browser leaves for GitHub; it comes back to the settings.
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
          params: { path: { provider: PROVIDER } },
        }),
      );
      toasts.push('ok', m.github_unlinked());
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

{#snippet revokeAtGitHub(testid: string)}
  <p class="hint revoke" data-testid={testid}>
    {revokeHint[0]}<a href={GITHUB_APPLICATIONS} target="_blank" rel="noopener noreferrer"
      >{m.github_revoke_place()}</a
    >{revokeHint[1]}
  </p>
{/snippet}

{#if enabled || link || unlinked}
  <Panel label={m.github_title()} testid="github-panel">
    <p class="intro">{m.github_intro()}</p>

    {#if loadProblem}
      <ProblemNotice
        title={problemMessage(loadProblem)}
        reference={loadProblem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {:else if link}
      <div class="account" data-testid="github-linked">
        <span class="glyph"><GitHubMark size={20} /></span>
        <span class="body">
          <span class="name">{m.github_linked_as({ name: link.display_name ?? 'GitHub' })}</span>
          <dl class="meta">
            <div>
              <dt>{m.github_linked_at()}</dt>
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
              <span>{m.github_unlink_confirm()}</span>
              {@render revokeAtGitHub('github-unlink-revoke-hint')}
              <span class="row">
                <Button
                  variant="danger"
                  icon="close"
                  pending={working}
                  testid="github-unlink-confirm"
                  onclick={unlink}>{m.github_unlink()}</Button
                >
                <Button variant="ghost" onclick={() => (confirming = false)}>{m.cancel()}</Button>
              </span>
            </span>
          {:else}
            <Button
              variant="ghost"
              icon="close"
              testid="github-unlink"
              onclick={() => (confirming = true)}>{m.github_unlink()}</Button
            >
          {/if}
        </span>
      </div>
    {:else if links}
      <p class="empty" data-testid="github-not-linked">{m.github_not_linked()}</p>
      {#if unlinked}
        {@render revokeAtGitHub('github-revoke-hint')}
      {/if}
    {/if}

    {#if notice}
      <ProblemNotice tone="info" title={notice} />
    {/if}

    {#if links && !link && enabled}
      {#if inApp}
        <p class="hint" data-testid="github-in-app">{m.github_in_app_hint()}</p>
      {:else if linking}
        <form class="add" onsubmit={startLink} novalidate data-testid="github-link-form">
          <p class="hint">{m.github_link_hint()}</p>
          <Field
            id="github-password"
            type="password"
            label={m.password_current()}
            autocomplete="current-password"
            required
            bind:value={password}
          />
          {#if problem}
            <ProblemNotice
              title={problem.status === 401 ? m.github_wrong_password() : problemMessage(problem)}
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
              testid="github-link-continue"
              ><span class="provider"><GitHubMark />{m.github_link_continue()}</span></Button
            >
            <Button variant="ghost" onclick={() => (linking = false)}>{m.cancel()}</Button>
          </div>
        </form>
      {:else}
        <div class="row">
          <Button testid="github-link" onclick={openLink}
            ><span class="provider"><GitHubMark />{m.github_link()}</span></Button
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
  .row {
    margin-top: var(--md-space-3);
  }

  .add .hint,
  .add .row {
    margin-top: 0;
  }

  .confirm .revoke {
    margin-top: 0;
  }

  .revoke a {
    color: var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
    text-decoration: underline;
    text-underline-offset: 2px;
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
