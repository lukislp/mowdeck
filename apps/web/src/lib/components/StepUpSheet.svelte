<!--
  The re-authentication sheet of step-up (ADR 0053). It opens when the API answers `401
  reauth-required` for a critical action (deleting a mower, creating an API token, changing
  roles, ...): confirm with a passkey (browser only) or the password, then the request is
  repeated by the API client. One sheet for the whole app, mounted once in the root layout.
-->
<script lang="ts">
  import { Button, Field, Icon, ProblemNotice, Sheet } from '@mowdeck/ui';

  import { stepUp } from '#lib/auth/stepup.svelte.js';
  import { problemMessage } from '#lib/i18n.js';
  import { nativeShell } from '#lib/native/runtime.js';
  import { m } from '#lib/paraglide/messages.js';

  let password = $state('');

  $effect(() => {
    if (!stepUp.open) password = '';
  });

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (password) await stepUp.withPassword(password);
  }
</script>

<Sheet
  open={stepUp.open}
  title={m.stepup_title()}
  closeLabel={m.close()}
  onclose={() => stepUp.cancel()}
  testid="stepup-sheet"
>
  <p class="intro">{m.stepup_intro()}</p>

  {#if stepUp.passkey}
    <Button
      variant="primary"
      full
      icon="key"
      pending={stepUp.busy}
      testid="stepup-passkey"
      onclick={() => stepUp.withPasskey()}>{m.stepup_passkey()}</Button
    >
    {#if stepUp.cancelledPasskey}
      <ProblemNotice tone="info" title={m.stepup_passkey_cancelled()} />
    {/if}
  {:else if nativeShell.active}
    <p class="hint" data-testid="stepup-app-hint">
      <Icon name="key" size={14} />
      {m.stepup_app_hint()}
    </p>
  {/if}

  <form class="password" onsubmit={submit} novalidate>
    <Field
      id="stepup-password"
      type="password"
      label={m.stepup_password()}
      autocomplete="current-password"
      required
      testid="stepup-password"
      bind:value={password}
    />
    {#if stepUp.problem}
      <div data-testid="stepup-error">
        <ProblemNotice
          title={problemMessage(stepUp.problem)}
          detail={stepUp.problem.detail}
          reference={stepUp.problem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      </div>
    {/if}
    <Button
      type="submit"
      variant={stepUp.passkey ? 'ghost' : 'primary'}
      full
      icon="check"
      pending={stepUp.busy}
      disabled={!password}
      testid="stepup-confirm">{m.stepup_confirm()}</Button
    >
  </form>
</Sheet>

<style>
  .intro,
  .hint {
    margin: 0 0 var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .hint {
    display: flex;
    align-items: center;
    gap: var(--md-space-1);
  }

  .password {
    display: grid;
    gap: var(--md-space-3);
    margin-top: var(--md-space-4);
  }
</style>
