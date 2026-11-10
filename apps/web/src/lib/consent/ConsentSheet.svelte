<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  The consent dialog for cloud AI (ADR 0081). It opens when a feature wants to send camera
  pictures or text to a cloud model the user has not agreed to (state in `consent.svelte.ts`):
  what is sent, to whom (provider and model from the API), that it leaves this installation,
  that it is optional and how to withdraw. "Not now" (also Escape and the close button) leaves
  the feature unused without an error. One sheet for the whole app, mounted once in the root
  layout. Focus moves into the dialog when it opens and back to the element that had it.
-->
<script lang="ts">
  import { Button, ProblemNotice, Sheet } from '@mowdeck/ui';

  import { problemMessage } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';

  import { consent as shared, type ConsentState } from './consent.svelte.js';

  let { consent = shared }: { consent?: ConsentState } = $props();

  const destination = $derived(consent.status?.destination ?? null);

  let intro = $state<HTMLElement | null>(null);
  let returnTo: Element | null = null;

  $effect(() => {
    if (consent.open) {
      returnTo = document.activeElement;
      // The sheet renders after `open` flips: focus its first paragraph, so a screen reader
      // starts reading at the question and Tab reaches the buttons next.
      queueMicrotask(() => intro?.focus());
    } else if (returnTo instanceof HTMLElement) {
      returnTo.focus();
      returnTo = null;
    }
  });
</script>

<Sheet
  open={consent.open}
  title={m.consent_title()}
  closeLabel={m.consent_decline()}
  onclose={() => consent.decline()}
  testid="consent-sheet"
>
  {#if destination}
    <div class="text" data-testid="consent-body">
      <p class="intro" tabindex="-1" bind:this={intro}>
        {m.consent_intro({ provider: destination.provider, model: destination.model })}
      </p>

      {#if destination.endpoint !== 'default'}
        <p data-testid="consent-endpoint">{m.consent_endpoint({ host: destination.endpoint })}</p>
      {/if}

      <h3>{m.consent_sends_title()}</h3>
      <ul>
        {#if consent.scope !== 'chat'}
          <li>{m.consent_sends_pictures()}</li>
          <li>{m.consent_sends_map()}</li>
          <li>{m.consent_sends_status()}</li>
        {/if}
        {#if consent.scope !== 'assist'}
          <li>{m.consent_sends_text()}</li>
          <li>{m.consent_sends_names()}</li>
        {/if}
      </ul>

      <p>{m.consent_leaves({ provider: destination.provider })}</p>
      <p>{m.consent_optional()}</p>
      <p>{m.consent_withdraw()}</p>
    </div>
  {/if}

  {#if consent.problem}
    <ProblemNotice
      title={problemMessage(consent.problem)}
      detail={consent.problem.detail}
      reference={consent.problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  {/if}

  {#snippet footer()}
    <div class="actions">
      <Button
        variant="primary"
        icon="check"
        pending={consent.busy}
        testid="consent-agree"
        onclick={() => void consent.agree()}>{m.consent_agree()}</Button
      >
      <Button
        variant="ghost"
        disabled={consent.busy}
        testid="consent-decline"
        onclick={() => consent.decline()}>{m.consent_decline()}</Button
      >
    </div>
  {/snippet}
</Sheet>

<style>
  .text {
    display: grid;
    gap: var(--md-space-2);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
  }

  .text p,
  .text ul,
  .text h3 {
    margin: 0;
  }

  .intro:focus {
    outline: none;
  }

  h3 {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  ul {
    display: grid;
    gap: var(--md-space-1);
    padding-inline-start: var(--md-space-4);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }
</style>
