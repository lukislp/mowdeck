<!--
  "Fahrverhalten kalibrieren" in the mower settings (ADR 0042), under the manual driving section.
  Before a run: the start conditions as green and red rows (the same square lamps as the spot
  sheet), the free lawn ahead as an instruction the user confirms, the blades as information, and
  START. While the gateway measures: the state with its lamp, "Schritt i von n" with the step being
  driven, a segmented progress rail (one cell per step), the measured steps as they arrive and a
  big STOP (`spot_stop`). Afterwards: the end state with its reason, the result table and the fit
  with "Ergebnis speichern". The stored calibration (Fleet) is shown with its date and
  "Kalibrierung löschen", and with a hint when the driving speed changed since it was measured.
-->
<script lang="ts">
  import { Button, Icon, ProblemNotice } from '@mowdeck/ui';

  import { formatDateTime, formatNumber, problemMessage } from '$lib/i18n';
  import {
    calibConditionLabel,
    calibrationProgress,
    calibrationReasonLabel,
    calibrationStateLabel,
    canStartCalibration,
    currentCalibrationStep,
    fitFacts,
    formatCommand,
    formatSpeedMps,
    isCalibrationFinal,
    speedSettingDiffers,
    stepKindLabel,
    stepRows,
    type CalibCondition,
  } from '$lib/mower/drive-calibration';
  import type { DriveCalibrationFlow } from '$lib/mower/drive-calibration.svelte';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    flow: DriveCalibrationFlow;
    conditions: CalibCondition[];
    /** The user may drive this mower (`can_drive`). */
    allowed: boolean;
    /** `MowerState.drive_speed_mps`, for the hint about a changed speed setting. */
    currentSpeed: number | null;
    now: number;
    onsaved?: () => void;
    onremoved?: () => void;
  }
  let { flow, conditions, allowed, currentSpeed, now, onsaved, onremoved }: Props = $props();

  const uid = $props.id();
  const status = $derived(flow.status);
  const launching = $derived(flow.launching(now));
  const running = $derived(flow.active || launching);
  const final = $derived(isCalibrationFinal(status) ? status : null);
  const progress = $derived(status ? calibrationProgress(status) : { i: 0, n: 0, percent: 0 });
  const current = $derived(currentCalibrationStep(status));
  const rows = $derived(stepRows(status?.steps));
  const green = $derived(conditions.filter((c) => c.ok).length);
  const startable = $derived(canStartCalibration(conditions, flow.roomConfirmed, allowed));
  const stored = $derived(flow.stored);
  const mismatch = $derived(speedSettingDiffers(currentSpeed, stored));
  const reason = $derived(final ? calibrationReasonLabel(final.reason) : null);
  const tone = $derived(
    status?.state === 'measuring' ? 'mowing' : status?.state === 'stopping' ? 'error' : 'returning',
  );

  async function save(): Promise<void> {
    if (await flow.save()) onsaved?.();
  }

  async function remove(): Promise<void> {
    if (await flow.remove()) onremoved?.();
  }
</script>

