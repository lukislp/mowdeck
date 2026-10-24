<script lang="ts">
  import { Button, Field, ProblemNotice } from '@mowdeck/ui';

  import { page } from '$app/state';

  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { session } from '#lib/auth/session.svelte.js';
  import AuthFrame from '#lib/components/AuthFrame.svelte';
  import { loadConfig } from '#lib/config.js';
  import { problemMessage } from '#lib/i18n.js';
  import { navigatePath } from '#lib/navigate.js';
  import { m } from '#lib/paraglide/messages.js';

  let username = $state('');
  let password = $state('');
  let busy = $state(false);
  let problem = $state<ApiProblem | null>(null);

  const next = $derived.by(() => {
    const target = page.url.searchParams.get('next') ?? '/mowers';
    return target.startsWith('/') && !target.startsWith('//') ? target : '/mowers';
  });

  $effect(() => {
    if (session.status === 'authenticated') void navigatePath(next, { replace: true });
  });

  // Links to the legal pages only when the installation turns them on (`/config.json`).
  let legal = $state(false);
  $effect(() => {
    void loadConfig().then((config) => (legal = config.legalEnabled));
  });

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    problem = null;
    try {
      await session.login(username.trim(), password);
      password = '';
      await navigatePath(session.me?.must_change_password ? '/settings?password=1#profile' : next, {
        replace: true,
      });
    } catch (error) {
      problem = asProblem(error);
    } finally {
      busy = false;
    }
  }

  const message = $derived(
    problem === null ? null : problem.status === 401 ? m.login_failed() : problemMessage(problem),
  );
</script>

<svelte:head><title>{m.login_title()} · {m.app_name()}</title></svelte:head>

<AuthFrame title={m.login_title()}>
  {#if session.ended === 'expired' && !problem}
    <ProblemNotice tone="info" title={m.login_session_ended()} />
  {/if}
  <form class="form" onsubmit={submit} novalidate>
    <Field
      id="username"
      label={m.login_username()}
      autocomplete="username"
      required
      bind:value={username}
    />
    <Field
      id="password"
      type="password"
      label={m.login_password()}
      autocomplete="current-password"
      required
      bind:value={password}
    />
    {#if message && problem}
      <ProblemNotice
        title={message}
        reference={problem.status === 401 ? null : problem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {/if}
    <Button
      type="submit"
      variant="primary"
      full
      pending={busy}
      disabled={!username || !password}
      testid="login-submit">{m.login_submit()}</Button
    >
  </form>
  <a class="switch" href="/register">{m.login_register_link()}</a>
  {#if legal}
    <nav class="legal" aria-label={m.legal_nav()} data-testid="legal-links">
      <a href="/legal/imprint" data-testid="legal-link-imprint">{m.legal_imprint()}</a>
      <a href="/legal/terms" data-testid="legal-link-terms">{m.legal_terms()}</a>
      <a href="/legal/privacy" data-testid="legal-link-privacy">{m.legal_privacy()}</a>
    </nav>
  {/if}
</AuthFrame>

<style>
  .form {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-4);
  }

  .switch {
    display: inline-flex;
    align-items: center;
    min-height: var(--md-target);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .legal {
    display: flex;
    flex-wrap: wrap;
    gap: 0 var(--md-space-4);
    padding-top: var(--md-space-2);
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .legal a {
    display: inline-flex;
    align-items: center;
    min-height: var(--md-target);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }
</style>
