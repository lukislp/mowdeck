<!--
  The spot sheet: the shape of the spot (a circle with its radius, or a drawn polygon with its
  corner count), the pattern (spiral or stripes with an angle), the path spacing, the speed (at
  most 0.3 m/s), the blades (off by default: a dry run; on only when the mower allows blades
  while driving, with a second confirmation), the Planner's preview (duration, segments,
  warnings) and the start conditions as green and red rows. "Starten" is enabled only when every
  row is green and a preview exists. The flow (`$lib/mower/spot-sheet.svelte`) owns the state;
  this file is markup.
-->
<script lang="ts">
  import {
    Button,
    Icon,
    SegmentedControl,
    Sheet,
    Stepper,
    Switch,
    polygonAreaM2,
    spotPolygon,
  } from '@mowdeck/ui';

  import { formatNumber } from '$lib/i18n';
  import { formatDuration } from '$lib/mower/duration';
  import { ANGLE_MAX, ANGLE_STEP } from '$lib/mower/mow-pattern';
  import {
    FALLBACK_CUTTING_WIDTH_MM,
    SPOT_CORNERS_MAX,
    SPOT_RADIUS_MAX_M,
    SPOT_RADIUS_MIN_M,
    SPOT_RADIUS_STEP_M,
    SPOT_SPACING_MIN_MM,
    SPOT_SPACING_STEP_MM,
    SPOT_SPEED_MAX_MPS,
    SPOT_SPEED_MIN_MPS,
    SPOT_SPEED_STEP_MPS,
    conditionLabel,
  } from '$lib/mower/spot-sheet';
  import type { SpotSheetFlow } from '$lib/mower/spot-sheet.svelte';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    flow: SpotSheetFlow;
    /** The display name of a mapped area. */
    areaName: (id: string) => string;
  }
  let { flow, areaName }: Props = $props();

  const uid = $props.id();
  const areaM2 = $derived(polygonAreaM2(spotPolygon(flow.spot)));
  const cuttingWidth = $derived(flow.cuttingWidthMm ?? FALLBACK_CUTTING_WIDTH_MM);
  const shapeOptions = $derived([
    { value: 'circle', label: m.spot_shape_circle() },
    { value: 'polygon', label: m.spot_shape_polygon() },
  ]);
  const patternOptions = $derived([
    { value: 'spiral', label: m.spot_pattern_spiral() },
    { value: 'stripes', label: m.spot_pattern_stripes() },
  ]);
  const preview = $derived(flow.previewReady);
  const duration = $derived(preview ? formatDuration(preview.estimated_duration_s / 60) : '');
  const green = $derived(flow.conditions.filter((c) => c.ok).length);
</script>

<Sheet
  open={flow.open}
  title={m.spot_title()}
  closeLabel={m.close()}
  onclose={() => flow.close()}
  testid="spot-sheet"
