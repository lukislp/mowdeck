<!--
  The run bar of a spot run, above the command bar next to the emergency stop: while the gateway
  drives the run, its state as word and lamp, the progress bar with the percentage, the segment
  i of n, the RTK fix and whether the blades run (or a dry run), the pause or abort reason in
  words, and a big STOP that sends `spot_stop` (a controlled stop: zero frame, blades off; the
  emergency stop below stays the last resort). After the run, a notice with the end state (done,
  aborted with its reason, failed), the progress reached, "Nachmähen" again and "Ausblenden".
  Between a start and the run's first status (`launching`) it says the run is starting and waits
  for live positions, with STOP already there. `embedded` drops the bar's own edge and padding
  (inside the phone bottom sheet's peek).
-->
<script lang="ts">
  import { Button, Icon, type RtkFix } from '@mowdeck/ui';

  import { formatNumber, rtkFixShort } from '$lib/i18n';
  import { spotReasonLabel, spotRunLabel, type SpotRunStatus } from '$lib/mower/spot-sheet';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    /** The run being driven, or null. */
    run: SpotRunStatus | null;
    /** A run that ended while this page watched it, or null. */
    ended: SpotRunStatus | null;
    rtkFix: string | null | undefined;
    /** The mower drives over the Bluetooth bridge (a lost drive link names it). */
    bridge?: boolean;
    stopping: boolean;
    onstop: () => void;
    /** "Nachmähen" again is offered when given. */
    onremow?: (() => void) | null;
    ondismiss: () => void;
    /** A start went out and the run has not shown up yet. */
    launching?: boolean;
    /** Inside another container (the bottom sheet): no edge, no padding of its own. */
    embedded?: boolean;
  }
  let {
    run,
    ended,
    rtkFix,
    bridge = false,
    stopping,
    onstop,
    onremow = null,
    ondismiss,
    launching = false,
    embedded = false,
  }: Props = $props();

  const tone = $derived(
    run?.state === 'spot_mowing'
      ? 'mowing'
      : run?.state === 'paused_rtk'
        ? 'paused'
        : run?.state === 'stopping'
          ? 'error'
          : 'returning',
  );
  const points = $derived(
    run
      ? m.path_status_done({
          done: formatNumber(run.driven_points),
          total: formatNumber(run.planned_points),
        })
      : '',
  );
</script>

