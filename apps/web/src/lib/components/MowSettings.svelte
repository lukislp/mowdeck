<!--
  The "Mähen" section of the start sheet: the native pattern (only those the mower reports in
  `capabilities.native_patterns`), its base angle, the border laps, the path spacing (only when
  the model reports a range), a summary line and "remember as default". Blade height and speed
  are mower setpoints (separate commands, not part of the defaults): with the model's ranges the
  section offers two steppers (`setpoints`, bindable; the page sends the changed values before
  the start), without them it shows the current values and jumps to the page's steppers.
  The job options the mower declares (`options`, from `capabilities.start_options`: obstacle
  detection, laps around no-go zones, path order, start progress) sit behind "Mehr"
  (`start-more-toggle`), like the vendor app's task assignment; an option left empty keeps the
  mower's own setting, and the start progress belongs to this start only (never remembered).
  Layout: on phones the section is collapsed to the summary line plus "Anpassen"
  (`start-settings-toggle`); expanded it shows the pattern tiles, then the numeric settings. From
  1024 px it is always open (the toggle is hidden) and the sheet puts it beside the area list.
  The page owns the draft (`$lib/mower/mow-pattern`), the start and saving the defaults.
-->
<script lang="ts">
  import { Stepper, Switch } from '@mowdeck/ui';

  import {
    formatNumber,
    formatPercent,
    obstacleLabel,
    orderLabel,
    patternLabel,
    patternSummary,
  } from '$lib/i18n';
  import {
    NO_OPTIONS,
    optionsSet,
    stepObstacleLaps,
    stepProgress,
    type JobOptionsDraft,
    type JobOptionsSpec,
  } from '$lib/mower/job-options';
  import {
    ANGLE_MAX,
    ANGLE_STEP,
    BORDER_LAPS_MAX,
    angleApplies,
    minLaps,
    stepSpacing,
    toPattern,
    type PatternDraft,
    type PatternName,
    type SpacingRange,
  } from '$lib/mower/mow-pattern';
  import type { Range } from '$lib/mower/limits';
  import { BLADE_STEP_MM, SPEED_STEP_MPS, type SetpointDraft } from '$lib/mower/setpoints';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    /** Patterns the mower runs natively, in sheet order. */
    offered: readonly PatternName[];
    /** The model's path spacing range in mm, or null (no spacing control). */
    spacing: SpacingRange | null;
    draft: PatternDraft;
    remember: boolean;
    /** Current blade height and speed, already formatted with their units (null: unknown). */
    blade?: string | null;
    speed?: string | null;
    /** Close the sheet and show the blade and speed steppers. */
    onadjust?: () => void;
    /** Blade height and speed of the start (bindable); shown with their ranges. */
    setpoints?: SetpointDraft;
    bladeRange?: Range | null;
    speedRange?: Range | null;
    /** The job options the mower offers, or null (no "Mehr"). */
    options?: JobOptionsSpec | null;
  }
  let {
    offered,
    spacing,
    draft = $bindable(),
    remember = $bindable(true),
    blade = null,
    speed = null,
    onadjust,
    setpoints = $bindable({ bladeMm: null, speedMps: null }),
    bladeRange = null,
    speedRange = null,
    options = null,
  }: Props = $props();

  const MM = 'mm';
  const MPS = 'm/s';
  const withSetpoints = $derived(bladeRange !== null || speedRange !== null);

  /** Phones: the settings below the summary are folded away until "Anpassen". */
  let expanded = $state(false);

  /** Units are not translated (see `$lib/i18n`). */
  const CM = 'cm';
  const switchStates = $derived({ on: m.switch_on(), off: m.switch_off() });
  const summary = $derived(patternSummary(toPattern(draft)));
  const withAngle = $derived(angleApplies(draft.name));
  const lapsFloor = $derived(minLaps(draft.name));

  function choose(name: PatternName): void {
    draft = { ...draft, name, laps: Math.max(draft.laps, minLaps(name)) };
  }

  function turn(direction: 1 | -1): void {
    const next = Math.min(ANGLE_MAX, Math.max(0, draft.angle + direction * ANGLE_STEP));
    draft = { ...draft, angle: next };
  }

  function laps(direction: 1 | -1): void {
    const next = Math.min(BORDER_LAPS_MAX, Math.max(lapsFloor, draft.laps + direction));
    draft = { ...draft, laps: next };
  }

  function space(direction: 1 | -1): void {
    if (spacing) draft = { ...draft, spacingMm: stepSpacing(draft.spacingMm, direction, spacing) };
  }

  /** "Mehr": the job options, folded away until asked for. */
  let moreOpen = $state(false);
  const opts = $derived(draft.options ?? NO_OPTIONS);
  const optionCount = $derived(optionsSet(draft.options));

  function setOptions(change: Partial<JobOptionsDraft>): void {
    draft = { ...draft, options: { ...opts, ...change } };
  }

  /** The angle the stripes of the glyph run at (diagonal adds 45°, as the mower does). */
  function glyphAngle(name: PatternName): number {
    return name === 'diagonal' ? draft.angle + 45 : draft.angle;
  }
