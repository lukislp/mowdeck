<!--
  The setup tour (ADR 0062): every step that applies to this user and device as a numbered
  checklist with its state read from the real data (done, skipped, open). The next open step is
  expanded with what it is for, its action (the flow that already exists for it) and "Skip";
  a skipped step can be resumed. Opened from the mower list, Settings → Einrichtung or a link;
  opening it resumes a hidden tour. The facts are read again when the page comes back into view.
-->
<script lang="ts">
  import { Button, Icon, ProblemNotice, TopBar } from '@mowdeck/ui';
  import { tick } from 'svelte';

  import { goto } from '$app/navigation';

  import { session } from '#lib/auth/session.svelte.js';
  import { install } from '#lib/stores/ui.svelte.js';
  import { nativeShell } from '#lib/native/runtime.js';
  import { stateLabel, stepHref, stepText } from '#lib/onboarding/labels.js';
  import { onboarding } from '#lib/onboarding/runtime.js';
  import StepMeter from '#lib/onboarding/StepMeter.svelte';
  import { settledCount, type StepKey } from '#lib/onboarding/steps.js';
  import { m } from '#lib/paraglide/messages.js';

  /** The step the user expanded; null follows the next open step. */
  let chosen = $state<StepKey | null>(null);
  let completeHeading = $state<HTMLHeadingElement | null>(null);

  $effect(() => {
    if (session.status !== 'authenticated') return;
    void onboarding.load().then(() => {
      if (onboarding.failed) return;
      // Opening the tour on purpose resumes it.
      if (onboarding.status === 'new' || onboarding.status === 'dismissed') {
        void onboarding.start();
      }
    });
    void onboarding.refresh();
  });

  // Back from a step's flow in another tab or app: read the facts again.
  $effect(() => {
    const again = () => {
      if (document.visibilityState === 'visible') void onboarding.refresh();
    };
    document.addEventListener('visibilitychange', again);
    window.addEventListener('focus', again);
    return () => {
      document.removeEventListener('visibilitychange', again);
      window.removeEventListener('focus', again);
    };
  });

  const steps = $derived(onboarding.steps);
  const expanded = $derived(
    chosen && steps.some((step) => step.key === chosen) ? chosen : (onboarding.next?.key ?? null),
  );
  const complete = $derived(onboarding.complete);
  const iosBrowser = $derived(install.offer === 'ios' && !nativeShell.active);

  // The last step settled: move the focus to the result, so it is announced.
  let wasComplete = false;
  $effect(() => {
    if (complete && !wasComplete && onboarding.factsLoaded) {
      void tick().then(() => completeHeading?.focus());
    }
    wasComplete = complete;
  });

  function toggle(key: StepKey): void {
    chosen = expanded === key ? null : key;
  }

  async function settle(action: Promise<void>): Promise<void> {
    chosen = null;
    await action;
    await tick();
    // The last required step settled: the completion panel takes the focus (effect above).
    if (onboarding.complete) return;
    const next = onboarding.next;
    if (next) document.getElementById(`setup-step-${next.key}-toggle`)?.focus();
  }

  async function end(): Promise<void> {
    await onboarding.dismiss();
    await goto('/mowers');
  }

  async function finish(): Promise<void> {
    await onboarding.finish();
    await goto('/mowers');
  }
</script>

<svelte:head><title>{m.onboarding_title()} · {m.app_name()}</title></svelte:head>

<TopBar
  title={m.onboarding_title()}
  meta={onboarding.factsLoaded
    ? m.onboarding_progress({ done: settledCount(steps), total: steps.length })
    : undefined}
  backHref="/mowers"
  backLabel={m.back()}
/>

