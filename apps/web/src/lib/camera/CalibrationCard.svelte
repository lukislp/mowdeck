<!--
  The panorama's calibration card (ADR 0037). A method chooser: "Manuell" keeps the sliders
  (overlap, vertical offset, mirror; "Auto" suggests the overlap from the picture edges) and saves
  every change at once; "Automatisch (lokal)" (features in the browser) and "KI-gestützt" (the
  vision model supplies points, the homography is fitted here) capture the current pair with
  "Kalibrieren" and show the result with its confidence, the inlier count and a before/after
  preview on the captured frames, until it is applied or discarded. "Zurücksetzen" returns to the
  default sliders. The flow (the "Auto" overlap estimate included) lives in
  `calibration/flow.svelte.ts`, the drawing in `panorama-renderer.ts`; this component only wires
  them to markup and hands the flow its DOM grabs.
-->
<script lang="ts">
  import { Button, Switch } from '@mowdeck/ui';
  import { untrack } from 'svelte';

  import { capturePair } from '$lib/camera/calibration/capture';
  import { CalibrationFlow } from '$lib/camera/calibration/flow.svelte';
  import {
    calibrationMethods,
    methodLabel,
    type CalibrationMethods,
  } from '$lib/camera/calibration/methods';
  import {
    defaultCalibration,
    manualCalibration,
    sliderSettings,
  } from '$lib/camera/calibration/storage';
  import {
    CHOOSABLE_METHODS,
    type CalibrationFailure,
    type ChoosableMethod,
    type PanoramaCalibration,
  } from '$lib/camera/calibration/types';
  import { OFFSET_MAX, OVERLAP_MAX, type PanoramaSettings } from '$lib/camera/panorama';
  import { grabGray, paintPanorama, type PanoramaSource } from '$lib/camera/panorama-renderer';
  import { formatDateTime, formatNumber } from '$lib/i18n';
  import { m } from '$lib/paraglide/messages';
  import { PROVIDER_SETTINGS_ROUTE, visionCalls } from '$lib/vision/api';
  import { visionCorrespondences } from '$lib/vision/calibration';
  import { formatCost } from '$lib/vision/labels';

  type ProviderState = 'unknown' | 'ok' | 'none';

  interface Props {
    mowerId: string;
    calibration: PanoramaCalibration;
    onchange: (calibration: PanoramaCalibration) => void;
    /** The left and right live picture. */
    sources: () => readonly [PanoramaSource | null, PanoramaSource | null];
    /** Both tiles show a picture. */
    live: boolean;
    background?: string;
    /** The methods (tests hand in their own). */
    methods?: CalibrationMethods;
    /** Which AI provider a request would use (tests hand in their own). */
    provider?: () => Promise<'own' | 'hosted' | 'none'>;
  }

  let {
    mowerId,
    calibration,
    onchange,
    sources,
    live,
    background = '#000',
    methods,
    provider = async () => (await visionCalls().provider()).effective,
  }: Props = $props();

  const HINTS: Record<ChoosableMethod, () => string> = {
    manual: () => m.camera_calibration_method_manual_hint(),
    features: () => m.camera_calibration_method_features_hint(),
    ai: () => m.camera_calibration_method_ai_hint(),
  };
  const FAILURES: Record<CalibrationFailure, () => string> = {
    no_picture: () => m.camera_calibration_needs_live(),
    no_match: () => m.camera_calibration_error_no_match(),
    implausible: () => m.camera_calibration_error_implausible(),
    provider_missing: () => m.camera_calibration_error_provider_missing(),
    quota: () => m.camera_calibration_error_quota(),
    not_in_plan: () => m.camera_calibration_error_not_in_plan(),
    unreachable: () => m.camera_calibration_error_unreachable(),
    failed: () => m.camera_calibration_error_failed(),
  };

  // The mirror switch: the slider setting in "Manuell", a hint for the local features otherwise.
  let mirror = $state(untrack(() => calibration.mirror));
  const sliders = $derived(sliderSettings(calibration));
  let providerState = $state<ProviderState>('unknown');
  let beforeCanvas = $state<HTMLCanvasElement | null>(null);
  let afterCanvas = $state<HTMLCanvasElement | null>(null);

  const flow = new CalibrationFlow({
    methods: untrack(() => methods ?? calibrationMethods(visionCorrespondences(mowerId))),
    capture: () => {
      const [left, right] = sources();
      return capturePair(left, right);
    },
    grab: () => {
      const [left, right] = sources();
      return [grabGray(left), grabGray(right)];
    },
    options: () => ({ mirror, manual: { ...sliders, mirror } }),
  });
  flow.method = untrack(() =>
    calibration.method === 'correlation' ? 'features' : calibration.method,
  );

  const running = $derived(flow.phase === 'running');
  const result = $derived(flow.candidate);
  const aiBlocked = $derived(flow.method === 'ai' && providerState === 'none');

  // The AI method needs a provider: ask once, when it is chosen.
  $effect(() => {
    if (flow.method !== 'ai' || providerState !== 'unknown') return;
    void provider()
      .then((effective) => (providerState = effective === 'none' ? 'none' : 'ok'))
      .catch(() => (providerState = 'ok'));
  });

  // Before/after preview on the captured still frames.
  $effect(() => {
    const pair = flow.pair;
    const candidate = flow.candidate;
    if (!pair || !candidate) return;
    const scratch = document.createElement('canvas');
    const paint = (canvas: HTMLCanvasElement | null, homography: typeof candidate.homography) => {
      if (!canvas) return;
      paintPanorama(canvas, pair.leftCanvas, pair.rightCanvas, homography, {
        background,
        scratch: () => scratch,
      });
    };
    paint(
      beforeCanvas,
      untrack(() => calibration.homography),
    );
    paint(afterCanvas, candidate.homography);
  });

  function choose(method: ChoosableMethod): void {
    flow.choose(method);
  }

  function setSliders(patch: Partial<PanoramaSettings>): void {
    onchange(manualCalibration({ ...sliders, mirror, ...patch }));
  }

  function setMirror(checked: boolean): void {
    mirror = checked;
    if (flow.method === 'manual') setSliders({ mirror: checked });
  }

  /** One-shot estimate of the overlap from the two current frames (an approximation). */
  function autoOverlap(): void {
    const overlap = flow.autoOverlap();
    if (overlap !== null) setSliders({ overlap });
  }

  function apply(): void {
    const applied = flow.apply();
    if (!applied) return;
    mirror = applied.mirror;
    onchange(applied);
  }

  function reset(): void {
    flow.discard();
    mirror = false;
    onchange(defaultCalibration());
  }

  const percent = (value: number): string =>
    m.camera_panorama_percent({ value: formatNumber(Math.round(value * 100)) });
