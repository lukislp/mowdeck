<!--
  Einrichtung: the way back into the setup tour (ADR 0062) for every user, with its status and
  progress, and the guided tour (ADR 0073): its outcome and "Start the tour" (from its first
  step, whatever happened before). Opening the setup tour resumes a hidden one.
-->
<script lang="ts">
  import { Button, Panel, Readout } from '@mowdeck/ui';

  import { statusLabel } from '#lib/onboarding/labels.js';
  import { onboarding } from '#lib/onboarding/runtime.js';
  import StepMeter from '#lib/onboarding/StepMeter.svelte';
  import { m } from '#lib/paraglide/messages.js';
  import { outcomeLabel } from '#lib/tour/labels.js';
  import { tour } from '#lib/tour/runtime.js';

  $effect(() => {
    void onboarding.load();
    void onboarding.refresh();
    void tour.loadOutcome();
  });

  const running = $derived(onboarding.status === 'active');
</script>

<Panel label={m.tour_section_label()} testid="tour-panel">
  <div class="stack">
    <p class="text">{m.tour_section_text()}</p>
    {#if tour.outcome}
      <Readout
        label={m.onboarding_section_status()}
        value={outcomeLabel(tour.outcome)}
        size="inline"
        emptyText={m.no_data()}
        testid="tour-status"
      />
    {/if}
    <div class="tour-start" data-tour="tour-restart">
      <Button
        variant={tour.outcome === 'new' ? 'primary' : 'secondary'}
        icon="crosshair"
        testid="tour-start"
        onclick={() => void tour.start()}>{m.tour_start()}</Button
      >
    </div>
  </div>
</Panel>

<Panel label={m.onboarding_title()} testid="setup-panel">
  <div class="stack">
    <p class="text">{m.onboarding_section_text()}</p>
    {#if onboarding.loaded && !onboarding.failed}
      <Readout
        label={m.onboarding_section_status()}
        value={statusLabel(onboarding.status)}
        size="inline"
        emptyText={m.no_data()}
        testid="setup-status"
      />
    {/if}
    {#if onboarding.factsLoaded}
      <StepMeter steps={onboarding.steps} />
    {/if}
    <div>
      <Button
        href="/setup"
        variant={running ? 'primary' : 'secondary'}
        icon="list"
        testid="setup-open"
        >{running ? m.onboarding_section_resume() : m.onboarding_section_open()}</Button
      >
    </div>
  </div>
</Panel>

<style>
  .stack {
    display: grid;
    gap: var(--md-space-3);
    padding: 0 var(--md-gutter) var(--md-space-2);
  }

  .text {
    max-width: 60ch;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  /* Only as wide as its button: the tour frames exactly this. */
  .tour-start {
    justify-self: start;
  }
</style>
