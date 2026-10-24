<!--
  The "Mähen" section of the start sheet: a summary line, the mowing options of the start
  (`MowOptions`, shared with the schedule editor: the native patterns the mower reports in
  `capabilities.native_patterns`, angle, border laps, the path spacing with a model range, blade
  height and speed steppers with the model's ranges, and behind "Mehr" (`start-more-toggle`) the
  job options the mower declares in `capabilities.start_options`) and "remember as default".
  Blade height and speed are mower setpoints (separate commands, not part of the defaults):
  without the model's ranges the section shows the current values and jumps to the page's
  steppers. An option the mower does not declare is not shown at all; an option left empty keeps
  the mower's own setting, and the start progress belongs to this start only (never remembered).
  Layout: on phones the section is collapsed to the summary line plus "Anpassen"
  (`start-settings-toggle`); expanded it shows the pattern tiles, then the numeric settings. From
  1024 px it is always open (the toggle is hidden) and the sheet puts it beside the area list.
  The page owns the draft (`$lib/mower/mow-pattern`), the start and saving the defaults.
-->
<script lang="ts">
  import { Switch } from '@mowdeck/ui';

  import MowOptions from '$lib/components/MowOptions.svelte';
  import { patternSummary } from '$lib/i18n';
  import type { JobOptionsSpec } from '$lib/mower/job-options';
  import type { Range } from '$lib/mower/limits';
  import {
    toPattern,
    type PatternDraft,
    type PatternName,
    type SpacingRange,
  } from '$lib/mower/mow-pattern';
  import type { SetpointDraft } from '$lib/mower/setpoints';
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

  /** Phones: the settings below the summary are folded away until "Anpassen". */
  let expanded = $state(false);

  const switchStates = $derived({ on: m.switch_on(), off: m.switch_off() });
  const summary = $derived(patternSummary(toPattern(draft)));
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
    <MowOptions {offered} {spacing} {options} {bladeRange} {speedRange} bind:draft bind:setpoints>
      {#snippet setpointsFallback()}
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
      {/snippet}
    </MowOptions>

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

  .title {
    margin: 0;
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    color: var(--md-color-ink-muted);
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
    padding: 0;
    border: 0;
    background: none;
    color: var(--md-color-ink);
    font: inherit;
    font-weight: var(--md-weight-medium);
    text-decoration: underline;
    text-underline-offset: 3px;
    cursor: pointer;
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