</script>

<section
  class="calibrate"
  aria-label={m.camera_panorama_calibrate()}
  data-testid="camera-panorama-calibration"
  data-method={flow.method}
  data-phase={flow.phase}
>
  <div
    class="methods"
    role="radiogroup"
    aria-label={m.camera_calibration_method()}
    data-testid="camera-calibration-method"
  >
    {#each CHOOSABLE_METHODS as id (id)}
      <button
        type="button"
        role="radio"
        class="method"
        aria-checked={flow.method === id}
        data-testid="camera-calibration-method-{id}"
        onclick={() => choose(id)}
      >
        <span class="method-name">{methodLabel(id)}</span>
        <span class="method-hint">{HINTS[id]()}</span>
      </button>
    {/each}
  </div>

  {#if flow.method === 'manual'}
    <label class="slider">
      <span class="slider-head">
        <span>{m.camera_panorama_overlap()}</span>
        <span class="mono">{percent(sliders.overlap)}</span>
      </span>
      <input
        type="range"
        min="0"
        max={Math.round(OVERLAP_MAX * 100)}
        step="1"
        value={Math.round(sliders.overlap * 100)}
        data-testid="camera-panorama-overlap"
        oninput={(event) =>
          setSliders({ overlap: Number((event.currentTarget as HTMLInputElement).value) / 100 })}
      />
    </label>
    <label class="slider">
      <span class="slider-head">
        <span>{m.camera_panorama_offset()}</span>
        <span class="mono">{percent(sliders.offset)}</span>
      </span>
      <input
        type="range"
        min={-Math.round(OFFSET_MAX * 100)}
        max={Math.round(OFFSET_MAX * 100)}
        step="1"
        value={Math.round(sliders.offset * 100)}
        data-testid="camera-panorama-offset"
        oninput={(event) =>
          setSliders({ offset: Number((event.currentTarget as HTMLInputElement).value) / 100 })}
      />
    </label>
  {:else}
    <p class="hint" data-testid="camera-calibration-scene">{m.camera_calibration_scene()}</p>
  {/if}

  {#if flow.method !== 'ai'}
    <Switch
      id="camera-panorama-mirror"
      label={m.camera_panorama_mirror()}
      hint={m.camera_panorama_mirror_hint()}
      states={{ on: m.switch_on(), off: m.switch_off() }}
      checked={mirror}
      onchange={setMirror}
      testid="camera-panorama-mirror"
    />
  {/if}

  {#if flow.method === 'ai'}
    <p class="hint" data-testid="camera-calibration-ai-cost">{m.camera_calibration_ai_cost()}</p>
    {#if providerState === 'none'}
      <p class="notice" data-testid="camera-calibration-ai-missing">
        {m.camera_calibration_ai_missing()}
        <a href={PROVIDER_SETTINGS_ROUTE}>{m.camera_calibration_ai_settings()}</a>
      </p>
    {/if}
  {/if}

  <div class="actions">
    {#if flow.method === 'manual'}
      <Button
        variant="secondary"
        testid="camera-panorama-auto"
        disabled={!live}
        onclick={autoOverlap}>{m.camera_panorama_auto()}</Button
      >
    {:else}
      <Button
        variant="primary"
        testid="camera-calibration-run"
        disabled={!live || running || aiBlocked}
        onclick={() => void flow.run()}
        >{running ? m.camera_calibration_running() : m.camera_calibration_run()}</Button
      >
    {/if}
    <Button variant="ghost" testid="camera-panorama-reset" onclick={reset}
      >{m.camera_panorama_reset()}</Button
    >
  </div>

  {#if flow.method !== 'manual' && !live}
    <p class="hint" data-testid="camera-calibration-needs-live">
      {m.camera_calibration_needs_live()}
    </p>
  {/if}

  {#if flow.method === 'manual' && flow.auto}
    <p
      class="hint"
      role="status"
      data-testid="camera-panorama-auto-result"
      data-kind={flow.auto.kind}
    >
      {#if flow.auto.kind === 'failed'}
        {m.camera_panorama_auto_failed()}
      {:else}
        {m.camera_panorama_auto_done({
          percent: formatNumber(flow.auto.percent),
          score: formatNumber(flow.auto.score, 2),
        })}
        {#if flow.auto.kind === 'limit'}{m.camera_panorama_auto_limit()}{/if}
      {/if}
    </p>
  {/if}

  {#if flow.phase === 'error' && flow.failure}
    <p
      class="notice error"
      role="alert"
      data-testid="camera-calibration-error"
      data-reason={flow.failure}
    >
      {FAILURES[flow.failure]()}
      {#if flow.failure === 'provider_missing'}
        <a href={PROVIDER_SETTINGS_ROUTE}>{m.camera_calibration_ai_settings()}</a>
      {/if}
      {#if flow.failure === 'failed' && flow.detail}
        <small class="detail">{flow.detail}</small>
      {/if}
    </p>
  {/if}

  {#if result}
    <div
      class="result"
      role="status"
      data-testid="camera-calibration-result"
      data-method={result.method}
      data-confidence={Math.round(result.confidence * 100)}
      data-inliers={result.inliers}
    >
      <p class="result-title">
        {m.camera_calibration_result_title({ method: methodLabel(result.method) })}
      </p>
      <div class="meter" aria-hidden="true">
        <span class="meter-fill" style:inline-size="{Math.round(result.confidence * 100)}%"></span>
      </div>
      <p class="readout mono">
        <span
          >{m.camera_calibration_confidence({
            percent: formatNumber(Math.round(result.confidence * 100)),
          })}</span
        >
        {#if result.matches > 0}
          <span>
            {m.camera_calibration_inliers({
              inliers: formatNumber(result.inliers),
              matches: formatNumber(result.matches),
            })}
          </span>
        {/if}
        {#if result.model}<span>{m.camera_calibration_model({ model: result.model })}</span>{/if}
        {#if formatCost(result.costUsd)}
          <span>{m.camera_calibration_cost({ cost: formatCost(result.costUsd) ?? '' })}</span>
        {/if}
      </p>
      {#if result.fallbackFrom}
        <p class="hint" data-testid="camera-calibration-fallback">
          {m.camera_calibration_fallback()}
        </p>
      {/if}
      <div class="previews">
        <figure class="preview">
          <canvas bind:this={beforeCanvas} data-testid="camera-calibration-before"></canvas>
          <figcaption>{m.camera_calibration_before()}</figcaption>
        </figure>
        <figure class="preview">
          <canvas bind:this={afterCanvas} data-testid="camera-calibration-after"></canvas>
          <figcaption>{m.camera_calibration_after()}</figcaption>
        </figure>
      </div>
      <div class="actions">
        <Button variant="primary" testid="camera-calibration-apply" onclick={apply}
          >{m.camera_calibration_apply()}</Button
        >
        <Button variant="ghost" testid="camera-calibration-discard" onclick={() => flow.discard()}
          >{m.camera_calibration_discard()}</Button
        >
      </div>
    </div>
  {/if}

  {#if calibration.capturedAt}
    <p class="hint mono" data-testid="camera-calibration-active" data-method={calibration.method}>
      {m.camera_calibration_active({
        method: methodLabel(calibration.method),
        time: formatDateTime(calibration.capturedAt),
      })}
    </p>
  {/if}
  {#if flow.method === 'manual'}
    <p class="hint" data-testid="camera-panorama-note">{m.camera_panorama_note()}</p>
  {/if}
</section>

<style>
  .calibrate {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface-sunken);
  }

  .methods {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: var(--md-space-1);
  }

  .method {
    display: grid;
    gap: var(--md-space-0_5);
    align-content: start;
    min-height: var(--md-target);
    padding: var(--md-space-2) var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    text-align: start;
    cursor: pointer;
  }

  .method[aria-checked='true'] {
    border-color: var(--md-color-ink);
    box-shadow: inset 3px 0 0 var(--md-color-ink);
  }

  .method-name {
    font-weight: var(--md-weight-semibold);
  }

  .method-hint {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .slider {
    display: grid;
    gap: var(--md-space-1);
    font-size: var(--md-font-size-14);
  }

  .slider-head {
    display: flex;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .slider input {
    width: 100%;
    min-height: var(--md-target);
    accent-color: var(--md-color-ink);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .notice {
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-radius: var(--md-radius-1);
    background: var(--md-color-surface);
    font-size: var(--md-font-size-13);
  }

  .notice.error {
    border-inline-start: 3px solid var(--md-state-error);
  }

  .notice .detail {
    display: block;
    margin-block-start: var(--md-space-1);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    overflow-wrap: anywhere;
  }

  .result {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-3);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
  }

  .result-title {
    margin: 0;
    font-weight: var(--md-weight-semibold);
  }

  .meter {
    block-size: 6px;
    border-radius: var(--md-radius-round);
    background: var(--md-color-line);
    overflow: hidden;
  }

  .meter-fill {
    display: block;
    block-size: 100%;
    background: var(--md-state-mowing);
  }

  .readout {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-3);
    margin: 0;
    font-size: var(--md-font-size-12);
    font-variant-numeric: tabular-nums;
  }

  .previews {
    display: grid;
    gap: var(--md-space-2);
  }

  @container (min-width: 560px) {
    .previews {
      grid-template-columns: 1fr 1fr;
    }
  }

  .preview {
    display: grid;
    gap: var(--md-space-1);
    margin: 0;
  }

  .preview canvas {
    inline-size: 100%;
    block-size: auto;
    border-radius: var(--md-radius-1);
    background: var(--md-color-ink);
  }

  .preview figcaption {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .mono {
    font-family: var(--md-font-mono);
  }
</style>