<div class="setup" data-testid="setup-page" data-status={onboarding.status}>
  <header class="intro">
    {#if onboarding.factsLoaded}
      <StepMeter {steps} testid="setup-meter" />
    {/if}
    <p>{m.onboarding_intro()}</p>
  </header>

  {#if onboarding.failed}
    <div class="notice">
      <ProblemNotice title={m.onboarding_loading_failed()} />
    </div>
  {/if}

  {#if complete}
    <section class="complete" aria-labelledby="setup-complete-title" data-testid="setup-complete">
      <span class="seal" aria-hidden="true"><Icon name="check" size={20} /></span>
      <div>
        <h2 id="setup-complete-title" tabindex="-1" bind:this={completeHeading}>
          {m.onboarding_complete_title()}
        </h2>
        <p>{m.onboarding_complete_text()}</p>
      </div>
      <Button variant="primary" testid="setup-finish" onclick={() => void finish()}
        >{m.onboarding_complete_action()}</Button
      >
    </section>
  {/if}

  {#if onboarding.factsLoaded}
    <ol class="steps" data-testid="setup-steps">
      {#each steps as step, index (step.key)}
        {@const text = stepText(step.key)}
        {@const open = expanded === step.key}
        <li class="step" data-state={step.state} data-testid="setup-step-{step.key}">
          <h2 class="step-head">
            <button
              type="button"
              class="toggle"
              id="setup-step-{step.key}-toggle"
              aria-expanded={open}
              aria-controls="setup-step-{step.key}-panel"
              onclick={() => toggle(step.key)}
            >
              <span class="num" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
              <span class="glyph" data-state={step.state} aria-hidden="true">
                {#if step.state === 'done'}
                  <Icon name="check" size={14} />
                {:else if step.state === 'skipped'}
                  <Icon name="skip" size={14} />
                {/if}
              </span>
              <span class="title">{text.title}</span>
              <span class="state" data-testid="setup-step-{step.key}-state"
                >{stateLabel(step.state)}{#if step.optional}<span class="optional"
                    >{m.onboarding_optional()}</span
                  >{/if}</span
              >
            </button>
          </h2>
          {#if open}
            <div
              class="panel"
              id="setup-step-{step.key}-panel"
              role="region"
              aria-labelledby="setup-step-{step.key}-toggle"
            >
              <p>{text.text}</p>
              {#if step.key === 'notifications' && iosBrowser}
                <p class="note">
                  {m.onboarding_step_notifications_ios()}
                  <a href="/settings#profile">{m.onboarding_step_notifications_ios_link()}</a>
                </p>
              {/if}
              <div class="actions">
                {#if step.state !== 'done'}
                  <Button
                    variant={step.state === 'open' ? 'primary' : 'secondary'}
                    href={stepHref(step.key, onboarding.facts)}
                    testid="setup-step-{step.key}-action">{text.action}</Button
                  >
                {/if}
                {#if step.state === 'open'}
                  <Button
                    variant="ghost"
                    testid="setup-step-{step.key}-skip"
                    onclick={() => void settle(onboarding.skip(step.key))}
                    >{m.onboarding_skip()}</Button
                  >
                {:else if step.state === 'skipped'}
                  <Button
                    testid="setup-step-{step.key}-resume"
                    onclick={() => void onboarding.unskip(step.key)}>{m.onboarding_resume()}</Button
                  >
                {/if}
              </div>
            </div>
          {/if}
        </li>
      {/each}
    </ol>
  {:else if !onboarding.failed}
    <div class="loading" aria-hidden="true">
      {#each [0, 1, 2, 3] as row (row)}<span></span>{/each}
    </div>
  {/if}

  {#if !complete}
    <footer class="end">
      <Button variant="ghost" testid="setup-end" onclick={() => void end()}
        >{m.onboarding_end()}</Button
      >
      <p>{m.onboarding_end_hint()}</p>
    </footer>
  {/if}
</div>

<style>
  .setup {
    max-width: 760px;
    padding-bottom: var(--md-space-8);
  }

  .intro {
    display: grid;
    gap: var(--md-space-3);
    padding: var(--md-space-4) var(--md-gutter);
  }

  .intro p {
    max-width: 60ch;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .notice {
    padding: 0 var(--md-gutter) var(--md-space-4);
  }

  .complete {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--md-space-3);
    align-items: start;
    margin: 0 var(--md-gutter) var(--md-space-4);
    padding: var(--md-space-4);
    border: var(--md-hairline) solid var(--md-state-mowing);
    border-radius: var(--md-radius-2);
    background: var(--md-state-mowing-bg);
  }

  .complete :global(.md-button) {
    grid-column: 1 / -1;
    justify-self: start;
  }

  .complete h2 {
    margin: 0;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-28);
    letter-spacing: normal;
    text-transform: none;
  }

  .complete h2:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .complete p {
    margin: var(--md-space-1) 0 0;
    font-size: var(--md-font-size-14);
  }

  .seal {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    border-radius: var(--md-radius-round);
    background: var(--md-state-mowing);
    color: var(--md-color-ink-inverse);
  }

  .steps {
    margin: 0;
    padding: 0;
    border-top: var(--md-hairline) solid var(--md-color-line-strong);
    list-style: none;
  }

  .step {
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .step-head {
    margin: 0;
    font-size: inherit;
    font-weight: inherit;
  }

  .toggle {
    display: grid;
    grid-template-columns: 2ch 20px minmax(0, 1fr) auto;
    gap: var(--md-space-3);
    align-items: center;
    width: 100%;
    min-height: var(--md-target-command);
    padding: var(--md-space-2) var(--md-gutter);
    border: 0;
    background: none;
    color: var(--md-color-ink);
    font: inherit;
    text-align: start;
    cursor: pointer;
  }

  .toggle:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .toggle[aria-expanded='true'] {
    background: var(--md-color-surface);
  }

  .num {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    font-variant-numeric: tabular-nums slashed-zero;
  }

  .glyph {
    display: grid;
    place-items: center;
    width: 20px;
    height: 20px;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-round);
    color: var(--md-color-ink-inverse);
  }

  .glyph[data-state='done'] {
    border-color: var(--md-state-mowing);
    background: var(--md-state-mowing);
  }

  .glyph[data-state='skipped'] {
    border-style: dashed;
    color: var(--md-color-ink-muted);
  }

  .title {
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-semibold);
    line-height: var(--md-line-24);
  }

  .step[data-state='done'] .title {
    color: var(--md-color-ink-muted);
  }

  .state {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .step[data-state='done'] .state {
    color: var(--md-state-mowing);
  }

  .optional {
    font-weight: var(--md-weight-regular);
  }

  .panel {
    display: grid;
    gap: var(--md-space-3);
    padding: 0 var(--md-gutter) var(--md-space-4)
      calc(var(--md-gutter) + 2ch + 20px + 2 * var(--md-space-3));
    background: var(--md-color-surface);
    animation: open var(--md-motion-base) var(--md-ease);
  }

  .panel p {
    max-width: 60ch;
    margin: 0;
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .note {
    color: var(--md-color-ink-muted);
  }

  .note a {
    color: inherit;
    text-underline-offset: 3px;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .loading {
    display: grid;
    gap: var(--md-space-1);
    padding: 0 var(--md-gutter);
  }

  .loading span {
    height: var(--md-target-command);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface-sunken);
  }

  .end {
    display: grid;
    justify-items: start;
    gap: var(--md-space-1);
    padding: var(--md-space-4) var(--md-gutter);
  }

  .end p {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  @keyframes open {
    from {
      opacity: 0;
      transform: translateY(-4px);
    }
  }

  /* Phones: the panel uses the full width under the step. */
  @media (max-width: 599px) {
    .panel {
      padding-inline-start: var(--md-gutter);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .panel {
      animation: none;
    }
  }
</style>
