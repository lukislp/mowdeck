<!--
  The forecast hours of a rain decision as a strip of bars: height is the precipitation
  probability, hours that reach a threshold of the rain pause are in the paused tone (with a
  drop mark, never colour alone), the hours of the next firing's window are underlined, and a
  dashed line marks the probability threshold. `summary` is the accessible description.
-->
<script lang="ts">
  import type { HourBar } from '#lib/schedules/preview.js';

  interface Props {
    bars: HourBar[];
    /** Probability threshold in percent (the dashed line). */
    threshold: number;
    /** The firing's window, to underline its hours. */
    window?: { start: string; end: string } | null;
    /** Formats an hour's start for the axis ("07"). */
    hourLabel: (iso: string) => string;
    summary: string;
    testid?: string;
    /** Epoch ms of "now": past hours are dimmed and the current hour carries a mark. */
    now?: number | null;
    /** Label of the now mark ("jetzt"). */
    nowLabel?: string;
  }
  let {
    bars,
    threshold,
    window = null,
    hourLabel,
    summary,
    testid,
    now = null,
    nowLabel = '',
  }: Props = $props();

  const isPast = (bar: HourBar) => now !== null && Date.parse(bar.end) <= now;
  const nowIndex = $derived(
    now === null
      ? -1
      : bars.findIndex((b) => Date.parse(b.start) <= now && now < Date.parse(b.end)),
  );

  const inWindow = (bar: HourBar) =>
    !!window &&
    Date.parse(bar.end) > Date.parse(window.start) &&
    Date.parse(bar.start) < Date.parse(window.end);
  const ticks = $derived(
    bars.length
      ? nowIndex > 0
        ? [0, nowIndex, bars.length - 1]
        : [0, Math.floor((bars.length - 1) / 2), bars.length - 1]
      : [],
  );
</script>

<figure class="strip" data-testid={testid} role="img" aria-label={summary}>
  <div class="plot" style:--threshold="{Math.min(100, Math.max(0, threshold))}%">
    <span class="threshold" aria-hidden="true"></span>
    {#each bars as bar, i (bar.start)}
      <span
        class="bar"
        class:wet={bar.wet}
        class:window={inWindow(bar)}
        class:missing={bar.probability === null}
        class:past={isPast(bar)}
        class:now={i === nowIndex}
        data-wet={bar.wet}
        style:--h="{Math.max(4, Math.round((bar.probability ?? 0) * 100))}%"
        aria-hidden="true"
      >
        {#if bar.wet}<span class="drop"></span>{/if}
        {#if ticks.includes(i)}<span class="tick"
            >{i === nowIndex && nowLabel ? nowLabel : hourLabel(bar.start)}</span
          >{/if}
      </span>
    {/each}
  </div>
</figure>

<style>
  .strip {
    margin: 0;
    padding: var(--md-space-3) var(--md-space-3) var(--md-space-6);
    background: var(--md-color-surface-sunken);
  }

  .plot {
    position: relative;
    display: flex;
    align-items: flex-end;
    gap: 2px;
    height: 56px;
    border-bottom: var(--md-hairline) solid var(--md-color-line-strong);
  }

  .threshold {
    position: absolute;
    z-index: 1;
    inset-inline: 0;
    bottom: var(--threshold);
    border-top: var(--md-hairline) dashed var(--md-state-paused);
  }

  .bar {
    position: relative;
    flex: 1;
    height: var(--h);
    min-width: 2px;
    background: var(--md-color-line-strong);
  }

  .bar.missing {
    background: none;
    border: var(--md-hairline) dashed var(--md-color-line);
  }

  .bar.past {
    opacity: 0.45;
  }

  .bar.now::before {
    content: '';
    position: absolute;
    inset-block: -6px 0;
    inset-inline-start: -2px;
    border-inline-start: var(--md-hairline) solid var(--md-color-ink);
  }

  .bar.wet {
    background: var(--md-state-paused);
  }

  .bar.window::after {
    content: '';
    position: absolute;
    inset-inline: 0;
    bottom: -6px;
    height: 3px;
    background: var(--md-color-ink);
  }

  .drop {
    position: absolute;
    top: -8px;
    left: 50%;
    width: 4px;
    height: 4px;
    margin-left: -2px;
    border-radius: var(--md-radius-round);
    background: var(--md-state-paused);
  }

  .tick {
    position: absolute;
    top: calc(100% + 10px);
    left: 0;
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    font-variant-numeric: tabular-nums;
    line-height: var(--md-line-16);
    white-space: nowrap;
  }
</style>
