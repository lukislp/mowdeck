<!--
  The spot sheet: the shape of the spot (a circle with its radius, or a drawn polygon with its
  corner count), the pattern (spiral or stripes with an angle), the path spacing, the speed (at
  most 0.3 m/s), the turns at lane ends (ADR 0043), the blades (off by default: a dry run; on only when the mower allows blades
  while driving, with a second confirmation), the Planner's preview (duration, segments,
  warnings) and the start conditions as green and red rows. "Starten" is enabled only when every
  row is green and a preview exists. The flow (`$lib/mower/spot-sheet.svelte`) owns the state;
  this file is markup.

  Desktop: the side sheet with everything and the start row in its footer. Phones (`phone`): a
  `BottomSheet` over the full-screen map, which stays visible and tappable above it:
  - peek: pattern and size ("Spirale · 1,5 m"), the estimated duration, the start conditions as
    one lamp with "6/8" (a tap opens half, scrolled to the conditions) and START; while a run
    starts, runs or has just ended, the run bar (`runBar`, with its big STOP) instead, and the
    sheet stays at peek, so STOP never needs a drag;
  - half: shape and radius, pattern (and angle), speed, turns ("Wenden": the mower's own setting
    with the style it resolves to, in place, arc, multi-point; multi-point is unavailable when
    the drive link cannot reverse);
  - full: spacing, blades with the second confirmation, every start condition, the preview's
    details, cancel.
  `snap` and `visibleHeight` are bindable: the page collapses to peek when the map is tapped and
  pads the map by the covered height, so the spot and its route stay above the sheet.
-->
<script lang="ts">
  import { tick, type Snippet } from 'svelte';

  import {
    BottomSheet,
    Button,
    Icon,
    SegmentedControl,
    Sheet,
    Stepper,
    Switch,
    polygonAreaM2,
    spotPolygon,
    type SnapPoint,
  } from '@mowdeck/ui';

  import { formatNumber } from '$lib/i18n';
  import { formatSpotDuration } from '$lib/mower/duration';
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
    conditionHint,
    conditionText,
    hasTurnKinds,
    turnStyleHint,
    type TurnStyle,
  } from '$lib/mower/spot-sheet';
  import type { SpotSheetFlow } from '$lib/mower/spot-sheet.svelte';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    flow: SpotSheetFlow;
    /** The display name of a mapped area. */
    areaName: (id: string) => string;
    /** Narrow viewport: the bottom sheet instead of the side sheet. */
    phone?: boolean;
    /** The bottom sheet's snap point (bindable). */
    snap?: SnapPoint;
    /** Pixels the bottom sheet covers (bindable, read only). */
    visibleHeight?: number;
    /** The run bar (starting, running, ended) for the bottom sheet's peek. */
    runBar?: Snippet;
  }
  let {
    flow,
    areaName,
    phone = false,
    snap = $bindable('peek'),
    visibleHeight = $bindable(0),
    runBar,
  }: Props = $props();

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
  const duration = $derived(preview ? formatSpotDuration(preview.estimated_duration_s) : '');
  const green = $derived(flow.conditions.filter((c) => c.ok).length);
  const allOk = $derived(green === flow.conditions.length);
  /** The run owns the peek: starting, running or just ended. */
  const runShown = $derived(!!runBar && (flow.launching || flow.running || flow.ended !== null));
  const summary = $derived(
    [
      flow.settings.pattern === 'stripes' ? m.spot_pattern_stripes() : m.spot_pattern_spiral(),
      flow.spot?.kind === 'polygon'
        ? m.spot_size_area({ area: formatNumber(areaM2, 1) })
        : m.spot_radius_value({ value: formatNumber(flow.radiusM, 1) }),
    ].join(' · '),
  );
  let conditionsEl: HTMLElement | undefined = $state();

  /**
   * A small top-down sketch of each turn (24 x 24): the lane comes up from the bottom, turns and
   * goes back down; a dashed stroke is the reverse leg of the three-point turn.
   */
  const TURN_GLYPHS: Record<TurnStyle, { d: string; reverse?: boolean }[]> = {
    // Rotating on the spot leaves square corners.
    in_place: [{ d: 'M6 21V5h12v16' }],
    arc: [{ d: 'M5 21V11a7 7 0 0 1 14 0v10' }],
    multi_point: [
      { d: 'M5 21V9a4 4 0 0 1 4-4h2' },
      { d: 'M11 5l2 6', reverse: true },
      { d: 'M13 11a4 4 0 0 1 6 3v7' },
    ],
  };
  const showLegend = $derived(hasTurnKinds(flow.previewPath));

  /** The lamp in the peek: half, scrolled to the start conditions. */
  async function showConditions(): Promise<void> {
    snap = 'half';
    await tick();
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    conditionsEl?.scrollIntoView({ block: 'start', behavior: reduced ? 'auto' : 'smooth' });
  }
