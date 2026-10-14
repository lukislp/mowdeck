<script lang="ts">
  import { Button, Field, ProblemNotice } from '@mowdeck/ui';

  import { goto } from '$app/navigation';

  import { asProblem, problemFromResponse, type ApiProblem } from '$lib/api/problem';
  import { IDENTITY, session } from '$lib/auth/session.svelte';
  import AuthFrame from '$lib/components/AuthFrame.svelte';
  import { problemMessage } from '$lib/i18n';
  import { m } from '$lib/paraglide/messages';

  let username = $state('');
  let password = $state('');
  let accountName = $state('');
  let displayName = $state('');
  let busy = $state(false);
  let problem = $state<ApiProblem | null>(null);

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    problem = null;
    try {
      const response = await fetch(`${IDENTITY}/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: username.trim(),
          password,
          account_name: accountName.trim(),
          display_name: displayName.trim() || null,
        }),
      });
      if (!response.ok) throw await problemFromResponse(response);
      await session.login(username.trim(), password);
      password = '';
      await goto('/mowers', { replaceState: true });
    } catch (error) {
      problem = asProblem(error);
    } finally {
      busy = false;
    }
  }

  const message = $derived(
    problem === null
      ? null
      : problem.status === 403
        ? m.register_closed()
        : problemMessage(problem),
  );
</script>

<svelte:head><title>{m.register_title()} · {m.app_name()}</title></svelte:head>

<AuthFrame title={m.register_title()}>
  <form class="form" onsubmit={submit} novalidate>
    <Field
      id="account_name"
      label={m.register_account_name()}
      autocomplete="organization"
      required
      bind:value={accountName}
      error={problem?.fieldError('account_name') ?? null}
    />
    <Field
      id="display_name"
      label={m.register_display_name()}
      autocomplete="name"
      hint={m.optional()}
      bind:value={displayName}
    />
    <Field
      id="username"
      label={m.login_username()}
      autocomplete="username"
      required
      bind:value={username}
      error={problem?.fieldError('username') ?? null}
    />
    <Field
      id="password"
      type="password"
      label={m.login_password()}
      autocomplete="new-password"
      required
      hint={m.password_hint()}
      bind:value={password}
      error={problem?.fieldError('password') ?? null}
    />
    {#if message && problem}
      <ProblemNotice
        title={message}
        detail={problem.detail}
        reference={problem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {/if}
    <Button
      type="submit"
      variant="primary"
      full
      pending={busy}
      disabled={!username || !password || !accountName}
      testid="register-submit">{m.register_submit()}</Button
    >
  </form>
  <a class="switch" href="/login">{m.register_have_account()}</a>
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
</style>
