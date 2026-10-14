<!--
  The rain readout of the mower page's instrument column: what falls now (or "trocken"), the
  next hours as a strip, and the rain pause when the mower's schedule holds. Without site
  coordinates it says so and links to the site settings. Presentational: the page loads the
  weather (`GET /weather/{mower_id}`) and passes the parts in.
-->
<script lang="ts">
  import RainStrip from '$lib/components/RainStrip.svelte';
  import { formatClock, formatNumber } from '$lib/i18n';
  import { m } from '$lib/paraglide/messages';
  import { getLocale } from '$lib/paraglide/runtime';
  import type { HourBar } from '$lib/schedules/preview';
  import type { RainNow } from '$lib/mower/rain';

  interface Props {
    /** `ok`: data shown; `no_location`, `disabled` (provider off), `unavailable` (provider down). */
    status: 'ok' | 'no_location' | 'disabled' | 'unavailable';
    now: RainNow;
    bars: HourBar[];
    threshold: number;
    /**
     * `paused` says whether a pause holds; `pauseUntil` is the Scheduler's `decision.pause_until`
     * (ISO, or null when it does not know the end). Displayed as is, never computed here.
     */
    paused: boolean;
    pauseUntil: string | null;
    settingsHref: string;
    /** Epoch ms of now (past hours dimmed, now mark). */
    clock: number;
    /** The lawn is still wet; `wetUntil` is the estimated dry time. */
    wet?: boolean;
    wetUntil?: string | null;
  }
  let {
    status,
    now,
    bars,
    threshold,
    paused,
    pauseUntil,
    settingsHref,
    clock,
    wet = false,
    wetUntil = null,
  }: Props = $props();

  const time = (iso: string) => formatClock(iso);
  const hour = (iso: string) =>
    new Intl.DateTimeFormat(getLocale(), { hour: '2-digit' }).format(new Date(iso));
  const wettest = $derived(
    bars.reduce(
      (acc, b) => ({
        ...acc,
        p: Math.max(acc.p, b.probability ?? 0),
        mm: Math.max(acc.mm, b.mm ?? 0),
      }),
      { p: 0, mm: 0 },
    ),
  );
</script>

<div class="rain" data-testid="rain-readout" data-status={status}>
  {#if status === 'no_location'}
    <p class="line">
      <span class="value">{m.rain_no_location()}</span>
      <a href={settingsHref} data-testid="rain-site-link">{m.rain_set_site()}</a>
    </p>
  {:else if status === 'disabled'}
    <p class="line"><span class="quiet">{m.rain_disabled()}</span></p>
  {:else if status === 'unavailable'}
    <p class="line"><span class="quiet">{m.rain_unavailable()}</span></p>
  {:else}
    <p class="line">
      <span class="value" class:wet={now.raining} data-testid="rain-now">
        {#if now.raining}
          {m.rain_now()}
          {#if now.mm !== null}<span class="mono"
              >· {m.rain_now_mm({ mm: formatNumber(now.mm, 1) })}</span
            >{/if}
        {:else}
          {m.rain_dry()}
        {/if}
      </span>
      {#if now.probability !== null}
        <span class="mono quiet"
          >{m.rain_chance({ percent: formatNumber(Math.round(now.probability * 100)) })}</span
        >
      {/if}
    </p>
    {#if bars.length}
      <RainStrip
        {bars}
        {threshold}
        hourLabel={hour}
        summary={`${m.rain_strip_label({ count: bars.length })}: ${m.sched_strip_summary({
          percent: formatNumber(wettest.p * 100),
          mm: formatNumber(wettest.mm, 1),
        })}`}
        testid="rain-strip"
        now={clock}
        nowLabel={m.rain_now_tick()}
      />
    {/if}
    <p class="lawn" class:wet data-testid="rain-wet" data-wet={wet}>
      {#if wet && wetUntil}
        {m.rain_wet({ time: time(wetUntil) })}
      {:else}
        {m.rain_lawn_dry()}
      {/if}
    </p>
    {#if paused}
      <p class="pause" data-testid="rain-pause">
        {pauseUntil ? m.rain_pause_active({ time: time(pauseUntil) }) : m.rain_pause_active_open()}
      </p>
    {/if}
  {/if}
</div>

<style>
  .rain {
    display: grid;
    gap: var(--md-space-2);
  }

  .line {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-2);
    margin: 0;
  }

  .value {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
  }

  .value.wet {
    color: var(--md-state-paused);
  }

  .quiet {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .lawn {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .lawn.wet {
    color: var(--md-state-paused);
  }

  .pause {
    margin: 0;
    padding-inline-start: var(--md-space-2);
    border-inline-start: 2px solid var(--md-state-paused);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
  }

  a {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
  }
</style>
