<!--
  "Mähwinkel drehen" in the start sheet (ADR 0061): a switch, the step chips and what the start
  will mow with, next to a small dial. With rotation on, the start turns the last angle the
  picked areas were mowed with by the step (or uses the sheet's angle when none is known) and
  the sheet reports the angle afterwards; with it off the start mows at the angle above and the
  rotation stays where it is. The flow (`StartSheetFlow`) holds the state; nothing shows when
  the pattern has no direction or the Scheduler is not reachable.
-->
<script lang="ts">
  import { Switch } from '@mowdeck/ui';

  import AngleDial from '#lib/components/AngleDial.svelte';
  import { formatNumber } from '#lib/i18n.js';
  import type { StartSheetFlow } from '#lib/mower/start-sheet.svelte.js';
  import { m } from '#lib/paraglide/messages.js';
  import { STEP_PRESETS, angleText, lastAngle, stepSequence } from '#lib/schedules/rotation.js';

  interface Props {
    flow: StartSheetFlow;
  }
  let { flow }: Props = $props();

  const switchStates = $derived({ on: m.switch_on(), off: m.switch_off() });
  const known = $derived(
    flow.rotation && flow.picked.length ? lastAngle(flow.rotation.areas, flow.picked) : null,
  );
  /** This start and the three after it, for the dial. */
  const upcoming = $derived(
    flow.rotatedAngle === null ? [] : stepSequence(flow.rotatedAngle, flow.rotateStep, 0, 3),
  );
  const dialAngles = $derived(flow.rotatedAngle === null ? [] : [flow.rotatedAngle, ...upcoming]);
  const angle = $derived(
    flow.rotatedAngle === null ? '' : angleText(flow.rotatedAngle, formatNumber),
  );
  const step = $derived(formatNumber(flow.rotateStep, flow.rotateStep % 1 ? 1 : 0));
</script>

{#if flow.rotationFailed}
  <p class="hint small" data-testid="start-rotate-unavailable">{m.start_rotate_unavailable()}</p>
{:else if flow.rotationOffered}
  <section class="rotate" data-testid="start-rotate">
    <Switch
      id="start-rotate"
      testid="start-rotate-switch"
      label={m.start_rotate()}
      states={switchStates}
      bind:checked={flow.rotate}
    />
    {#if flow.rotate}
      <div class="body">
        <AngleDial
          angles={dialAngles}
          size={76}
          readout={false}
          label={m.sched_rotation_dial({
            angles: dialAngles.map((a) => angleText(a, formatNumber)).join(', '),
          })}
        />
        <div class="text">
          <div class="steps" role="group" aria-label={m.start_rotate_step()}>
            {#each STEP_PRESETS as preset (preset)}
              <button
                type="button"
                class="chip mono"
                aria-pressed={flow.rotateStep === preset}
                data-testid="start-rotate-step-{preset}"
                onclick={() => (flow.rotateStep = preset)}>+{preset}°</button
              >
            {/each}
          </div>
          <p class="hint small" data-testid="start-rotate-hint">
            {#if flow.rotatedAngle === null}
              {m.start_rotate_hint_pick()}
            {:else if known === null}
              {m.start_rotate_hint_first({ angle })}
            {:else}
              {m.start_rotate_hint_next({ angle, step })}
            {/if}
          </p>
        </div>
      </div>
    {:else}
      <p class="hint small">{m.start_rotate_hint_off()}</p>
    {/if}
  </section>
{/if}

<style>
  .rotate {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    padding-top: var(--md-space-3);
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .body {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: var(--md-space-3);
  }

  .text {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    min-width: 0;
  }

  .steps {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1);
  }

  .chip {
    min-width: 52px;
    min-height: var(--md-target);
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
    cursor: pointer;
  }

  .chip[aria-pressed='true'] {
    border-color: var(--md-state-mowing);
    background: var(--md-state-mowing-bg);
    font-weight: var(--md-weight-semibold);
    box-shadow: inset 0 -3px 0 var(--md-state-mowing);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .small {
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }
</style>
