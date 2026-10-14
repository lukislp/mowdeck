<!--
  Link a Mammotion account: e-mail and a write-only password. The login at the vendor cloud can
  take close to a minute, so the pending state says so calmly. A refused login lands on the
  password, "already linked" on the e-mail, an unreachable cloud above the form. `onconnect`
  does the request and throws the ApiProblem; the password is cleared after every attempt.
-->
<script lang="ts">
  import { Button, Field, ProblemNotice } from '@mowdeck/ui';
  import { tick } from 'svelte';

  import { asProblem } from '$lib/api/problem';
  import { accountIssue, emailLooksValid, type AccountIssue } from '$lib/linked/accounts';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    onconnect: (email: string, password: string) => Promise<void>;
    oncancel?: () => void;
    /** Prefix of the input ids (two forms on one page stay distinct). */
    idPrefix?: string;
  }
  let { onconnect, oncancel, idPrefix = 'linked' }: Props = $props();

  let email = $state('');
  let password = $state('');
  let tried = $state(false);
  let pending = $state(false);
  let issue = $state<AccountIssue | null>(null);
  let correlation = $state<string | null>(null);

  const emailError = $derived.by(() => {
    if (issue?.field === 'email') return issue.message;
    if (tried && !emailLooksValid(email)) return m.linked_err_email();
    return null;
  });
  const passwordError = $derived.by(() => {
    if (issue?.field === 'password') return issue.message;
    if (tried && !password) return m.conn_error_required();
    return null;
  });

  async function focusInvalid(): Promise<void> {
    await tick();
    document
      .querySelector<HTMLElement>(`[data-testid="linked-connect"] [aria-invalid="true"]`)
      ?.focus();
  }

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    tried = true;
    issue = null;
    correlation = null;
    if (!emailLooksValid(email) || !password) {
      await focusInvalid();
      return;
    }
    pending = true;
    try {
      await onconnect(email.trim(), password);
      email = '';
      tried = false;
    } catch (error) {
      const problem = asProblem(error);
      issue = accountIssue(problem);
      correlation = problem.correlationId;
    } finally {
      password = '';
      pending = false;
    }
    // After `pending`: disabled inputs cannot take the focus.
    if (issue) await focusInvalid();
  }
</script>

<form class="connect" data-testid="linked-connect" onsubmit={submit} novalidate>
  <p class="hint">{m.linked_connect_hint()}</p>
  <div class="fields">
    <Field
      id="{idPrefix}-email"
      type="email"
      label={m.linked_email()}
      autocomplete="off"
      required
      disabled={pending}
      error={emailError}
      bind:value={email}
    />
    <Field
      id="{idPrefix}-password"
      type="password"
      label={m.linked_password()}
      autocomplete="new-password"
      required
      disabled={pending}
      hint={m.linked_password_hint()}
      error={passwordError}
      bind:value={password}
    />
  </div>
  {#if issue && !issue.field}
    <div data-testid="linked-connect-problem">
      <ProblemNotice
        title={issue.message}
        detail={issue.detail}
        reference={correlation}
        referenceLabel={m.problem_reference()}
      />
    </div>
  {/if}
  {#if pending}
    <div class="pending" role="status" data-testid="linked-connect-pending">
      <span class="pulse" aria-hidden="true"></span>
      <span class="what">{m.linked_connecting()}</span>
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

<style>
  .connect {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-4);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
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
