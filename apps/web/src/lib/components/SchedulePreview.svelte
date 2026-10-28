<!--
  The preview of a saved schedule: the rain verdict for the next firing with the forecast strip,
  then the next firings (skipped ones struck through) in the schedule's time zone with a
  countdown to the first, each with the mowing angle it will use (rotation applied, ADR 0061);
  when the angles change from run to run a dial shows them. `dirty` says the form has unsaved
  changes the preview does not show.
-->
<script lang="ts">
  import { Icon, ProblemNotice } from '@mowdeck/ui';

  import type { ApiProblem } from '#lib/api/problem.js';
  import { clock } from '#lib/clock.svelte.js';
  import AngleDial from '#lib/components/AngleDial.svelte';
  import RainStrip from '#lib/components/RainStrip.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import ToneBadge from '#lib/components/ToneBadge.svelte';
  import { formatNumber, problemMessage } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import { verdictLook } from '#lib/schedules/labels.js';
  import { hourBars, parsePreview } from '#lib/schedules/preview.js';
  import { formatCountdown, formatFiring } from '#lib/schedules/recurrence.js';
  import { angleText } from '#lib/schedules/rotation.js';
  import type { Preview, RainPause } from '#lib/schedules/types.js';

  interface Props {
    preview: Preview | null;
    rain: RainPause;
    timeZone: string | null;
    problem?: ApiProblem | null;
    dirty?: boolean;
    /** Show the list of next firings (a new schedule has none yet). */
    firings?: boolean;
    testid?: string;
  }
  let {
    preview,
    rain,
    timeZone,
    problem = null,
    dirty = false,
    firings = true,
    testid,
  }: Props = $props();

  const view = $derived(parsePreview(preview));
  const look = $derived(verdictLook(view.badge));
  const bars = $derived(hourBars(view.hours, rain));
  const locale = $derived(getLocale());
  const wettest = $derived(
    bars.reduce(
      (max, bar) => ({
        p: Math.max(max.p, bar.probability ?? 0),
        mm: Math.max(max.mm, bar.mm ?? 0),
      }),
      { p: 0, mm: 0 },
    ),
  );

  /** The angles of the firings that run, in order. */
  const angles = $derived(
    view.items.flatMap((item) => (item.skipped || item.angle_deg == null ? [] : [item.angle_deg])),
  );
  const rotates = $derived(new Set(angles).size > 1);

  $effect(() => clock.hold(30_000));

  function hour(iso: string): string {
    try {
      return new Intl.DateTimeFormat(locale, {
        hour: '2-digit',
        timeZone: timeZone ?? undefined,
      }).format(new Date(iso));
    } catch {
      return new Intl.DateTimeFormat(locale, { hour: '2-digit' }).format(new Date(iso));
    }
  }

  function locationText(source: string | null): string {
    switch (source) {
      case 'site':
        return m.sched_location_site();
      case 'map':
        return m.sched_location_map();
      case 'position':
        return m.sched_location_position();
      default:
        return m.sched_location_none();
    }
  }
</script>

<div class="preview" data-testid={testid}>
  {#if problem}
    <ProblemNotice
      title={problemMessage(problem)}
      detail={problem.detail}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  {:else if !preview}
    <p class="quiet" role="status"><SlowLoading /></p>
  {:else}
    {#if dirty}
      <p class="note"><Icon name="alert" size={16} />{m.sched_preview_saved_state()}</p>
    {/if}

    <div class="verdict">
      <span class="label">{m.sched_rain_verdict()}</span>
      <ToneBadge {...look} value={view.badge} testid="preview-verdict" />
    </div>
    {#if view.decision}
      {#if bars.length}
        <RainStrip
          {bars}
          threshold={rain.probability_threshold}
          window={view.next ? { start: view.next.at, end: view.next.window_end } : null}
          hourLabel={hour}
          summary={m.sched_strip_summary({
            percent: formatNumber(wettest.p * 100),
            mm: formatNumber(wettest.mm, 1),
          })}
          testid="preview-strip"
        />
      {/if}
      <p class="meta mono">
        {locationText(view.decision.location_source)}
        {#if view.decision.latitude != null && view.decision.longitude != null}
          · {formatNumber(view.decision.latitude, 3)}, {formatNumber(view.decision.longitude, 3)}
        {/if}
        · {view.decision.provider}
      </p>
      {#if view.decision.attribution}
        <p class="meta">{view.decision.attribution}</p>
      {/if}
    {/if}

    {#if firings}
      <span class="label">{m.sched_next_firings()}</span>
      {#if rotates}
        <div class="rotation" data-testid="preview-rotation">
          <AngleDial
            {angles}
            size={88}
            label={m.sched_rotation_dial({
              angles: angles.map((a) => angleText(a, formatNumber)).join(', '),
            })}
          />
          <p class="sequence mono">
            {angles.map((a) => angleText(a, formatNumber)).join(' → ')}
          </p>
        </div>
      {/if}
      <ol class="firings" data-testid="preview-firings">
        {#each view.items as item, i (item.at)}
          <li class:skipped={item.skipped} data-skipped={item.skipped}>
            <span class="index mono">{i + 1}</span>
            <span class="when mono">{formatFiring(item.at, timeZone, locale)}</span>
            {#if item.angle_deg != null && !item.skipped}
              <span
                class="angle mono"
                title={m.sched_preview_angle({ angle: angleText(item.angle_deg, formatNumber) })}
                data-testid="preview-angle"
              >
                <svg viewBox="-8 -8 16 16" aria-hidden="true">
                  <g transform="rotate({-item.angle_deg})">
                    <line x1="-7" y1="-3.5" x2="7" y2="-3.5" />
                    <line x1="-7" y1="0" x2="7" y2="0" />
                    <line x1="-7" y1="3.5" x2="7" y2="3.5" />
                  </g>
                </svg>
                {angleText(item.angle_deg, formatNumber)}
              </span>
            {:else}
              <span></span>
            {/if}
            {#if item.skipped}
              <span class="flag">{m.sched_skipped()}</span>
            {:else if item === view.next}
              <span class="countdown mono">{formatCountdown(item.at, clock.now, locale)}</span>
            {:else}
              <span></span>
            {/if}
          </li>
        {:else}
          <li class="none">{m.sched_no_firings()}</li>
        {/each}
      </ol>
    {/if}
  {/if}
</div>

<style>
  .preview {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .note {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 3px solid var(--md-state-paused);
    background: var(--md-state-paused-bg);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .verdict {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .meta {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .firings {
    margin: 0;
    padding: 0;
    list-style: none;
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .firings li {
    display: grid;
    grid-template-columns: 24px minmax(0, 1fr) auto auto;
    align-items: center;
    gap: var(--md-space-2);
    min-height: 40px;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .index {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .when {
    font-size: var(--md-font-size-14);
  }

  .skipped .when {
    color: var(--md-color-ink-muted);
    text-decoration: line-through;
  }

  .flag {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .countdown {
    font-size: var(--md-font-size-12);
  }

  .rotation {
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
  }

  .sequence {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .angle {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 0 6px;
    border-radius: var(--md-radius-0);
    background: var(--md-state-mowing-bg);
    font-size: var(--md-font-size-12);
  }

  .angle svg {
    width: 14px;
    height: 14px;
  }

  .angle line {
    stroke: var(--md-state-mowing);
    stroke-width: 1.6;
    stroke-linecap: round;
  }

  .none {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }
</style>