>
  <div class="spot">
    <section class="block" aria-labelledby="{uid}-shape">
      <h3 class="label" id="{uid}-shape">{m.spot_shape()}</h3>
      <SegmentedControl
        label={m.spot_shape()}
        options={shapeOptions}
        value={flow.mode === 'polygon' ? 'polygon' : 'circle'}
        onchange={(value) => (value === 'polygon' ? flow.drawShape() : flow.useCircle())}
        testid="spot-shape"
      />
      {#if flow.mode === 'circle'}
        <label class="slider">
          <span class="slider-head">
            <span>{m.spot_radius()}</span>
            <output class="mono" data-testid="spot-radius-value"
              >{m.spot_radius_value({ value: formatNumber(flow.radiusM, 1) })}</output
            >
          </span>
          <input
            type="range"
            min={SPOT_RADIUS_MIN_M}
            max={SPOT_RADIUS_MAX_M}
            step={SPOT_RADIUS_STEP_M}
            value={flow.radiusM}
            data-testid="spot-radius"
            oninput={(event) => flow.setRadius(Number(event.currentTarget.value))}
          />
        </label>
      {:else}
        <div class="corners">
          <span class="mono" data-testid="spot-corners">
            {m.spot_corners({ n: flow.corners, max: SPOT_CORNERS_MAX })}
          </span>
          <button
            type="button"
            class="act"
            disabled={flow.corners === 0}
            data-testid="spot-undo"
            onclick={() => flow.undoCorner()}>{m.spot_undo_corner()}</button
          >
          <button
            type="button"
            class="act"
            data-testid="spot-redraw"
            onclick={() => flow.drawShape()}>{m.spot_redraw()}</button
          >
        </div>
      {/if}
      <p class="where" data-testid="spot-area" data-ok={flow.areaId ? 'true' : 'false'}>
        {#if flow.areaId}
          <Icon name="check" size={14} />
          {m.spot_in_area({ name: areaName(flow.areaId), area: formatNumber(areaM2, 1) })}
        {:else if flow.complete}
          <Icon name="alert" size={14} />
          {m.spot_outside()}
        {:else}
          {m.spot_more_corners()}
        {/if}
      </p>
    </section>

    <section class="block" aria-labelledby="{uid}-pattern">
      <h3 class="label" id="{uid}-pattern">{m.spot_pattern()}</h3>
      <SegmentedControl
        label={m.spot_pattern()}
        options={patternOptions}
        value={flow.settings.pattern}
        onchange={(value) =>
          (flow.settings = {
            ...flow.settings,
            pattern: value === 'stripes' ? 'stripes' : 'spiral',
          })}
        testid="spot-pattern"
      />
      <div class="steppers">
        {#if flow.settings.pattern === 'stripes'}
          <Stepper
            id="spot-angle"
            label={m.start_angle()}
            value={flow.settings.angleDeg}
            min={0}
            max={ANGLE_MAX}
            step={ANGLE_STEP}
            unit="°"
            decreaseLabel={m.start_angle_lower()}
            increaseLabel={m.start_angle_raise()}
            immediate
            oncommit={(angleDeg) => (flow.settings = { ...flow.settings, angleDeg })}
          />
        {/if}
        <Stepper
          id="spot-spacing"
          label={m.spot_spacing()}
          value={flow.settings.spacingMm}
          min={SPOT_SPACING_MIN_MM}
          max={cuttingWidth}
          step={SPOT_SPACING_STEP_MM}
          unit="mm"
          decreaseLabel={m.start_spacing_narrower()}
          increaseLabel={m.start_spacing_wider()}
          immediate
          oncommit={(mm) => flow.setSpacing(mm)}
        />
      </div>
      <p class="hint">{m.spot_spacing_hint({ width: formatNumber(cuttingWidth) })}</p>
    </section>

    <section class="block" aria-labelledby="{uid}-speed">
      <h3 class="label" id="{uid}-speed">{m.spot_speed()}</h3>
      <label class="slider">
        <span class="slider-head">
          <span>{m.spot_speed()}</span>
          <output class="mono" data-testid="spot-speed-value"
            >{m.spot_speed_value({ value: formatNumber(flow.settings.speedMps, 2) })}</output
          >
        </span>
        <input
          type="range"
          min={SPOT_SPEED_MIN_MPS}
          max={SPOT_SPEED_MAX_MPS}
          step={SPOT_SPEED_STEP_MPS}
          value={flow.settings.speedMps}
          data-testid="spot-speed"
          oninput={(event) => flow.setSpeed(Number(event.currentTarget.value))}
        />
      </label>
      <p class="hint">{m.spot_speed_cap({ max: formatNumber(SPOT_SPEED_MAX_MPS, 1) })}</p>
    </section>

    <section class="block" aria-labelledby="{uid}-blades">
      <h3 class="label" id="{uid}-blades">{m.spot_blades()}</h3>
      <Switch
        id="spot-blades"
        label={m.spot_blades_switch()}
        hint={flow.bladesAllowed ? m.spot_blades_dry() : m.spot_blades_unsupported()}
        checked={flow.bladesAllowed && flow.settings.blades}
        states={{ on: m.switch_on(), off: m.switch_off() }}
        disabled={!flow.bladesAllowed}
        testid="spot-blades"
        onchange={(on) => flow.setBlades(on)}
      />
      {#if flow.bladesAllowed && flow.settings.blades}
        <label class="confirm" data-testid="spot-blades-confirm">
          <input
            type="checkbox"
            checked={flow.settings.confirmBlades}
            onchange={(event) =>
              (flow.settings = { ...flow.settings, confirmBlades: event.currentTarget.checked })}
          />
          <span>{m.spot_blades_confirm()}</span>
        </label>
      {/if}
    </section>

    <section class="block" aria-labelledby="{uid}-conds">
      <h3 class="label" id="{uid}-conds">
        {m.spot_conditions()}
        <span class="mono count" data-testid="spot-conditions-count"
          >{green}/{flow.conditions.length}</span
        >
      </h3>
      <ul class="conds" data-testid="spot-conditions">
        {#each flow.conditions as condition (condition.id)}
          <li data-condition={condition.id} data-ok={condition.ok ? 'true' : 'false'}>
            <span class="lamp" aria-hidden="true">
              <Icon name={condition.ok ? 'check' : 'close'} size={12} />
            </span>
            <span>{conditionLabel(condition.id)}</span>
          </li>
        {/each}
      </ul>
      <p class="hint" data-testid="spot-fresh-hint">{m.spot_cond_fresh_hint()}</p>
    </section>
  </div>

  {#snippet footer()}
    {@const state = flow.preview}
    <div class="foot">
      <p
        class="preview"
        role="status"
        data-testid="spot-preview"
        data-state={state.kind === 'idle' ? 'idle' : preview ? 'ready' : state.kind}
      >
        {#if preview}
          <span class="mono">
            {m.spot_preview_ready({
              duration,
              segments: formatNumber(preview.segment_count),
              area: formatNumber(preview.covered_area_m2, 1),
            })}
          </span>
        {:else if state.kind === 'loading'}
          {m.spot_preview_loading()}
        {:else if state.kind === 'error' && state.key === flow.key}
          <span class="bad">{m.spot_preview_failed({ reason: state.reason })}</span>
        {:else}
          {m.spot_preview_hint()}
        {/if}
      </p>
      {#if preview?.warnings.length}
        <ul class="warnings" data-testid="spot-warnings">
          {#each preview.warnings as warning (warning)}
            <li><Icon name="alert" size={14} />{warning}</li>
          {/each}
        </ul>
      {/if}
      {#if flow.startError}
        <p class="bad" role="alert" data-testid="spot-start-error">{flow.startError}</p>
      {/if}
      <div class="buttons">
        <Button variant="ghost" onclick={() => flow.cancel()} testid="spot-cancel"
          >{m.cancel()}</Button
        >
        <div class="go">
          <Button
            variant="primary"
            full
            icon="play"
            disabled={!flow.canStart}
            pending={flow.starting}
            onclick={() => flow.start()}
            testid="spot-start"
            >{flow.starting
              ? m.spot_starting()
              : flow.bladesOn
                ? m.spot_start()
                : m.spot_start_dry()}</Button
          >
        </div>
      </div>
    </div>
  {/snippet}
</Sheet>

<style>
  .spot {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-4);
    padding-block: var(--md-space-2);
  }

  .block {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
  }

  .label {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .count {
    font-size: var(--md-font-size-12);
    letter-spacing: 0;
    text-transform: none;
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  /* A slider with its value as an instrument readout above the track. */
  .slider {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    padding: var(--md-space-2) var(--md-space-3);
    background: var(--md-color-surface-sunken);
  }

  .slider-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    font-size: var(--md-font-size-13);
  }

  .slider-head output {
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-28);
  }

  .slider input {
    width: 100%;
    height: var(--md-target);
    margin: 0;
    accent-color: var(--md-color-focus);
    cursor: pointer;
  }

  .corners {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
  }

  .corners .mono {
    flex: 1 1 auto;
    font-size: var(--md-font-size-14);
  }

  .act {
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
  }

  .act:disabled {
    border-style: dashed;
    color: var(--md-color-ink-muted);
    cursor: not-allowed;
  }

  .where {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  .where[data-ok='true'] {
    color: var(--md-state-mowing);
  }

  .where[data-ok='false']:has(:global(svg)) {
    color: var(--md-state-error);
  }

  .steppers {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(10.5rem, 1fr));
    gap: var(--md-space-2);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .confirm {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-3);
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 3px solid var(--md-state-error);
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
    accent-color: var(--md-state-error);
  }

  /* The start conditions: a square lamp with a glyph, never colour alone. */
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

  .lamp {
    display: grid;
    flex: none;
    place-items: center;
    width: 18px;
    height: 18px;
    border-radius: var(--md-radius-1);
    background: var(--md-state-error);
    color: var(--md-color-ink-inverse);
  }

  .conds li[data-ok='true'] .lamp {
    background: var(--md-state-mowing);
  }

  .conds li[data-ok='false'] {
    color: var(--md-color-ink);
  }

  .foot {
    display: grid;
    gap: var(--md-space-2);
  }

  .preview {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .preview .mono {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-13);
  }

  .bad {
    margin: 0;
    color: var(--md-state-error);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .warnings {
    display: grid;
    gap: var(--md-space-1);
    margin: 0;
    padding: 0;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    list-style: none;
  }

  .warnings li {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-2);
    padding: var(--md-space-1) var(--md-space-2);
    border-inline-start: 3px solid var(--md-state-paused);
    background: var(--md-color-surface-sunken);
  }

  .buttons {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
  }

  .go {
    flex: 1 1 10rem;
  }
</style>
