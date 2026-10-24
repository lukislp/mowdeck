<!--
  One plan limit as an instrument row: the limit's name, the use against the limit in monospaced
  figures ("3 / 4", or the word for unlimited) and a ten-segment gauge. Segments fill with the
  share used; the last free segments turn amber, a reached limit turns the gauge red. An unlimited
  or uncounted limit shows a hatched gauge.
-->
<script lang="ts">
  import { formatNumber } from '#lib/i18n.js';
  import { limitLabel } from '#lib/plan/labels.js';
  import type { LimitRow } from '#lib/plan/usage.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    row: LimitRow;
    /** The unit after a value without use (e.g. days of history). */
    unit?: string;
  }
  let { row, unit }: Props = $props();

  const SEGMENTS = 10;
  const filled = $derived(row.share === null ? 0 : Math.ceil(row.share * SEGMENTS));
  const tone = $derived(
    row.reached ? 'error' : row.share !== null && row.share >= 0.75 ? 'warn' : 'ok',
  );
  const limitText = $derived(row.limit === null ? m.plan_unlimited() : formatNumber(row.limit));
</script>

<li
  class="meter"
  data-testid="plan-limit-{row.key}"
  data-limit={row.limit ?? 'unlimited'}
  data-used={row.used ?? ''}
  data-tone={tone}
>
  <span class="name">{limitLabel(row.key)}</span>
  <span class="figure">
    {#if row.used !== null}
      <span class="used">{formatNumber(row.used)}</span>
      <span class="sep" aria-hidden="true">/</span>
    {/if}
    <span class="limit" class:word={row.limit === null}>{limitText}</span>
    {#if row.used === null && row.limit !== null && unit}<span class="unit">{unit}</span>{/if}
  </span>
  <span class="gauge" class:open={row.share === null} aria-hidden="true">
    {#each { length: SEGMENTS }, index (index)}
      <span class="seg" class:on={index < filled}></span>
    {/each}
  </span>
  {#if row.reached}<span class="flag">{m.plan_limit_reached()}</span>{/if}
</li>

<style>
  .meter {
    --tone: var(--md-color-ink);

    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas:
      'name figure'
      'gauge gauge'
      'flag flag';
    gap: var(--md-space-1) var(--md-space-3);
    padding: var(--md-space-3) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .meter[data-tone='warn'] {
    --tone: var(--md-state-paused);
  }

  .meter[data-tone='error'] {
    --tone: var(--md-state-error);
  }

  .name {
    grid-area: name;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    align-self: end;
  }

  .figure {
    grid-area: figure;
    display: inline-flex;
    align-items: baseline;
    gap: var(--md-space-1);
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .used {
    color: var(--tone);
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-medium);
  }

  .sep,
  .unit {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .limit {
    font-size: var(--md-font-size-16);
  }

  .limit.word {
    font-family: var(--md-font-sans);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .gauge {
    grid-area: gauge;
    display: grid;
    grid-template-columns: repeat(10, 1fr);
    gap: var(--md-space-0_5);
    height: 8px;
  }

  .seg {
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface-sunken);
  }

  .seg.on {
    border-color: var(--tone);
    background: var(--tone);
  }

  .gauge.open .seg {
    border-style: dashed;
    border-color: var(--md-color-line);
    background: repeating-linear-gradient(135deg, var(--md-color-line) 0 1px, transparent 1px 4px);
  }

  .flag {
    grid-area: flag;
    color: var(--md-state-error);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }
</style>