</script>

{#snippet shapeSection()}
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
        <button type="button" class="act" data-testid="spot-redraw" onclick={() => flow.drawShape()}
          >{m.spot_redraw()}</button
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
{/snippet}

{#snippet patternPicker()}
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
{/snippet}

{#snippet angleStepper()}
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
{/snippet}

{#snippet spacingStepper()}
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
{/snippet}

{#snippet speedSection()}
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
{/snippet}

{#snippet turnSection()}
  <section class="block" aria-labelledby="{uid}-turns">
    <h3 class="label" id="{uid}-turns">{m.spot_turns()}</h3>
    <div class="turns" role="radiogroup" aria-labelledby="{uid}-turns" data-testid="spot-turns">
      {#each flow.turnOptions as option (option.value ?? 'mower')}
        {@const value = option.value ?? 'mower'}
        <label
          class="turn"
          class:mower={option.value === null}
          data-value={value}
          data-checked={flow.settings.turnStyle === option.value ? 'true' : 'false'}
          data-disabled={option.disabled ? 'true' : 'false'}
          data-testid="spot-turn-{value}"
        >
          <input
            type="radio"
            name="{uid}-turn"
            {value}
            checked={flow.settings.turnStyle === option.value}
            disabled={option.disabled}
            onchange={() => flow.setTurnStyle(option.value)}
          />
          <svg class="glyph" viewBox="0 0 24 24" aria-hidden="true">
            {#each TURN_GLYPHS[option.resolved] as stroke (stroke.d)}
              <path d={stroke.d} class:reverse={stroke.reverse} />
            {/each}
          </svg>
          <span class="turn-text">
            <span class="turn-label">{option.label}</span>
            {#if option.hint}<span class="turn-hint">{option.hint}</span>{/if}
          </span>
        </label>
      {/each}
    </div>
    {#if flow.turnFallback}
      <p class="note" data-testid="spot-turn-fallback">
        <Icon name="alert" size={14} />{m.spot_turn_fallback()}
      </p>
    {:else}
      <p class="hint" data-testid="spot-turn-hint">{turnStyleHint(flow.turnStyle)}</p>
    {/if}
  </section>
{/snippet}

{#snippet bladesSection()}
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
{/snippet}

{#snippet conditionsSection()}
  <section class="block conds-block" aria-labelledby="{uid}-conds" bind:this={conditionsEl}>
    <h3 class="label" id="{uid}-conds">
      {m.spot_conditions()}
      <span class="mono count" data-testid="spot-conditions-count"
        >{green}/{flow.conditions.length}</span
      >
    </h3>
    <ul class="conds" data-testid="spot-conditions">
      {#each flow.conditions as condition (condition.id)}
        {@const hint = conditionHint(condition)}
        <li
          data-condition={condition.id}
          data-ok={condition.neutral ? 'info' : condition.ok ? 'true' : 'false'}
        >
          <span class="lamp" aria-hidden="true">
            <Icon
              name={condition.neutral ? 'signal' : condition.ok ? 'check' : 'close'}
              size={12}
            />
          </span>
          <span>
            {conditionText(condition)}
            {#if hint}<span class="dim">{hint}</span>{/if}
          </span>
        </li>
      {/each}
    </ul>
    <p class="hint" data-testid="spot-fresh-hint">{m.spot_cond_fresh_hint()}</p>
  </section>
{/snippet}

{#snippet previewDetails()}
  {@const state = flow.preview}
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
  {#if preview && showLegend}
    <ul class="legend" data-testid="spot-legend">
      <li><span class="swatch" data-kind="mow" aria-hidden="true"></span>{m.spot_legend_mow()}</li>
      <li>
        <span class="swatch" data-kind="turn" aria-hidden="true"></span>{m.spot_legend_turn()}
      </li>
      {#if flow.previewPath?.kinds?.includes('reverse')}
        <li>
          <span class="swatch" data-kind="reverse" aria-hidden="true"
          ></span>{m.spot_legend_reverse()}
        </li>
      {/if}
    </ul>
  {/if}
  {#if preview?.warnings.length}
    <ul class="warnings" data-testid="spot-warnings">
      {#each preview.warnings as warning (warning)}
        <li><Icon name="alert" size={14} />{warning}</li>
      {/each}
    </ul>
  {/if}
{/snippet}

{#snippet startError()}
  {#if flow.startError}
    <p class="bad" role="alert" data-testid="spot-start-error">{flow.startError}</p>
  {/if}
{/snippet}

{#snippet startButton()}
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
{/snippet}

{#snippet peek()}
  {#if runShown && runBar}
    {@render runBar()}
  {:else}
    <div class="peek" data-testid="spot-peek">
      <div class="summary">
        <span class="sum-shape mono" data-testid="spot-peek-summary">{summary}</span>
        <span
          class="sum-time mono"
          data-testid="spot-peek-duration"
          data-state={preview ? 'ready' : flow.preview.kind}
        >
          {#if preview}
            {m.spot_peek_duration({ duration })}
          {:else if flow.preview.kind === 'loading'}
            {m.spot_peek_computing()}
          {:else if flow.preview.kind === 'error' && flow.preview.key === flow.key}
            <span class="bad">{m.spot_peek_no_route()}</span>
          {:else}
            –
          {/if}
        </span>
      </div>
      <div class="peek-row">
        <button
          type="button"
          class="conds-lamp"
          data-ok={allOk ? 'true' : 'false'}
          aria-label={m.spot_conditions_summary({
            ok: String(green),
            total: String(flow.conditions.length),
          })}
          data-testid="spot-peek-conditions"
          onclick={showConditions}
        >
          <span class="lamp" aria-hidden="true">
            <Icon name={allOk ? 'check' : 'close'} size={12} />
          </span>
          <span class="mono">{green}/{flow.conditions.length}</span>
        </button>
        <div class="go">{@render startButton()}</div>
      </div>
      {@render startError()}
    </div>
  {/if}
{/snippet}

{#snippet body()}
  <div class="spot">
    {@render shapeSection()}
    <section class="block" aria-labelledby="{uid}-pattern">
      <h3 class="label" id="{uid}-pattern">{m.spot_pattern()}</h3>
      {@render patternPicker()}
      {#if flow.settings.pattern === 'stripes'}
        <div class="steppers">{@render angleStepper()}</div>
      {/if}
    </section>
    {@render speedSection()}
    {@render turnSection()}
    <section class="block" aria-labelledby="{uid}-spacing">
      <h3 class="label" id="{uid}-spacing">{m.spot_spacing()}</h3>
      <div class="steppers">{@render spacingStepper()}</div>
      <p class="hint">{m.spot_spacing_hint({ width: formatNumber(cuttingWidth) })}</p>
    </section>
    {@render bladesSection()}
    {@render conditionsSection()}
    <section class="block" aria-labelledby="{uid}-preview">
      <h3 class="label" id="{uid}-preview">{m.spot_preview_title()}</h3>
      {@render previewDetails()}
    </section>
    <div class="buttons">
      <Button variant="ghost" onclick={() => flow.cancel()} testid="spot-cancel"
        >{m.cancel()}</Button
      >
    </div>
  </div>
{/snippet}

{#if phone}
  <BottomSheet
    open={flow.open || runShown}
    bind:snap
    bind:visibleHeight
    snaps={runShown ? ['peek'] : undefined}
    label={m.spot_title()}
    handleLabel={m.spot_sheet_handle()}
    safeArea={false}
    {peek}
    children={runShown ? undefined : body}
    testid="spot-sheet"
  />
{:else}
  <Sheet
    open={flow.open}
    title={m.spot_title()}
    closeLabel={m.close()}
    onclose={() => flow.close()}
    testid="spot-sheet"
  >
    <div class="spot">
      {@render shapeSection()}
      <section class="block" aria-labelledby="{uid}-pattern">
        <h3 class="label" id="{uid}-pattern">{m.spot_pattern()}</h3>
        {@render patternPicker()}
        <div class="steppers">
          {#if flow.settings.pattern === 'stripes'}
            {@render angleStepper()}
          {/if}
          {@render spacingStepper()}
        </div>
        <p class="hint">{m.spot_spacing_hint({ width: formatNumber(cuttingWidth) })}</p>
      </section>
      {@render speedSection()}
      {@render turnSection()}
      {@render bladesSection()}
      {@render conditionsSection()}
    </div>

    {#snippet footer()}
      <div class="foot">
        {@render previewDetails()}
        {@render startError()}
        <div class="buttons">
          <Button variant="ghost" onclick={() => flow.cancel()} testid="spot-cancel"
            >{m.cancel()}</Button
          >
          <div class="go">{@render startButton()}</div>
        </div>
      </div>
    {/snippet}
  </Sheet>
{/if}

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

  /* not known yet (the bridge signal): neutral, not blocking */
  .conds li[data-ok='info'] .lamp {
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
  }

  .conds .dim {
    display: block;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
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

  /* --- the phone peek: the summary row, then the conditions lamp and START ----------------- */
  .peek {
    display: grid;
    gap: var(--md-space-2);
  }

  .summary {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-3);
    min-width: 0;
  }

  .sum-shape {
    overflow: hidden;
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-overflow: ellipsis;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .sum-time {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    white-space: nowrap;
  }

  .sum-time[data-state='ready'] {
    color: var(--md-color-ink);
  }

  .peek-row {
    display: flex;
    align-items: stretch;
    gap: var(--md-space-2);
  }

  .peek-row .go {
    flex: 1 1 auto;
  }

  /* One square lamp for all start conditions with the green count; a tap shows the list. */
  .conds-lamp {
    display: flex;
    flex: none;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface-sunken);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
    cursor: pointer;
  }

  .conds-lamp[data-ok='true'] .lamp {
    background: var(--md-state-mowing);
  }

  .conds-lamp:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .conds-block {
    scroll-margin-top: var(--md-space-2);
  }

  /* --- turns: the mower's own setting across the top, the three styles as sketched tiles ---- */
  .turns {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--md-space-1);
  }

  .turn {
    position: relative;
    display: grid;
    grid-template-rows: auto 1fr;
    justify-items: start;
    gap: var(--md-space-1);
    min-height: var(--md-target);
    padding: var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    cursor: pointer;
  }

  .turn.mower {
    grid-column: 1 / -1;
    grid-template-columns: auto minmax(0, 1fr);
    grid-template-rows: none;
    align-items: center;
    gap: var(--md-space-2);
  }

  /* The radio stays in the tab order and the accessibility tree; the tile shows its state. */
  .turn input {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    opacity: 0;
    cursor: inherit;
  }

  .turn[data-checked='true'] {
    border-color: var(--md-color-focus);
    background: var(--md-color-surface-sunken);
    box-shadow: inset 0 0 0 1px var(--md-color-focus);
  }

  .turn:has(input:focus-visible) {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .turn[data-disabled='true'] {
    border-style: dashed;
    color: var(--md-color-ink-muted);
    cursor: not-allowed;
  }

  .glyph {
    width: 22px;
    height: 22px;
    fill: none;
    stroke: currentcolor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 2;
  }

  .turn[data-checked='true'] .glyph {
    stroke: var(--md-color-focus);
  }

  .glyph .reverse {
    stroke-dasharray: 2 3;
  }

  .turn-text {
    display: grid;
    gap: var(--md-space-0_5);
    min-width: 0;
  }

  .turn-label {
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
    line-height: var(--md-line-16);
  }

  .turn-hint {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    line-height: var(--md-line-16);
  }

  .note {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-2);
    margin: 0;
    padding: var(--md-space-1) var(--md-space-2);
    border-inline-start: 3px solid var(--md-state-paused);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  /* --- the preview's legend: the same strokes as the map's path ----------------------------- */
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-3);
    margin: 0;
    padding: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    letter-spacing: var(--md-label-tracking);
    list-style: none;
    text-transform: uppercase;
  }

  .legend li {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
  }

  .swatch {
    width: 18px;
    height: 0;
    border-top: 2px solid var(--md-color-focus);
  }

  .swatch[data-kind='turn'] {
    border-top-color: var(--md-state-paused);
  }

  .swatch[data-kind='reverse'] {
    border-top: 2px dashed var(--md-state-paused);
  }

  /*
   * The plan preview on the map (MowerMap draws each polyline as `md-mowpath--<kind>`): lane-end
   * turns in amber so they read apart from the lanes, a three-point turn's reverse leg dashed.
   * Only spot previews (and a spot run seeded with one) carry these kinds; the vendor's live
   * paths keep their styles.
   */
  :global(.md-map path.md-mowpath.md-mowpath--turn),
  :global(.md-map path.md-mowpath.md-mowpath--reverse) {
    stroke: var(--md-state-paused);
    stroke-opacity: 0.85;
    stroke-width: 1.5;
  }

  :global(.md-map path.md-mowpath.md-mowpath--reverse) {
    stroke-dasharray: 5 4;
    stroke-linecap: butt;
  }

  :global(.md-map path.md-mowpath.md-mowpath--done.md-mowpath--turn),
  :global(.md-map path.md-mowpath.md-mowpath--done.md-mowpath--reverse) {
    stroke-opacity: 1;
    stroke-width: 2;
  }
</style>
