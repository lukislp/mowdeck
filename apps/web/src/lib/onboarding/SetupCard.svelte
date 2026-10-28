<!--
  The setup tour on the mower list (ADR 0062), in the page flow (never an overlay, so the list
  and the add button stay usable):
  - `tour`: a card with the progress scale, the next open step and its action (the existing
    flow), "Tour öffnen" for the whole list and "Später" to hide it. A new account without a
    mower starts the tour here on its first visit.
  - `hint`: one slim, dismissible line for an account that was set up before the tour existed.
-->
<script lang="ts">
  import { Button, Icon } from '@mowdeck/ui';

  import { session } from '#lib/auth/session.svelte.js';
  import { m } from '#lib/paraglide/messages.js';

  import { stepHref, stepText } from './labels';
  import { onboarding } from './runtime';
  import StepMeter from './StepMeter.svelte';

  $effect(() => {
    if (session.status !== 'authenticated') return;
    void onboarding.load().then(() => {
      if (onboarding.status === 'new' || onboarding.status === 'active') void onboarding.refresh();
    });
  });

  // A new account that may add mowers and has none: the tour starts by itself, once.
  $effect(() => {
    if (onboarding.status === 'new' && onboarding.offer === 'tour') void onboarding.start();
  });

  const next = $derived(onboarding.next);
  const fresh = $derived(onboarding.facts.mowers === 0);
</script>

{#if onboarding.offer === 'tour'}
  <section class="setup" aria-labelledby="setup-card-title" data-testid="setup-card">
    <div class="bar">
      <span class="label">{m.onboarding_title()}</span>
      <Button variant="ghost" testid="setup-card-later" onclick={() => void onboarding.dismiss()}
        >{m.onboarding_card_later()}</Button
      >
    </div>
    {#if onboarding.factsLoaded}
      <StepMeter steps={onboarding.steps} testid="setup-card-meter" />
      {#if next}
        {@const text = stepText(next.key)}
        <p class="eyebrow">{fresh ? m.onboarding_card_welcome() : m.onboarding_card_next()}</p>
        <h2 id="setup-card-title" data-step={next.key}>{text.title}</h2>
        <p class="text">{text.text}</p>
        <div class="actions">
          <Button
            variant="primary"
            href={stepHref(next.key, onboarding.facts)}
            testid="setup-card-action">{text.action}</Button
          >
          <Button href="/setup" icon="list" testid="setup-card-open"
            >{m.onboarding_card_open()}</Button
          >
        </div>
      {:else}
        <h2 id="setup-card-title">{m.onboarding_card_done_title()}</h2>
        <p class="text">{m.onboarding_card_done_text()}</p>
        <div class="actions">
          <Button
            variant="primary"
            icon="check"
            testid="setup-card-finish"
            onclick={() => void onboarding.finish()}>{m.onboarding_finish()}</Button
          >
        </div>
      {/if}
    {:else}
      <h2 id="setup-card-title" class="md-visually-hidden">{m.onboarding_title()}</h2>
      <div class="placeholder" aria-hidden="true"></div>
    {/if}
  </section>
{:else if onboarding.offer === 'hint'}
  <div class="hint" data-testid="setup-hint">
    <Icon name="list" size={18} />
    <span class="hint-text">{m.onboarding_hint_setup()}</span>
    <Button variant="ghost" href="/setup" testid="setup-hint-open"
      >{m.onboarding_hint_open()}</Button
    >
    <Button
      variant="ghost"
      icon="close"
      label={m.dismiss()}
      testid="setup-hint-dismiss"
      onclick={() => void onboarding.dismiss()}
    />
  </div>
{/if}

<style>
  .setup {
    display: grid;
    gap: var(--md-space-2);
    margin: var(--md-space-4) var(--md-gutter);
    padding: var(--md-space-3) var(--md-space-4) var(--md-space-4);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-inline-start: var(--md-space-1) solid var(--md-color-ink);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    animation: settle var(--md-motion-base) var(--md-ease);
  }

  .bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-inline-end: calc(-1 * var(--md-space-2));
  }

  .label,
  .eyebrow {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .eyebrow {
    margin-top: var(--md-space-2);
  }

  h2 {
    margin: 0;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-semibold);
    line-height: var(--md-line-28);
    letter-spacing: normal;
    text-transform: none;
  }

  .text {
    max-width: 60ch;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    margin-top: var(--md-space-2);
  }

  .placeholder {
    height: var(--md-target);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface-sunken);
  }

  .hint {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    padding: 0 var(--md-space-2) 0 var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-14);
  }

  .hint-text {
    flex: 1;
  }

  @keyframes settle {
    from {
      transform: translateY(4px);
      opacity: 0;
    }
  }

  @media (min-width: 1024px) {
    .setup {
      max-width: 760px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .setup {
      animation: none;
    }
  }
</style>