</script>

<section
  class="mow"
  class:expanded
  aria-labelledby="start-section-title"
  data-testid="start-settings"
>
  <div class="head">
    <h3 class="title" id="start-section-title">{m.start_section()}</h3>
    <button
      type="button"
      class="toggle"
      aria-expanded={expanded}
      aria-controls="start-settings-body"
      data-testid="start-settings-toggle"
      onclick={() => (expanded = !expanded)}
      >{expanded ? m.start_settings_less() : m.start_settings_more()}</button
    >
  </div>
  <p class="summary mono" data-testid="start-summary">{summary}</p>

  <div class="body" id="start-settings-body">
    <div
      class="patterns"
      role="radiogroup"
      aria-label={m.start_pattern()}
      data-testid="start-pattern"
    >
      {#each offered as name (name)}
        <button
          type="button"
          role="radio"
          class="pattern"
          aria-checked={draft.name === name}
          data-value={name}
          data-testid="start-pattern-{name}"
          onclick={() => choose(name)}
        >
          <svg class="glyph" viewBox="-12 -12 24 24" aria-hidden="true">
            <rect class="frame" x="-10" y="-10" width="20" height="20" rx="2" />
            {#if name === 'edge_only'}
              <rect class="lane" x="-6.5" y="-6.5" width="13" height="13" rx="1" />
            {:else if name === 'random_angle'}
              <path class="lane" d="M-8 3 L-2 -7 M-6 8 L7 -3 M1 8 L8 2" />
            {:else}
              <clipPath id="clip-{name}"><rect x="-9" y="-9" width="18" height="18" /></clipPath>
              <g clip-path="url(#clip-{name})">
                <g transform="rotate({glyphAngle(name)})">
                  <path class="lane" d="M-14 -6 H14 M-14 0 H14 M-14 6 H14" />
                  {#if name === 'checkerboard'}
                    <path class="lane cross" d="M-6 -14 V14 M0 -14 V14 M6 -14 V14" />
                  {/if}
                </g>
              </g>
            {/if}
          </svg>
          <span class="name">{patternLabel(name)}</span>
        </button>
      {/each}
    </div>

    <div class="dials">
      {#if withAngle}
        <div
          class="dial"
          role="group"
          aria-labelledby="start-angle-label"
          data-testid="start-angle"
        >
          <span class="label" id="start-angle-label">{m.start_angle()}</span>
          <div class="row">
            <button
              type="button"
              class="nudge"
              aria-label={m.start_angle_lower()}
              disabled={draft.angle <= 0}
              onclick={() => turn(-1)}>−</button
            >
            <output class="value" data-value={draft.angle} aria-live="polite"
              >{formatNumber(draft.angle)}<span class="unit">°</span></output
            >
            <button
              type="button"
              class="nudge"
              aria-label={m.start_angle_raise()}
              disabled={draft.angle >= ANGLE_MAX}
              onclick={() => turn(1)}>+</button
            >
          </div>
        </div>
      {/if}
      <div
        class="dial"
        role="group"
        aria-labelledby="start-laps-label"
        data-testid="start-border-laps"
      >
        <span class="label" id="start-laps-label">{m.start_border_laps()}</span>
        <div class="row">
          <button
            type="button"
            class="nudge"
            aria-label={m.start_laps_fewer()}
            disabled={draft.laps <= lapsFloor}
            onclick={() => laps(-1)}>−</button
          >
          <output class="value" data-value={draft.laps} aria-live="polite">{draft.laps}</output>
          <button
            type="button"
            class="nudge"
            aria-label={m.start_laps_more()}
            disabled={draft.laps >= BORDER_LAPS_MAX}
            onclick={() => laps(1)}>+</button
          >
        </div>
      </div>
      {#if spacing}
        <div
          class="dial"
          role="group"
          aria-labelledby="start-spacing-label"
          data-testid="start-spacing"
        >
          <span class="label" id="start-spacing-label">{m.start_spacing()}</span>
          <div class="row">
            <button
              type="button"
              class="nudge"
              aria-label={m.start_spacing_narrower()}
              disabled={draft.spacingMm !== null && draft.spacingMm <= spacing[0]}
              onclick={() => space(-1)}>−</button
            >
            <output class="value" data-value={draft.spacingMm ?? ''} aria-live="polite">
              {#if draft.spacingMm === null}
                <span class="auto">{m.start_spacing_default()}</span>
              {:else}
                {formatNumber(draft.spacingMm / 10)}<span class="unit">{CM}</span>
              {/if}
            </output>
            <button
              type="button"
              class="nudge"
              aria-label={m.start_spacing_wider()}
              disabled={draft.spacingMm !== null && draft.spacingMm >= spacing[1]}
              onclick={() => space(1)}>+</button
            >
          </div>
          <span class="range mono">
            {formatNumber(spacing[0] / 10)}–{formatNumber(spacing[1] / 10)}
            {CM}
            {#if draft.spacingMm !== null}
              <button
                type="button"
                class="reset"
                onclick={() => (draft = { ...draft, spacingMm: null })}
                >{m.start_spacing_reset()}</button
              >
            {/if}
          </span>
        </div>
      {/if}
    </div>
    {#if draft.name === 'diagonal'}
      <p class="hint">{m.start_angle_diagonal()}</p>
    {/if}

    {#if withSetpoints}
      <div class="steppers" data-testid="start-setpoints">
        {#if bladeRange}
          <div class="stepper" data-testid="start-blade">
            <Stepper
              id="start-blade-stepper"
              label={m.detail_blade()}
              value={setpoints.bladeMm}
              min={bladeRange[0]}
              max={bladeRange[1]}
              step={BLADE_STEP_MM}
              unit={MM}
              format={(v) => formatNumber(v)}
              decreaseLabel={m.blade_lower()}
              increaseLabel={m.blade_raise()}
              immediate
              oncommit={(v) => (setpoints = { ...setpoints, bladeMm: v })}
            />
          </div>
        {/if}
        {#if speedRange}
          <div class="stepper" data-testid="start-speed">
            <Stepper
              id="start-speed-stepper"
              label={m.start_speed()}
              value={setpoints.speedMps}
              min={speedRange[0]}
              max={speedRange[1]}
              step={SPEED_STEP_MPS}
              unit={MPS}
              decimals={2}
              format={(v) => formatNumber(v, 2)}
              decreaseLabel={m.speed_lower()}
              increaseLabel={m.speed_raise()}
              immediate
              oncommit={(v) => (setpoints = { ...setpoints, speedMps: v })}
            />
          </div>
        {/if}
      </div>
      <p class="hint small">{m.start_setpoints_sent()}</p>
    {:else}
      <div class="setpoints" data-testid="start-setpoints">
        <span class="readings mono">
          {m.detail_blade()}
          <strong>{blade ?? '--'}</strong>
          · {m.detail_speed()}
          <strong>{speed ?? '--'}</strong>
        </span>
        {#if onadjust}
          <button
            type="button"
            class="adjust"
            onclick={onadjust}
            data-testid="start-setpoints-adjust">{m.start_setpoints_adjust()}</button
          >
        {/if}
        <span class="hint small">{m.start_setpoints()}</span>
      </div>
    {/if}

    {#if options}
      <div class="more" class:open={moreOpen} data-testid="start-more">
        <button
          type="button"
          class="more-toggle"
          aria-expanded={moreOpen}
          aria-controls="start-more-body"
          data-testid="start-more-toggle"
          onclick={() => (moreOpen = !moreOpen)}
        >
          <span>{optionCount ? m.start_more_count({ count: optionCount }) : m.start_more()}</span>
          <svg class="chevron" viewBox="0 0 16 16" aria-hidden="true">
            <path d="M4 6 L8 10 L12 6" />
          </svg>
        </button>
        {#if moreOpen}
          <div class="more-body" id="start-more-body">
            {#if options.detection.length}
              <div class="choice" data-testid="start-obstacle">
                <span class="label" id="start-obstacle-label">{m.start_obstacle()}</span>
                <div class="segments" role="radiogroup" aria-labelledby="start-obstacle-label">
                  {#each options.detection as mode (mode)}
                    <button
                      type="button"
                      role="radio"
                      class="segment"
                      aria-checked={opts.detection === mode}
                      data-value={mode}
                      data-testid="start-obstacle-{mode}"
                      onclick={() =>
                        setOptions({ detection: opts.detection === mode ? null : mode })}
                      >{obstacleLabel(mode)}</button
                    >
                  {/each}
                </div>
                <span class="range">
                  {#if opts.detection === null}
                    <span class="auto">{m.start_option_default()}</span>
                  {:else}
                    <span
                      >{opts.detection === 'sensitive'
                        ? m.start_obstacle_hint_sensitive()
                        : ''}</span
                    >
                    <button
                      type="button"
                      class="reset"
                      onclick={() => setOptions({ detection: null })}
                      >{m.start_option_reset()}</button
                    >
                  {/if}
                </span>
              </div>
            {/if}
            {#if options.orders.length}
              <div class="choice" data-testid="start-order">
                <span class="label" id="start-order-label">{m.start_order()}</span>
                <div class="segments" role="radiogroup" aria-labelledby="start-order-label">
                  {#each options.orders as order (order)}
                    <button
                      type="button"
                      role="radio"
                      class="segment"
                      aria-checked={opts.order === order}
                      data-value={order}
                      data-testid="start-order-{order}"
                      onclick={() => setOptions({ order: opts.order === order ? null : order })}
                      >{orderLabel(order)}</button
                    >
                  {/each}
                </div>
                <span class="range">
                  {#if opts.order === null}
                    <span class="auto">{m.start_option_default()}</span>
                  {:else}
                    <span></span>
                    <button type="button" class="reset" onclick={() => setOptions({ order: null })}
                      >{m.start_option_reset()}</button
                    >
                  {/if}
                </span>
              </div>
            {/if}
            <div class="dials">
              {#if options.obstacleLaps}
                {@const limits = options.obstacleLaps}
                <div
                  class="dial"
                  role="group"
                  aria-labelledby="start-nogo-label"
                  data-testid="start-nogo-laps"
                >
                  <span class="label" id="start-nogo-label">{m.start_nogo_laps()}</span>
                  <div class="row">
                    <button
                      type="button"
                      class="nudge"
                      aria-label={m.start_nogo_fewer()}
                      disabled={opts.obstacleLaps !== null && opts.obstacleLaps <= limits[0]}
                      onclick={() =>
                        setOptions({
                          obstacleLaps: stepObstacleLaps(opts.obstacleLaps, -1, limits),
                        })}>−</button
                    >
                    <output class="value" data-value={opts.obstacleLaps ?? ''} aria-live="polite">
                      {#if opts.obstacleLaps === null}
                        <span class="auto">{m.start_option_default()}</span>
                      {:else}
                        {opts.obstacleLaps}
                      {/if}
                    </output>
                    <button
                      type="button"
                      class="nudge"
                      aria-label={m.start_nogo_more()}
                      disabled={opts.obstacleLaps !== null && opts.obstacleLaps >= limits[1]}
                      onclick={() =>
                        setOptions({
                          obstacleLaps: stepObstacleLaps(opts.obstacleLaps, 1, limits),
                        })}>+</button
                    >
                  </div>
                  {#if opts.obstacleLaps !== null}
                    <span class="range">
                      <span></span>
                      <button
                        type="button"
                        class="reset"
                        onclick={() => setOptions({ obstacleLaps: null })}
                        >{m.start_option_reset()}</button
                      >
                    </span>
                  {/if}
                </div>
              {/if}
              {#if options.progress}
                {@const limits = options.progress}
                <div
                  class="dial"
                  role="group"
                  aria-labelledby="start-progress-label"
                  data-testid="start-progress"
                >
                  <span class="label" id="start-progress-label">{m.start_progress()}</span>
                  <div class="row">
                    <button
                      type="button"
                      class="nudge"
                      aria-label={m.start_progress_lower()}
                      disabled={opts.progress <= limits[0]}
                      onclick={() =>
                        setOptions({ progress: stepProgress(opts.progress, -1, limits) })}>−</button
                    >
                    <output class="value" data-value={opts.progress} aria-live="polite"
                      >{formatPercent(opts.progress)}</output
                    >
                    <button
                      type="button"
                      class="nudge"
                      aria-label={m.start_progress_raise()}
                      disabled={opts.progress >= limits[1]}
                      onclick={() =>
                        setOptions({ progress: stepProgress(opts.progress, 1, limits) })}>+</button
                    >
                  </div>
                  <span class="meter" style:--progress="{opts.progress}%" aria-hidden="true"></span>
                </div>
              {/if}
            </div>
            {#if options.progress}
              <p class="hint small">{m.start_progress_hint()}</p>
            {/if}
            <p class="hint small">{m.start_more_hint()}</p>
          </div>
        {/if}
      </div>
    {/if}

    <Switch
      id="start-remember"
      testid="start-remember"
      label={m.start_remember()}
      hint={m.start_remember_hint()}
      states={switchStates}
      bind:checked={remember}
    />
  </div>
</section>

<style>
  .mow {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    margin-block-start: var(--md-space-3);
    padding-block-start: var(--md-space-3);
    border-block-start: var(--md-hairline) solid var(--md-color-line-strong);
  }

  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-3);
  }

  .toggle {
    min-block-size: var(--md-target);
    padding: 0 var(--md-space-2);
    border: 0;
    background: none;
    color: var(--md-color-ink);
    font: inherit;
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-medium);
    text-decoration: underline;
    text-underline-offset: 3px;
    cursor: pointer;
  }

  .body {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
  }

  /* Phones: folded until "Anpassen"; from 1024 px always open. */
  .mow:not(.expanded) .body {
    display: none;
  }

  @media (min-width: 1024px) {
    .toggle {
      display: none;
    }

    .mow:not(.expanded) .body {
      display: flex;
    }

    .mow {
      margin-block-start: 0;
      padding-block-start: 0;
      border-block-start: 0;
    }
  }

  .steppers {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: var(--md-space-2);
  }

  .stepper {
    min-inline-size: 0;
  }

  .title,
  .label {
    margin: 0;
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    color: var(--md-color-ink-muted);
  }

  .patterns {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(4.5rem, 1fr));
    gap: var(--md-space-1);
  }

  .pattern {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    min-height: var(--md-target);
    padding: var(--md-space-1);
    border: var(--md-border-control) solid var(--md-color-line);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    cursor: pointer;
    transition: border-color var(--md-motion-fast) var(--md-ease);
  }

  .pattern[aria-checked='true'] {
    border-color: var(--md-color-ink);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .glyph {
    inline-size: 1.5rem;
    block-size: 1.5rem;
  }

  .frame {
    fill: none;
    stroke: currentcolor;
    stroke-opacity: 0.35;
    stroke-width: 1;
  }

  .lane {
    fill: none;
    stroke: currentcolor;
    stroke-width: 1.6;
    stroke-linecap: round;
  }

  .cross {
    stroke-opacity: 0.55;
  }

  .name {
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-medium);
    text-align: center;
  }

  .dials {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(10.5rem, 1fr));
    gap: var(--md-space-2);
  }

  .dial {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    padding: var(--md-space-1) var(--md-space-2);
    background: var(--md-color-surface-sunken);
    min-inline-size: 0;
  }

  .row {
    display: grid;
    grid-template-columns: var(--md-target) minmax(0, 1fr) var(--md-target);
    align-items: center;
    gap: var(--md-space-1);
  }

  .nudge {
    inline-size: var(--md-target);
    block-size: var(--md-target);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-24);
    line-height: 1;
    cursor: pointer;
    touch-action: manipulation;
  }

  .nudge:disabled {
    border-style: dashed;
    color: var(--md-color-ink-muted);
    cursor: not-allowed;
  }

  .value {
    display: flex;
    align-items: baseline;
    justify-content: center;
    gap: 0.15em;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-32);
    font-weight: var(--md-weight-medium);
    font-variant-numeric: tabular-nums slashed-zero;
  }

  .unit,
  .auto {
    font-size: 0.6em;
    font-weight: var(--md-weight-regular);
    color: var(--md-color-ink-muted);
  }

  .auto {
    font-family: var(--md-font-sans);
    font-size: var(--md-font-size-14);
  }

  .range {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-2);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    line-height: var(--md-line-16);
  }

  .reset,
  .adjust {
    padding: 0;
    border: 0;
    background: none;
    color: var(--md-color-ink);
    font: inherit;
    text-decoration: underline;
    text-underline-offset: 3px;
    cursor: pointer;
  }

  .more {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    border-block: var(--md-hairline) solid var(--md-color-line);
  }

  .more-toggle {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
    min-block-size: var(--md-target);
    padding: 0;
    border: 0;
    background: none;
    color: var(--md-color-ink);
    font: inherit;
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
  }

  .chevron {
    inline-size: 1rem;
    block-size: 1rem;
    fill: none;
    stroke: currentcolor;
    stroke-width: 1.6;
    stroke-linecap: round;
    stroke-linejoin: round;
    transition: transform var(--md-motion-fast) var(--md-ease);
  }

  .more.open .chevron {
    transform: rotate(180deg);
  }

  @media (prefers-reduced-motion: reduce) {
    .chevron,
    .segment {
      transition: none;
    }
  }

  .more-body {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
    padding-block-end: var(--md-space-3);
  }

  .choice {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
  }

  .segments {
    display: grid;
    grid-auto-columns: minmax(0, 1fr);
    grid-auto-flow: column;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    overflow: hidden;
  }

  .segment {
    min-block-size: var(--md-target);
    padding: 0 var(--md-space-1);
    border: 0;
    border-inline-start: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font: inherit;
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-medium);
    cursor: pointer;
    transition: background-color var(--md-motion-fast) var(--md-ease);
  }

  .segment:first-child {
    border-inline-start: 0;
  }

  .segment[aria-checked='true'] {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .segment:focus-visible {
    outline-offset: -3px;
  }

  .meter {
    display: block;
    block-size: 3px;
    background: linear-gradient(
      to right,
      var(--md-color-ink) var(--progress),
      var(--md-color-line) var(--progress)
    );
  }

  .summary {
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 3px solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
    font-weight: var(--md-weight-medium);
  }

  .setpoints {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--md-space-1) var(--md-space-3);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  .readings strong {
    font-weight: var(--md-weight-semibold);
  }

  .adjust {
    min-height: var(--md-target);
    font-weight: var(--md-weight-medium);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  .small {
    flex-basis: 100%;
    font-size: var(--md-font-size-12);
  }

  .mono {
    font-family: var(--md-font-mono);
  }
</style>
