<!--
  The tour's progress as an instrument scale: one segment per step, filled when done, hatched
  when skipped, a hairline when open; the count in mono beside it. Screen readers get the
  sentence ("3 von 7 erledigt"), not the segments.
-->
<script lang="ts">
  import { m } from '#lib/paraglide/messages.js';

  import { settledCount, type StepView } from './steps';

  let { steps, testid }: { steps: readonly StepView[]; testid?: string } = $props();

  const done = $derived(settledCount(steps));
</script>

<div class="meter" data-testid={testid} data-done={done} data-total={steps.length}>
  <span class="count" aria-hidden="true"
    >{String(done).padStart(2, '0')}<span class="of">/{String(steps.length).padStart(2, '0')}</span
    ></span
  >
  <span class="scale" aria-hidden="true">
    {#each steps as step (step.key)}
      <span class="seg" data-state={step.state}></span>
    {/each}
  </span>
  <span class="md-visually-hidden">{m.onboarding_progress({ done, total: steps.length })}</span>
</div>

<style>
  .meter {
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
  }

  .count {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-20);
    font-variant-numeric: tabular-nums slashed-zero;
    line-height: var(--md-line-24);
  }

  .of {
    color: var(--md-color-ink-muted);
  }

  .scale {
    display: flex;
    flex: 1;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .seg {
    flex: 1;
    height: 8px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    background: var(--md-color-surface);
    transition: background var(--md-motion-base) var(--md-ease);
  }

  .seg[data-state='done'] {
    border-color: var(--md-state-mowing);
    background: var(--md-state-mowing);
  }

  .seg[data-state='skipped'] {
    background: repeating-linear-gradient(
      -45deg,
      var(--md-color-line-strong) 0 1px,
      transparent 1px 4px
    );
  }

  @media (prefers-reduced-motion: reduce) {
    .seg {
      transition: none;
    }
  }
</style>