{#snippet stopButton()}
  <button
    type="button"
    class="stop"
    disabled={stopping}
    aria-busy={stopping ? 'true' : undefined}
    title={m.spot_stop_hint()}
    data-testid="spot-stop"
    onclick={onstop}
  >
    <Icon name="stop" size={22} />
    <span>{m.spot_stop()}</span>
  </button>
{/snippet}

{#if run}
  <div class="run" class:embedded data-testid="spot-run" data-state={run.state} data-tone={tone}>
    <div class="info">
      <div class="head">
        <span class="lamp" aria-hidden="true"></span>
        <span class="state">{spotRunLabel(run.state)}</span>
        <span class="mono pct" data-testid="spot-run-percent">{run.progress_percent} %</span>
      </div>
      <div class="track" aria-hidden="true">
        <span class="fill" style:inline-size="{run.progress_percent}%"></span>
      </div>
      <div class="meta mono">
        <span data-testid="spot-run-segment">
          {m.spot_run_segment({
            i: formatNumber(Math.min(run.segment_index + 1, Math.max(run.segments_total, 1))),
            n: formatNumber(run.segments_total),
          })}
        </span>
        <span>{points}</span>
        <span class="badge" data-rtk={rtkFix ?? 'unknown'} data-testid="spot-run-rtk"
          >{rtkFixShort((rtkFix ?? 'unknown') as RtkFix)}</span
        >
        <span class="badge" data-blades={run.dry_run ? 'off' : 'on'} data-testid="spot-run-blades"
          >{run.dry_run ? m.spot_run_dry() : m.spot_run_blades_on()}</span
        >
        {#if spotReasonLabel(run.reason, { bridge })}
          <span class="reason" data-testid="spot-run-reason"
            >{spotReasonLabel(run.reason, { bridge })}</span
          >
        {/if}
      </div>
    </div>
    {@render stopButton()}
  </div>
{:else if launching}
  <div
    class="run"
    class:embedded
    data-testid="spot-run-launching"
    data-state="launching"
    data-tone="returning"
  >
    <div class="info">
      <div class="head">
        <span class="lamp" aria-hidden="true"></span>
        <span class="state">{m.spot_run_preparing()}</span>
      </div>
      <p class="wait" role="status">{m.spot_launching()}</p>
    </div>
    {@render stopButton()}
  </div>
{:else if ended}
  <div class="end" class:embedded data-testid="spot-end" data-state={ended.state}>
    <span class="glyph" aria-hidden="true">
      <Icon name={ended.state === 'done' ? 'check' : 'alert'} size={18} />
    </span>
    <div class="text">
      <strong>
        {ended.state === 'done'
          ? m.spot_end_done()
          : ended.state === 'aborted'
            ? m.spot_end_aborted()
            : m.spot_end_failed()}
      </strong>
      {#if spotReasonLabel(ended.reason, { bridge })}
        <span data-testid="spot-end-reason"
          >{m.spot_end_reason({ reason: spotReasonLabel(ended.reason, { bridge }) ?? '' })}</span
        >
      {/if}
      <span class="mono">
        {ended.progress_percent} % · {m.path_status_done({
          done: formatNumber(ended.driven_points),
          total: formatNumber(ended.planned_points),
        })} · {ended.dry_run ? m.spot_run_dry() : m.spot_run_blades_on()}
      </span>
    </div>
    <div class="end-actions">
      {#if onremow}
        <Button icon="crosshair" onclick={onremow} testid="spot-remow">{m.heat_gap_remow()}</Button>
      {/if}
      <button
        type="button"
        class="dismiss"
        aria-label={m.spot_dismiss()}
        data-testid="spot-dismiss"
        onclick={ondismiss}
      >
        <Icon name="close" size={18} />
      </button>
    </div>
  </div>
{/if}

<style>
  .run {
    --tone: var(--md-state-returning);

    display: flex;
    align-items: stretch;
    gap: var(--md-space-3);
    padding: var(--md-space-2) var(--md-gutter);
    border-top: var(--md-border-control) solid var(--tone);
    background: var(--md-color-surface);
  }

  .run[data-tone='mowing'] {
    --tone: var(--md-state-mowing);
  }

  .run[data-tone='paused'] {
    --tone: var(--md-state-paused);
  }

  .run[data-tone='error'] {
    --tone: var(--md-state-error);
  }

  .info {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .head {
    display: flex;
    align-items: baseline;
    gap: var(--md-space-2);
  }

  .lamp {
    align-self: center;
    width: 10px;
    height: 10px;
    border-radius: var(--md-radius-round);
    background: var(--tone);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--tone) 25%, transparent);
  }

  .run[data-state='spot_mowing'] .lamp,
  .run[data-state='launching'] .lamp {
    animation: pulse 1.6s var(--md-ease) infinite;
  }

  .wait {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .run.embedded,
  .end.embedded {
    padding: 0;
    border-top: 0;
    background: none;
  }

  .state {
    flex: 1 1 auto;
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .pct {
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-24);
  }

  .track {
    position: relative;
    height: 6px;
    overflow: hidden;
    background: var(--md-color-surface-sunken);
  }

  .fill {
    display: block;
    height: 100%;
    background: var(--tone);
    transition: inline-size var(--md-motion-base) var(--md-ease);
  }

  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .badge {
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid currentcolor;
    color: var(--md-color-ink);
    text-transform: uppercase;
  }

  .badge[data-rtk='fixed'] {
    color: var(--md-rtk-fixed);
  }

  .badge[data-rtk='float'] {
    color: var(--md-rtk-float);
  }

  .badge[data-rtk='none'] {
    color: var(--md-rtk-none);
  }

  .badge[data-blades='on'] {
    color: var(--md-state-error);
  }

  .reason {
    color: var(--md-color-ink);
  }

  /* The STOP: ink filled, as large as a command, the stop glyph on top of the word. */
  .stop {
    display: flex;
    flex: none;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--md-space-0_5);
    min-width: 88px;
    min-height: var(--md-target-command);
    padding: 0 var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-ink);
    border-radius: var(--md-radius-2);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
  }

  .stop:disabled {
    opacity: 0.6;
    cursor: progress;
  }

  .stop:focus-visible,
  .dismiss:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .end {
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
    padding: var(--md-space-2) var(--md-gutter);
    border-top: var(--md-border-control) solid var(--md-state-mowing);
    background: var(--md-color-surface);
  }

  .end[data-state='aborted'] {
    border-top-color: var(--md-state-paused);
  }

  .end[data-state='failed'] {
    border-top-color: var(--md-state-error);
  }

  .glyph {
    display: grid;
    flex: none;
    place-items: center;
    width: 32px;
    height: 32px;
    border-radius: var(--md-radius-round);
    background: var(--md-state-mowing-bg);
    color: var(--md-state-mowing);
  }

  .end[data-state='aborted'] .glyph {
    background: var(--md-state-paused-bg);
    color: var(--md-state-paused);
  }

  .end[data-state='failed'] .glyph {
    background: var(--md-state-error-bg);
    color: var(--md-state-error);
  }

  .text {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    min-width: 0;
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  .text .mono {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .end-actions {
    display: flex;
    flex: none;
    align-items: center;
    gap: var(--md-space-1);
  }

  .dismiss {
    display: grid;
    place-items: center;
    width: var(--md-target);
    height: var(--md-target);
    border: 0;
    background: none;
    color: var(--md-color-ink-muted);
    cursor: pointer;
  }

  @keyframes pulse {
    50% {
      box-shadow: 0 0 0 6px color-mix(in srgb, var(--tone) 10%, transparent);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .run[data-state='spot_mowing'] .lamp,
    .run[data-state='launching'] .lamp {
      animation: none;
    }
  }
</style>