{#snippet stepTable(caption: string)}
  <div class="table-wrap">
    <table class="steps" data-testid="calib-steps">
      <caption class="sr">{caption}</caption>
      <thead>
        <tr>
          <th scope="col">{m.calib_col_step()}</th>
          <th scope="col" class="num">{m.calib_col_command()}</th>
          <th scope="col" class="num">{m.calib_col_measured()}</th>
          <th scope="col" class="num">{m.calib_col_reports()}</th>
          <th scope="col" class="num">{m.calib_col_interval()}</th>
          <th scope="col">{m.calib_col_status()}</th>
        </tr>
      </thead>
      <tbody>
        {#each rows as row (row.index)}
          <tr
            data-kind={row.kind}
            data-outcome={row.outcome}
            data-dropped={row.dropped ? 'true' : undefined}
          >
            <th scope="row">
              <span class="idx mono">{String(row.index).padStart(2, '0')}</span>
              <span class="kind" data-kind={row.kind}>{row.kindLabel}</span>
            </th>
            <td class="num mono">{row.command}</td>
            <td class="num mono measured" data-missing={row.missing ? 'true' : undefined}
              >{row.measured}</td
            >
            <td class="num mono">{row.reports}</td>
            <td class="num mono">{row.interval}</td>
            <td class="outcome">
              {#if row.dropped}
                <span class="marker" data-outcome={row.outcome}>{row.outcomeLabel}</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
  {#if rows.some((row) => row.dropped && row.outcome !== 'skipped')}
    <p class="hint" data-testid="calib-outcome-hint">{m.calib_outcome_hint()}</p>
  {/if}
{/snippet}

<section class="calib" aria-labelledby="{uid}-title" data-testid="calib">
  <header class="top">
    <h3 class="label" id="{uid}-title">
      <Icon name="crosshair" size={16} />
      {m.calib_title()}
    </h3>
    <p class="hint">{m.calib_intro()}</p>
  </header>

  {#if running}
    <div
      class="live"
      data-testid="calib-live"
      data-state={status && flow.active ? status.state : 'preparing'}
      data-tone={tone}
    >
      <div class="info">
        <div class="head">
          <span class="lamp" aria-hidden="true"></span>
          <span class="state" role="status"
            >{calibrationStateLabel(status && flow.active ? status.state : 'preparing')}</span
          >
          {#if flow.active && progress.n > 0}
            <span class="mono step" data-testid="calib-progress"
              >{m.calib_step_progress({
                i: formatNumber(progress.i),
                n: formatNumber(progress.n),
              })}</span
            >
          {/if}
        </div>
        {#if current}
          <p class="now mono" data-testid="calib-current">
            {m.calib_current({
              kind: stepKindLabel(current.kind),
              command: formatCommand(current.command),
            })}
          </p>
        {/if}
        {#if flow.active && progress.n > 0}
          <ol class="rail" aria-hidden="true">
            {#each { length: progress.n } as _, i (i)}
              <li
                data-done={i < rows.length ? 'true' : undefined}
                data-now={i === progress.i - 1 && i >= rows.length ? 'true' : undefined}
                data-kind={rows[i]?.kind}
              ></li>
            {/each}
          </ol>
        {/if}
      </div>
      <button
        type="button"
        class="stop"
        disabled={flow.stopping}
        aria-busy={flow.stopping ? 'true' : undefined}
        title={m.calib_stop_hint()}
        data-testid="calib-stop"
        onclick={() => void flow.stop()}
      >
        <Icon name="stop" size={22} />
        <span>{m.spot_stop()}</span>
      </button>
    </div>
    {#if flow.active}
      {#if rows.length > 0}
        {@render stepTable(m.calib_title())}
      {:else}
        <p class="hint">{m.calib_steps_empty()}</p>
      {/if}
    {/if}
  {:else}
    {#if final}
      <div class="end" data-testid="calib-end" data-state={final.state}>
        <span class="glyph" aria-hidden="true">
          <Icon name={final.state === 'done' ? 'check' : 'alert'} size={18} />
        </span>
        <div class="text">
          <strong>{calibrationStateLabel(final.state)}</strong>
          {#if reason}
            <span data-testid="calib-reason">{m.calib_end_reason({ reason })}</span>
          {/if}
        </div>
      </div>
      {#if rows.length > 0}
        {@render stepTable(m.calib_fit_title())}
      {/if}
      {#if flow.result}
        <div class="fit" data-testid="calib-fit">
          <h4 class="label">{m.calib_fit_title()}</h4>
          <dl class="facts">
            {#each fitFacts(flow.result.calibration, flow.result.forward_m) as fact (fact.id)}
              <div data-fact={fact.id}>
                <dt>{fact.label}</dt>
                <dd class="mono">{fact.value}</dd>
              </div>
            {/each}
          </dl>
          <div class="row">
            {#if flow.resultStored}
              <p class="stored-mark" data-testid="calib-result-stored">
                <Icon name="check" size={14} />
                {m.calib_result_stored()}
              </p>
            {:else}
              <Button
                variant="primary"
                icon="download"
                pending={flow.saving}
                disabled={!allowed}
                testid="calib-save"
                onclick={() => void save()}>{m.calib_save()}</Button
              >
            {/if}
          </div>
        </div>
      {/if}
    {/if}

    <div class="start" data-testid="calib-start-block">
      <h4 class="label">
        {m.calib_conditions()}
        <span class="mono count" data-testid="calib-conditions-count"
          >{green}/{conditions.length}</span
        >
      </h4>
      <ul class="conds" data-testid="calib-conditions">
        {#each conditions as condition (condition.id)}
          <li data-condition={condition.id} data-ok={condition.ok ? 'true' : 'false'}>
            <span class="lamp-sq" aria-hidden="true">
              <Icon name={condition.ok ? 'check' : 'close'} size={12} />
            </span>
            <span>{calibConditionLabel(condition.id)}</span>
          </li>
        {/each}
        <li data-condition="blades" data-ok="info">
          <span class="lamp-sq" aria-hidden="true"><Icon name="blade" size={12} /></span>
          <span>{m.calib_cond_blades()}</span>
        </li>
      </ul>
      <label class="confirm" data-testid="calib-room">
        <input type="checkbox" bind:checked={flow.roomConfirmed} data-testid="calib-room-check" />
        <span>
          <strong>{m.calib_cond_room()}</strong>
          <span class="dim">{m.calib_cond_room_hint()}</span>
        </span>
      </label>
      <div class="row">
        <Button
          variant="primary"
          icon="play"
          pending={flow.starting}
          disabled={!startable}
          testid="calib-start"
          onclick={() => void flow.start()}>{m.calib_start()}</Button
        >
        {#if !allowed}
          <span class="hint">{m.calib_not_allowed()}</span>
        {/if}
      </div>
    </div>
  {/if}

  <div class="stored" data-testid="calib-stored" data-present={stored ? 'true' : 'false'}>
    <h4 class="label">{m.calib_stored_title()}</h4>
    {#if stored}
      <p class="when mono" data-testid="calib-stored-at">
        {m.calib_stored_at({ date: formatDateTime(stored.calibrated_at) })}
      </p>
      <dl class="facts">
        {#each fitFacts(stored) as fact (fact.id)}
          <div data-fact={fact.id}>
            <dt>{fact.label}</dt>
            <dd class="mono">{fact.value}</dd>
          </div>
        {/each}
      </dl>
      {#if mismatch && stored.drive_speed_setting != null && currentSpeed != null}
        <p class="mismatch" data-testid="calib-speed-mismatch">
          <Icon name="alert" size={14} />
          {m.calib_speed_mismatch({
            current: formatSpeedMps(currentSpeed),
            stored: formatSpeedMps(stored.drive_speed_setting),
          })}
        </p>
      {/if}
      <div class="row">
        <Button
          variant={flow.confirmRemove ? 'danger' : 'ghost'}
          icon="trash"
          pending={flow.removing}
          disabled={!allowed}
          testid="calib-remove"
          onclick={() => void remove()}
          >{flow.confirmRemove ? m.calib_remove_confirm() : m.calib_remove()}</Button
        >
        {#if flow.confirmRemove}
          <Button variant="ghost" onclick={() => flow.cancelRemove()}>{m.cancel()}</Button>
        {/if}
      </div>
    {:else}
      <p class="hint" data-testid="calib-stored-none">{m.calib_stored_none()}</p>
    {/if}
  </div>

  {#if flow.problem}
    <ProblemNotice
      title={problemMessage(flow.problem)}
      detail={flow.problem.detail}
      reference={flow.problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  {/if}
</section>

<style>
  /* Positioned, so the visually hidden table caption (absolute) is laid out inside the panel.
     Without it the caption's containing block is the viewport: it hangs below the shell's
     scrolling main area, outside its clip, and gives the document a second scrollbar. */
  .calib {
    position: relative;
    display: grid;
    gap: var(--md-space-4);
  }

  .top {
    display: grid;
    gap: var(--md-space-1);
  }

  .label {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .count {
    margin-inline-start: var(--md-space-1);
    color: var(--md-color-ink);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  .dim {
    display: block;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2) var(--md-space-4);
  }

  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  /* --- the live run: the run bar's language (lamp, uppercase state, ink STOP) ------------- */
  .live {
    --tone: var(--md-state-returning);

    display: flex;
    align-items: stretch;
    gap: var(--md-space-3);
    padding: var(--md-space-3);
    border-inline-start: 4px solid var(--tone);
    background: var(--md-color-surface-sunken);
  }

  .live[data-tone='mowing'] {
    --tone: var(--md-state-mowing);
  }

  .live[data-tone='error'] {
    --tone: var(--md-state-error);
  }

  .info {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    gap: var(--md-space-2);
    min-width: 0;
  }

  .head {
    display: flex;
    flex-wrap: wrap;
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

  .live[data-state='measuring'] .lamp,
  .live[data-state='preparing'] .lamp {
    animation: pulse 1.6s var(--md-ease) infinite;
  }

  .state {
    flex: 1 1 auto;
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .step {
    font-size: var(--md-font-size-14);
  }

  .now {
    margin: 0;
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-28);
  }

  /* One cell per step: forward cells square, turn cells round, the current one pulsing. */
  .rail {
    display: flex;
    gap: 3px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .rail li {
    flex: 1 1 0;
    height: 8px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
  }

  .rail li[data-done='true'] {
    border-color: var(--tone);
    background: var(--tone);
  }

  .rail li[data-now='true'] {
    border-color: var(--tone);
    background: color-mix(in srgb, var(--tone) 35%, transparent);
    animation: blink 1.2s var(--md-ease) infinite;
  }

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

  .stop:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  /* --- the step table: monospace numbers, the kind as a tag ------------------------------- */

  /* A wide table scrolls sideways in its own box, never up and down: overflow-x alone would
     make overflow-y auto as well, and the collapsed bottom border may stick out a pixel. */
  .table-wrap {
    overflow: auto hidden;
  }

  .steps {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--md-font-size-13);
  }

  .steps th,
  .steps td {
    padding: var(--md-space-1) var(--md-space-2);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    text-align: start;
    white-space: nowrap;
  }

  .steps thead th {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .steps tbody th {
    font-weight: var(--md-weight-regular);
  }

  .steps .num {
    text-align: end;
  }

  .idx {
    margin-inline-end: var(--md-space-2);
    color: var(--md-color-ink-muted);
  }

  .kind {
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid currentcolor;
    font-size: var(--md-font-size-11);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .kind[data-kind='turn'] {
    border-radius: var(--md-radius-round);
  }

  .steps td[data-missing='true'],
  .steps tr[data-dropped='true'] {
    color: var(--md-color-ink-muted);
  }

  /* a value the fit left out stays readable but is struck through */
  .steps tr[data-outcome='inconsistent'] .measured,
  .steps tr[data-outcome='outlier'] .measured {
    text-decoration: line-through;
  }

  .marker {
    display: inline-block;
    padding-inline-start: var(--md-space-2);
    border-inline-start: 3px solid var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: 1.3;
  }

  .marker[data-outcome='invalid'],
  .marker[data-outcome='inconsistent'],
  .marker[data-outcome='outlier'] {
    border-inline-start-color: var(--md-state-paused);
  }

  /* --- the end notice, as the run bar's -------------------------------------------------- */
  .end {
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 4px solid var(--md-state-mowing);
    background: var(--md-color-surface-sunken);
  }

  .end[data-state='aborted'] {
    border-inline-start-color: var(--md-state-paused);
  }

  .end[data-state='failed'] {
    border-inline-start-color: var(--md-state-error);
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
    flex-direction: column;
    min-width: 0;
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  /* --- facts (fit and stored) ------------------------------------------------------------- */
  .fit,
  .stored,
  .start {
    display: grid;
    gap: var(--md-space-3);
  }

  .stored {
    padding-top: var(--md-space-3);
    border-top: var(--md-hairline) dashed var(--md-color-line-strong);
  }

  .when {
    margin: 0;
    font-size: var(--md-font-size-13);
  }

  .facts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(9.5rem, 1fr));
    gap: var(--md-space-2) var(--md-space-4);
    margin: 0;
  }

  .facts div {
    min-width: 0;
    padding-inline-start: var(--md-space-2);
    border-inline-start: 2px solid var(--md-color-line-strong);
  }

  .facts dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .facts dd {
    margin: 0;
    font-size: var(--md-font-size-16);
  }

  .stored-mark {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    margin: 0;
    color: var(--md-state-mowing);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
  }

  .mismatch {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-2);
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 4px solid var(--md-state-paused);
    background: var(--md-state-paused-bg);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  /* --- start conditions: the spot sheet's square lamps -------------------------------------- */
  .conds {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
    gap: var(--md-space-1) var(--md-space-4);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .conds li {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-line-28);
    font-size: var(--md-font-size-13);
  }

  .lamp-sq {
    display: grid;
    flex: none;
    place-items: center;
    width: 18px;
    height: 18px;
    border-radius: var(--md-radius-1);
    background: var(--md-state-error);
    color: var(--md-color-ink-inverse);
  }

  .conds li[data-ok='true'] .lamp-sq {
    background: var(--md-state-mowing);
  }

  .conds li[data-ok='info'] {
    color: var(--md-color-ink-muted);
  }

  .conds li[data-ok='info'] .lamp-sq {
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
  }

  .confirm {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-3);
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 3px solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
    cursor: pointer;
  }

  .confirm input {
    flex: none;
    width: 20px;
    height: 20px;
    margin: 0;
    accent-color: var(--md-color-ink);
  }

  @keyframes pulse {
    50% {
      box-shadow: 0 0 0 6px color-mix(in srgb, var(--tone) 10%, transparent);
    }
  }

  @keyframes blink {
    50% {
      background: color-mix(in srgb, var(--tone) 70%, transparent);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .live .lamp,
    .rail li[data-now='true'] {
      animation: none;
    }
  }
</style>
