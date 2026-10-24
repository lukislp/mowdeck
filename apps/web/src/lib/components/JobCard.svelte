<!--
  The running mowing job on the mower page (`MowerState.job`): the progress as a big figure and
  a bar of twenty ticks (one per 5 %), the area the mower works in (its MowDeck name), and three
  instrument values: running for, remaining, mowed area. Shown only while a job runs; a paused
  job (or one an error interrupted) dims its ticks to the paused tone. The values come live over
  the socket; nothing here estimates.
-->
<script lang="ts">
  import { formatNumber } from '$lib/i18n';
  import { JOB_TICKS, formatRunTime, litTicks, type JobView } from '$lib/mower/job';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    job: JobView;
  }
  let { job }: Props = $props();

  /** Units are symbols, the same in every language. */
  const M2 = 'm²';

  const lit = $derived(litTicks(job.percent));
  const areaText = $derived(
    job.area.kind === 'area'
      ? job.area.name
      : job.area.kind === 'transit'
        ? m.job_transit()
        : m.job_area_unknown(),
  );
</script>

<section
  class="job"
  class:paused={job.paused}
  aria-label={m.job_title()}
  data-testid="job-card"
  data-paused={job.paused ? 'true' : 'false'}
>
  <header class="head">
    <span class="label">
      {m.job_title()}
      {#if job.paused}<span class="flag" data-testid="job-paused">· {m.job_paused()}</span>{/if}
    </span>
    {#if job.percent !== null}
      <span class="percent mono" data-testid="job-percent"
        >{formatNumber(job.percent)}<span class="unit">%</span></span
      >
    {/if}
  </header>

  <p
    class="area"
    class:dim={job.area.kind !== 'area'}
    data-testid="job-area"
    data-kind={job.area.kind}
  >
    {areaText}
  </p>

  {#if job.percent !== null}
    <div
      class="ticks"
      role="progressbar"
      aria-label={m.job_progress()}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={job.percent}
      data-testid="job-progress"
    >
      {#each { length: JOB_TICKS }, i (i)}
        <span class="tick" class:on={i < lit}></span>
      {/each}
    </div>
  {:else}
    <p class="hint" data-testid="job-progress-unknown">{m.job_progress_unknown()}</p>
  {/if}

  <dl class="values">
    <div>
      <dt>{m.job_elapsed()}</dt>
      <dd class="mono" data-testid="job-elapsed">
        {job.elapsedS !== null ? formatRunTime(job.elapsedS) : '—'}
      </dd>
    </div>
    <div>
      <dt>{m.job_remaining()}</dt>
      <dd class="mono" data-testid="job-remaining">
        {job.remainingS !== null ? formatRunTime(job.remainingS) : '—'}
      </dd>
    </div>
    <div>
      <dt>{m.job_mowed()}</dt>
      <dd class="mono" data-testid="job-mowed">
        {#if job.mowedM2 !== null}
          {formatNumber(job.mowedM2, job.mowedM2 < 100 ? 1 : 0)}<span class="unit">{M2}</span>
        {:else}
          —
        {/if}
      </dd>
    </div>
  </dl>
</section>

<style>
  .job {
    --job-tone: var(--md-state-mowing);

    display: grid;
    gap: var(--md-space-2);
    margin: 0 var(--md-gutter) var(--md-space-3);
    padding: var(--md-space-3);
    border-left: 3px solid var(--job-tone);
    background: var(--md-color-surface-sunken);
  }

  .job.paused {
    --job-tone: var(--md-state-paused);
  }

  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .label,
  dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .flag {
    color: var(--md-state-paused);
  }

  .mono {
    font-family: var(--md-font-mono);
  }

  .percent {
    font-size: var(--md-font-size-28);
    line-height: var(--md-line-32);
    font-variant-numeric: tabular-nums;
    color: var(--md-color-ink);
  }

  .unit {
    margin-left: 2px;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .area {
    margin: 0;
    font-size: var(--md-font-size-16);
    line-height: var(--md-line-20);
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  .area.dim,
  .hint {
    color: var(--md-color-ink-muted);
    font-weight: var(--md-weight-regular);
  }

  .hint {
    margin: 0;
    font-size: var(--md-font-size-13);
  }

  .ticks {
    display: grid;
    grid-template-columns: repeat(20, minmax(0, 1fr));
    gap: 3px;
    height: 12px;
  }

  .tick {
    background: var(--md-color-line);
  }

  .tick.on {
    background: var(--job-tone);
  }

  .values {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--md-space-2);
    margin: var(--md-space-1) 0 0;
  }

  dd {
    margin: 0;
    font-size: var(--md-font-size-16);
    line-height: var(--md-line-24);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  @media (prefers-reduced-motion: no-preference) {
    .tick {
      transition: background-color var(--md-motion-base) var(--md-ease);
    }
  }
</style>
