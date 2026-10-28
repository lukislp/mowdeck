<!--
  A short "first time here" note on a main screen (ADR 0062), in the page flow. Shown to users
  who started the setup tour (not to accounts set up before it existed) until they dismiss it;
  the dismissal is stored with the tour progress, so it stays gone on every device.
-->
<script lang="ts">
  import { Icon } from '@mowdeck/ui';

  import { session } from '#lib/auth/session.svelte.js';
  import { m } from '#lib/paraglide/messages.js';

  import { onboarding } from './runtime';
  import type { HintKey } from './steps';

  let { hint, text }: { hint: HintKey; text: string } = $props();

  $effect(() => {
    if (session.status === 'authenticated') void onboarding.load();
  });

  const id = $derived(`firsttime-${hint}`);
</script>

{#if onboarding.hintVisible(hint)}
  <aside class="firsttime" aria-labelledby="{id}-label" data-testid="firsttime-{hint}">
    <Icon name="pin" size={16} />
    <div class="body">
      <span class="label" id="{id}-label">{m.firsttime_label()}</span>
      <p>{text}</p>
    </div>
    <button
      type="button"
      class="got-it"
      data-testid="firsttime-{hint}-dismiss"
      onclick={() => void onboarding.dismissHint(hint)}>{m.firsttime_dismiss()}</button
    >
  </aside>
{/if}

<style>
  .firsttime {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-2);
    padding: var(--md-space-2) var(--md-space-2) var(--md-space-2) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .firsttime > :global(svg) {
    flex: none;
    margin-top: var(--md-space-0_5);
  }

  .body {
    flex: 1;
    min-width: 0;
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  p {
    margin: 0;
  }

  .got-it {
    flex: none;
    min-height: 40px;
    padding: 0 var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
    cursor: pointer;
  }

  .got-it:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }
</style>
