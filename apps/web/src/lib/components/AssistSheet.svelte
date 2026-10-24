<!--
  AI assist ("KI-Hilfe") for one mower: which provider answers, the analysis, the proposed
  maneuver (situation, confidence, readable steps, risks, rationale, expiry; for `resume` the
  paused job with its areas, done, current and next), the decision (approve drives the mower
  with the blades off, sends it home or continues the job; reject), the execution step by step and the
  outcome with a retry. A non-modal sheet above the command bar, so the emergency stop stays
  visible and usable the whole time. With both front cameras live in the camera panel (and the
  user's stereo preference on) both frames of the same moment are sent; the proposal then shows
  both images side by side with a "Stereo" mark. A recommendation below the confidence threshold
  is shown as what it is ("The AI recommends X, but only with 55 % confidence"): next to the dock
  fallback the user picks one of the two options (the fallback is preselected), alone it carries
  a low-confidence band; a recommendation that failed the safety check says why.
-->
<script lang="ts">
  import { Button, Icon, ProblemNotice, Sheet } from '@mowdeck/ui';
  import { untrack } from 'svelte';

  import ToneBadge from '#lib/components/ToneBadge.svelte';
  import { formatNumber } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import type { Glyph } from '#lib/schedules/labels.js';
  import {
    PROVIDER_SETTINGS_ROUTE,
    visionCalls,
    type ApprovalOption,
    type VisionTask,
  } from '#lib/vision/api.js';
  import { AssistStore } from '#lib/vision/assist.svelte.js';
  import { triggerNote } from '#lib/vision/auto.js';
  import {
    actionTitle,
    approveLabel,
    confidencePercent,
    expiryCountdown,
    fallbackReason,
    lowConfidenceLine,
    jobAreaLabel,
    photoProblemText,
    resumePlan,
    riskLabel,
    situationLabel,
    stateLabel,
    stateTone,
    stepDescription,
    stepOutcomeLabel,
    taskLabel,
  } from '#lib/vision/labels.js';
  import { visionProblemText } from '#lib/vision/problems.js';
  import {
    cameraImages,
    findCameraSources,
    findCameraVideo,
    stereoSources,
  } from '#lib/vision/snapshot.js';

  interface Props {
    mowerId: string;
    open: boolean;
    /** What a new request asks for (the entry button that opened the sheet). */
    task: VisionTask;
    /** A proposal to show (the inbox links with `?assist=<id>`). */
    proposalId?: string | null;
    onclose: () => void;
  }
  let { mowerId, open, task, proposalId = null, onclose }: Props = $props();

  const api = visionCalls();
  const assist = new AssistStore(api);

  let now = $state(Date.now());
  let imageUrls = $state<(string | null)[]>([]);
  let hasCamera = $state(false);
  let hasStereo = $state(false);

  $effect(() => {
    if (!open) return;
    const id = mowerId;
    const linked = proposalId;
    untrack(() => {
      hasCamera = findCameraVideo() !== null;
      hasStereo = stereoSources(findCameraSources()) !== null;
      void assist.open(id, linked);
    });
    return () => assist.close();
  });

  // The expiry countdown ticks every second while a proposal waits for approval.
  $effect(() => {
    if (assist.view !== 'proposed') return;
    now = Date.now();
    const timer = setInterval(() => (now = Date.now()), 1_000);
    return () => clearInterval(timer);
  });

  // The analysed snapshots (both of a stereo pair), fetched with the token, shown as blob URLs.
  const imageKey = $derived(
    assist.proposal ? `${assist.proposal.id}:${assist.proposal.image_count}` : '',
  );
  $effect(() => {
    if (!imageKey) return;
    const proposal = untrack(() => assist.proposal);
    if (!proposal || proposal.image_count < 1) return;
    let cancelled = false;
    const urls: string[] = [];
    // Up to three: a stereo pair plus the map rendering Vision adds after the frames.
    const count = Math.min(proposal.image_count, 3);
    imageUrls = Array.from({ length: count }, () => null);
    for (let index = 0; index < count; index += 1) {
      void api
        .image(proposal.id, index)
        .then((blob) => {
          if (cancelled) return;
          const url = URL.createObjectURL(blob);
          urls.push(url);
          imageUrls = imageUrls.map((old, i) => (i === index ? url : old));
        })
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
      for (const url of urls) URL.revokeObjectURL(url);
      imageUrls = [];
    };
  });

  /** The caption of an analysed image: its camera in the user's language. */
  function frameCaption(index: number): string | null {
    const frame = proposal?.frames?.[index];
    if (!frame) return null;
    if (frame.source === 'map') return m.vision_frame_map();
    if (frame.side === 'left') return m.camera_front_left();
    if (frame.side === 'right') return m.camera_front_right();
    return frame.camera ?? null;
  }

  const proposal = $derived(assist.proposal);
  const assessment = $derived(proposal?.assessment ?? null);
  const shownTask = $derived(proposal?.task ?? task);
  const countdown = $derived(expiryCountdown(proposal?.expires_at ?? null, now));
  const problemText = $derived(assist.problem ? visionProblemText(assist.problem) : null);
  const provider = $derived(assist.provider);

  const title = $derived(shownTask === 'find_dock' ? m.vision_find_dock() : m.vision_title());
  const photoProblem = $derived(proposal ? photoProblemText(proposal) : null);
  /** An automatic analysis that could not run without a camera photo. */
  const autoWithoutPhoto = $derived(
    !!photoProblem && proposal?.state === 'failed' && proposal.trigger === 'error',
  );

  // The options of a waiting proposal; with two the user picks one (the default preselected,
  // reset only when another proposal or another set of options arrives, not on every poll).
  const options = $derived(proposal?.state === 'proposed' ? (proposal.options ?? []) : []);
  const choice = $derived(options.length > 1);
  let selected = $state<ApprovalOption | null>(null);
  const optionKey = $derived(
    proposal && options.length ? `${proposal.id}:${options.map((o) => o.option).join(',')}` : '',
  );
  $effect(() => {
    void optionKey;
    untrack(() => {
      selected = options.find((o) => o.default)?.option ?? null;
    });
  });
  const selectedOption = $derived(options.find((o) => o.option === selected) ?? options[0]);
  /** What an approval runs now: the picked option, else the proposal's default action. */
  const shownAction = $derived(
    choice && selectedOption ? selectedOption.action : (proposal?.action ?? 'drive'),
  );
  const shownSteps = $derived(
    choice && selectedOption ? (selectedOption.steps ?? []).length : (proposal?.steps.length ?? 0),
  );
  /** The model's low-confidence recommendation offered alone (no fallback next to it). */
  const lowAlone = $derived(
    proposal?.state === 'proposed' && proposal.low_confidence && !proposal.fallback,
  );

  function approve(): Promise<void> {
    return assist.approve(choice && selected ? selected : undefined);
  }

  async function start(): Promise<void> {
    await assist.start(task, await cameraImages(document, { stereo: assist.stereo }));
  }

  async function retry(): Promise<void> {
    await assist.retry(await cameraImages(document, { stereo: assist.stereo }));
  }

  function glyphOf(tone: ReturnType<typeof stateTone>): Glyph {
    return tone === 'mowing'
      ? 'check'
      : tone === 'paused'
        ? 'clock'
        : tone === 'docked'
          ? 'dash'
          : 'alert';
  }

  function outcomeGlyph(outcome: string): Glyph {
    return outcome === 'done'
      ? 'check'
      : outcome === 'aborted'
        ? 'alert'
        : outcome === 'skipped'
          ? 'skip'
          : 'dash';
  }
</script>

<Sheet {open} {title} closeLabel={m.close()} {onclose} testid="assist-sheet">
  <div class="assist" data-view={assist.view} data-state={proposal?.state ?? ''}>
    <p class="provider" data-testid="assist-provider">
      {#if provider?.effective === 'own'}
        <span class="label">{m.vision_provider_own()}</span>
        <span class="mono">{provider.model}</span>
      {:else if provider?.effective === 'hosted'}
        <span class="label">{m.vision_provider_hosted()}</span>
        <span class="mono">{provider.hosted_model}</span>
      {:else if provider?.effective === 'none'}
        <span class="label">{m.vision_provider_none()}</span>
        <a class="link" href={PROVIDER_SETTINGS_ROUTE} data-testid="assist-provider-settings"
          >{m.vision_provider_settings()}</a
        >
      {:else if assist.providerProblem}
        <span class="label">{m.vision_provider_unknown()}</span>
      {:else}
        <span class="label">{m.loading()}</span>
      {/if}
    </p>

    <p class="blades">
      <Icon name="blade" size={18} />
      <span>{m.vision_blades_off()}</span>
    </p>

    {#if problemText && assist.problem}
      <ProblemNotice
        title={problemText.title}
        detail={problemText.detail}
        reference={assist.problem.correlationId}
        referenceLabel={m.problem_reference()}
      />
      {#if assist.providerMissing}
        <Button href={PROVIDER_SETTINGS_ROUTE} icon="settings" testid="assist-open-settings"
          >{m.vision_provider_settings()}</Button
        >
      {/if}
    {/if}

    {#if assist.view === 'loading'}
      <p class="quiet" role="status">{m.loading()}</p>
    {:else if assist.view === 'ready'}
      <p class="intro">
        {task === 'find_dock' ? m.vision_intro_find_dock() : m.vision_intro_recover()}
      </p>
      <p class="quiet small" data-testid="assist-image-source">
        {hasStereo && assist.stereo
          ? m.vision_image_stereo()
          : hasCamera
            ? m.vision_image_camera()
            : m.vision_image_mower()}
      </p>
    {:else if assist.view === 'analysing'}
      <div class="progress" role="status" data-testid="assist-analysing">
        <span class="bar" aria-hidden="true"></span>
        <span>{m.vision_analysing()}</span>
      </div>
    {:else if proposal}
      <div class="head-row">
        <ToneBadge
          tone={stateTone(proposal.state)}
          glyph={glyphOf(stateTone(proposal.state))}
          label={stateLabel(proposal.state)}
          value={proposal.state}
          testid="assist-state"
        />
        <span class="quiet small mono">
          {taskLabel(proposal.task)} · {m.vision_round({
            round: proposal.round,
            left: proposal.rounds_left,
          })}
        </span>
      </div>

      {#if assessment}
        <dl class="facts">
          <div>
            <dt>{m.vision_situation()}</dt>
            <dd data-testid="assist-situation">{situationLabel(assessment.situation)}</dd>
          </div>
          <div>
            <dt>{m.vision_confidence()}</dt>
            <dd class="mono" data-testid="assist-confidence">
              {formatNumber(confidencePercent(assessment.confidence))} %
            </dd>
          </div>
          {#if assist.view === 'proposed' && countdown}
            <div>
              <dt>{m.vision_expires()}</dt>
              <dd class="mono" data-testid="assist-expiry">{countdown}</dd>
            </div>
          {/if}
        </dl>
      {/if}

      {#if lowAlone && assessment}
        <p class="low-note" data-testid="assist-low-confidence">
          <span class="low-mark mono">{m.vision_low_confidence_mark()}</span>
          <span class="low-line">{lowConfidenceLine(proposal.action, assessment.confidence)}</span>
          <span class="quiet small">{m.vision_low_confidence_hint()}</span>
        </p>
      {/if}

      {#if choice && assessment}
        <fieldset class="choice" data-testid="assist-choice">
          <legend>{m.vision_choose()}</legend>
          {#each options as option (option.option)}
            <label
              class="option"
              data-option={option.option}
              data-low={option.low_confidence}
              data-testid={`assist-option-${option.option}`}
            >
              <input
                type="radio"
                name="assist-option"
                value={option.option}
                bind:group={selected}
                disabled={assist.busy !== null}
              />
              <span class="option-body">
                <span class="option-head">
                  <span class="option-title">{actionTitle(option.action)}</span>
                  {#if option.low_confidence}
                    <span class="low-mark mono"
                      >{m.vision_low_confidence_mark()} · {formatNumber(
                        confidencePercent(option.confidence ?? 0),
                      )} %</span
                    >
                  {:else if option.option === 'fallback'}
                    <span class="fallback-mark mono">{m.vision_fallback_mark()}</span>
                  {/if}
                </span>
                {#if option.low_confidence}
                  <span class="option-text">
                    {lowConfidenceLine(option.action, option.confidence ?? 0)}
                  </span>
                  <span class="gauge" aria-hidden="true">
                    <span
                      class="gauge-fill"
                      style:width={`${confidencePercent(option.confidence ?? 0)}%`}
                    ></span>
                  </span>
                  <span class="quiet small">{m.vision_low_confidence_hint()}</span>
                {:else}
                  <span class="option-text">{m.vision_fallback_hint_choice()}</span>
                {/if}
                {#if option.steps?.length}
                  <ol class="option-steps" aria-label={m.vision_steps()}>
                    {#each option.steps as step (step.index)}
                      <li><span class="num mono">{step.index + 1}</span>{stepDescription(step)}</li>
                    {/each}
                  </ol>
                {/if}
              </span>
            </label>
          {/each}
        </fieldset>
      {/if}

      {#if proposal.action === 'return_to_dock' && !choice}
        <section class="dock-plan" data-testid="assist-dock" data-fallback={proposal.fallback}>
          <h3>{m.vision_dock_title()}</h3>
          <p>{m.vision_dock_hint()}</p>
          {#if proposal.fallback}
            <p class="fallback-note" data-testid="assist-fallback">
              <span class="fallback-mark mono">{m.vision_fallback_mark()}</span>
              <span
                >{fallbackReason(proposal) === 'none'
                  ? m.vision_fallback_hint()
                  : m.vision_fallback_hint_refused()}</span
              >
            </p>
            {#if proposal.model_refused && assessment}
              <p class="refused-note" data-testid="assist-model-refused">
                {m.vision_model_refused({
                  action: actionTitle(assessment.action),
                  confidence: formatNumber(confidencePercent(assessment.confidence)),
                })}
                <span class="mono">{proposal.model_refused}</span>
              </p>
            {/if}
            {#if assessment?.abort_reasons.length}
              <ul class="reasons" data-testid="assist-fallback-reasons">
                {#each assessment.abort_reasons as reason, i (i)}<li>{reason}</li>{/each}
              </ul>
            {/if}
          {/if}
        </section>
      {/if}

      {#if shownAction === 'resume'}
        <section class="resume-plan" data-testid="assist-resume">
          <h3>{m.vision_resume_title()}</h3>
          <p data-testid="assist-resume-plan">{resumePlan(shownSteps, proposal.job)}</p>
          {#if proposal.job?.areas.length}
            <ol class="job-areas" aria-label={m.vision_job_areas()} data-testid="assist-job-areas">
              {#each proposal.job.areas as area (area.id)}
                <li data-status={area.status}>
                  <span class="area-name">{area.name ?? area.id}</span>
                  <span class="area-status mono">{jobAreaLabel(area.status)}</span>
                </li>
              {/each}
            </ol>
          {/if}
          {#if proposal.job?.mower_at_entry && proposal.job.entry_distance_m != null}
            <p class="quiet small" data-testid="assist-resume-entry">
              {m.vision_resume_at_entry({ distance: formatNumber(proposal.job.entry_distance_m) })}
            </p>
          {/if}
          <p class="quiet small">{m.vision_resume_guard()}</p>
        </section>
      {/if}

      {#if photoProblem}
        <section class="photo-note" data-testid="assist-photo-problem">
          <h3>{m.vision_photo_none()}</h3>
          <p>{photoProblem}</p>
          <p class="quiet small">
            {autoWithoutPhoto ? m.vision_photo_auto_failed() : m.vision_photo_map_only()}
          </p>
        </section>
      {/if}

      {#if triggerNote(proposal)}
        <p class="trigger-note" data-testid="assist-trigger" title={m.vision_trigger_error_hint()}>
          <span class="mark" aria-hidden="true"></span>
          <span class="mono">{triggerNote(proposal)}</span>
          <span class="quiet small">{m.vision_trigger_error_hint()}</span>
        </p>
      {/if}

      {#if proposal.stereo}
        <p class="stereo-note" data-testid="assist-stereo" title={m.vision_stereo_hint()}>
          <span class="stereo-mark mono" aria-hidden="true">{m.vision_stereo_mark()}</span>
          <span class="mono">{m.vision_stereo()}</span>
          <span class="quiet small">{m.vision_stereo_hint()}</span>
        </p>
      {/if}
      {#if imageUrls.some((url) => url)}
        <div class="snapshots" class:pair={imageUrls.length > 1} data-testid="assist-images">
          {#each imageUrls as url, index (index)}
            <figure class="shot">
              {#if url}
                <img
                  class="snapshot"
                  src={url}
                  alt={frameCaption(index)
                    ? m.vision_snapshot_alt_camera({ camera: frameCaption(index) ?? '' })
                    : m.vision_snapshot_alt()}
                />
              {/if}
              {#if frameCaption(index)}
                <figcaption class="quiet small mono">{frameCaption(index)}</figcaption>
              {/if}
            </figure>
          {/each}
        </div>
      {/if}
      {#if proposal.light_used}
        <p class="light-note" data-testid="assist-light-used" title={m.vision_light_used_hint()}>
          <span class="lamp" aria-hidden="true"></span>
          <span class="mono">{m.vision_light_used()}</span>
          <span class="quiet small">{m.vision_light_used_hint()}</span>
        </p>
      {/if}

      {#if proposal.steps.length && !choice}
        <h3>{m.vision_steps()}</h3>
        <ol class="steps" data-testid="assist-steps">
          {#each proposal.steps as step (step.index)}
            <li data-outcome={step.outcome}>
              <span class="num mono">{step.index + 1}</span>
              <span class="what">{stepDescription(step)}</span>
              {#if assist.view !== 'proposed'}
                <ToneBadge
                  tone={step.outcome === 'done'
                    ? 'mowing'
                    : step.outcome === 'aborted'
                      ? 'error'
                      : 'unknown'}
                  glyph={outcomeGlyph(step.outcome)}
                  label={stepOutcomeLabel(step.outcome)}
                  value={step.outcome}
                />
              {/if}
              {#if step.detail}<span class="step-detail">{step.detail}</span>{/if}
            </li>
          {/each}
        </ol>
      {/if}

      {#if assessment?.risk.length}
        <h3>{m.vision_risks()}</h3>
        <ul class="risks" data-testid="assist-risks">
          {#each assessment.risk as flag (flag)}
            <li><Icon name="alert" size={16} /> {riskLabel(flag)}</li>
          {/each}
        </ul>
      {/if}

      {#if assessment?.rationale}
        <h3>{m.vision_rationale()}</h3>
        <p class="rationale" data-testid="assist-rationale">{assessment.rationale}</p>
      {/if}

      {#if assist.view === 'executing'}
        <p class="quiet" role="status">{m.vision_executing_hint()}</p>
      {/if}

      {#if assist.view === 'outcome'}
        <div class="outcome" data-testid="assist-outcome" data-state={proposal.state}>
          {#if proposal.state === 'recovered'}
            <p>
              {proposal.action === 'return_to_dock'
                ? m.vision_outcome_dock_sent()
                : proposal.action === 'resume'
                  ? m.vision_outcome_resumed()
                  : proposal.task === 'find_dock'
                    ? m.vision_outcome_docking()
                    : m.vision_outcome_recovered()}
            </p>
          {:else if proposal.state === 'not_recovered'}
            <p>{m.vision_outcome_not_recovered()}</p>
          {:else if proposal.state === 'rejected_by_ai'}
            <p>{m.vision_outcome_rejected_by_ai()}</p>
            {#if assessment?.abort_reasons.length}
              <ul class="reasons">
                {#each assessment.abort_reasons as reason, i (i)}<li>{reason}</li>{/each}
              </ul>
            {/if}
          {:else if proposal.state === 'expired'}
            <p>{m.vision_outcome_expired()}</p>
          {:else if proposal.state === 'rejected'}
            <p>{m.vision_outcome_rejected()}</p>
          {/if}
          {#if proposal.detail && !autoWithoutPhoto}<p class="detail" data-testid="assist-detail">
              {proposal.detail}
            </p>{/if}
        </div>
      {/if}
    {/if}
  </div>

  {#snippet footer()}
    <div class="actions">
      {#if assist.view === 'ready'}
        <Button
          variant="primary"
          full
          icon="send"
          pending={assist.busy === 'start'}
          disabled={provider?.effective === 'none'}
          onclick={start}
          testid="assist-start"
          >{task === 'find_dock' ? m.vision_start_find_dock() : m.vision_start_recover()}</Button
        >
      {:else if assist.view === 'proposed'}
        <Button
          variant="primary"
          full
          icon="play"
          pending={assist.busy === 'approve'}
          disabled={assist.busy !== null}
          onclick={approve}
          testid="assist-approve">{approveLabel(shownAction)}</Button
        >
        <Button
          full
          icon="close"
          pending={assist.busy === 'reject'}
          disabled={assist.busy !== null}
          onclick={() => assist.reject()}
          testid="assist-reject">{m.vision_reject()}</Button
        >
      {:else if assist.view === 'executing'}
        <Button
          full
          icon="stop"
          pending={assist.busy === 'reject'}
          onclick={() => assist.reject()}
          testid="assist-cancel">{m.vision_cancel()}</Button
        >
      {:else if assist.view === 'outcome'}
        {#if autoWithoutPhoto}
          <Button
            variant="primary"
            full
            icon="send"
            onclick={() => assist.fresh()}
            testid="assist-start-by-hand">{m.vision_start_by_hand()}</Button
          >
        {/if}
        {#if assist.canRetry}
          <Button
            variant="primary"
            full
            icon="send"
            pending={assist.busy === 'retry'}
            onclick={retry}
            testid="assist-retry">{m.vision_retry()}</Button
          >
        {/if}
        <Button full icon="close" onclick={onclose} testid="assist-done">{m.close()}</Button>
      {/if}
    </div>
  {/snippet}
</Sheet>

<style>
  .assist {
    display: grid;
    gap: var(--md-space-3);
    padding-bottom: var(--md-space-2);
  }

  .provider {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--md-space-1) var(--md-space-2);
    margin: 0;
    font-size: var(--md-font-size-14);
  }

  .label,
  h3,
  dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  h3 {
    margin: var(--md-space-2) 0 0;
  }

  .light-note,
  .trigger-note {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: center;
    column-gap: var(--md-space-2);
    margin: 0;
  }

  .light-note .quiet,
  .trigger-note .quiet {
    grid-column: 2;
  }

  /* A mower error started the analysis: a hard-edged error tick, not a soft glow. */
  .mark {
    width: 10px;
    height: 10px;
    background: var(--md-state-error);
    clip-path: polygon(50% 0, 100% 100%, 0 100%);
  }

  .lamp {
    width: 10px;
    height: 10px;
    border-radius: var(--md-radius-1);
    background: var(--md-state-paused);
    box-shadow: 0 0 0 3px var(--md-state-paused-bg);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }

  .link {
    display: inline-flex;
    align-items: center;
    min-height: var(--md-target);
    color: var(--md-color-focus);
    font-weight: var(--md-weight-semibold);
  }

  .link:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .blades {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .small {
    font-size: var(--md-font-size-13);
  }

  .intro,
  .rationale,
  .outcome p {
    margin: 0;
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .progress {
    display: grid;
    gap: var(--md-space-2);
    font-size: var(--md-font-size-14);
  }

  .bar {
    display: block;
    height: 4px;
    background: linear-gradient(
      90deg,
      var(--md-color-surface-sunken) 0%,
      var(--md-state-paused) 50%,
      var(--md-color-surface-sunken) 100%
    );
    background-size: 200% 100%;
    animation: sweep 1.2s linear infinite;
  }

  @keyframes sweep {
    from {
      background-position: 100% 0;
    }

    to {
      background-position: -100% 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .bar {
      animation: none;
    }
  }

  .head-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .facts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
    gap: var(--md-space-2);
    margin: 0;
  }

  .facts div {
    display: grid;
    gap: var(--md-space-1);
    padding: var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line);
  }

  dd {
    margin: 0;
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-medium);
  }

  .snapshots {
    display: grid;
    gap: var(--md-space-2);
  }

  .snapshots.pair {
    grid-template-columns: 1fr 1fr;
  }

  .shot {
    display: grid;
    gap: var(--md-space-1);
    margin: 0;
    min-width: 0;
  }

  /* Return to dock: the mower drives home by itself; a fallback is marked as such. */
  .dock-plan {
    display: grid;
    gap: var(--md-space-1);
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 3px solid var(--md-state-returning);
    background: var(--md-state-returning-bg);
  }

  .dock-plan h3,
  .dock-plan p {
    margin: 0;
  }

  /* Resume: the paused job continues; its areas in driving order, status as text. */
  .resume-plan {
    display: grid;
    gap: var(--md-space-1);
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 3px solid var(--md-state-mowing);
    background: var(--md-state-mowing-bg);
  }

  .resume-plan h3,
  .resume-plan p {
    margin: 0;
  }

  .job-areas {
    display: grid;
    gap: var(--md-space-1);
    margin: 0;
    padding: 0;
    list-style: none;
    counter-reset: area;
  }

  .job-areas li {
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: center;
    column-gap: var(--md-space-2);
    min-height: 32px;
    counter-increment: area;
  }

  .job-areas li::before {
    content: counter(area);
    font-family: var(--md-font-mono);
    color: var(--md-color-ink-muted);
  }

  .job-areas li[data-status='done'] .area-name {
    color: var(--md-color-ink-muted);
  }

  .job-areas li[data-status='current'] .area-name,
  .job-areas li[data-status='next'] .area-name {
    font-weight: var(--md-weight-medium);
  }

  .area-status {
    font-size: var(--md-font-size-12);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--md-color-ink-muted);
  }

  .job-areas li[data-status='current'] .area-status {
    color: var(--md-state-mowing);
  }

  .fallback-note {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: start;
    column-gap: var(--md-space-2);
  }

  .fallback-mark {
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) dashed var(--md-color-line);
    border-radius: var(--md-radius-1);
    color: var(--md-color-ink-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  /* Two options: the safe fallback in a solid frame, the model's uncertain recommendation in a
     dashed amber one with its confidence as a gauge; the picked one gets the focus colour. */
  .choice {
    display: grid;
    gap: var(--md-space-2);
    min-width: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }

  .choice legend {
    margin-bottom: var(--md-space-2);
    padding: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .option {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--md-space-2);
    align-items: start;
    padding: var(--md-space-2) var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-inline-start: 3px solid var(--md-state-returning);
    background: var(--md-color-surface);
    cursor: pointer;
  }

  .option[data-low='true'] {
    border-style: dashed;
    border-color: var(--md-state-paused);
    border-inline-start: 3px solid var(--md-state-paused);
    background: var(--md-state-paused-bg);
  }

  .option:has(input:checked) {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -1px;
  }

  .option input {
    width: 20px;
    height: 20px;
    margin: 2px 0 0;
    accent-color: var(--md-color-focus);
  }

  .option-body {
    display: grid;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .option-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-1) var(--md-space-2);
  }

  .option-title {
    font-weight: var(--md-weight-semibold);
  }

  .option-text,
  .low-line {
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .low-mark {
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) dashed var(--md-state-paused);
    border-radius: var(--md-radius-1);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    letter-spacing: 0.04em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .gauge {
    display: block;
    height: 4px;
    background: var(--md-color-surface-sunken);
  }

  .gauge-fill {
    display: block;
    height: 100%;
    background: var(--md-state-paused);
  }

  .option-steps {
    display: grid;
    gap: var(--md-space-1);
    margin: var(--md-space-1) 0 0;
    padding: 0;
    font-size: var(--md-font-size-13);
    list-style: none;
  }

  .option-steps li {
    display: grid;
    grid-template-columns: 28px minmax(0, 1fr);
    align-items: center;
  }

  .low-note {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--md-space-1) var(--md-space-2);
    align-items: center;
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border: var(--md-hairline) dashed var(--md-state-paused);
    background: var(--md-state-paused-bg);
  }

  .low-note .quiet {
    grid-column: 1 / -1;
  }

  .refused-note {
    font-size: var(--md-font-size-13);
  }

  /* No camera photo: a dashed, signal-less frame; the cause in words, never a soft glow. */
  .photo-note {
    display: grid;
    gap: var(--md-space-1);
    padding: var(--md-space-2) var(--md-space-3);
    border: var(--md-hairline) dashed var(--md-color-line-strong);
    border-inline-start: 3px solid var(--md-state-error);
    background: var(--md-color-surface-sunken);
  }

  .photo-note h3,
  .photo-note p {
    margin: 0;
  }

  .photo-note p {
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .stereo-note {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: center;
    column-gap: var(--md-space-2);
    margin: 0;
  }

  .stereo-note .quiet {
    grid-column: 2;
  }

  .stereo-mark {
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-1);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    letter-spacing: 0.08em;
  }

  .snapshot {
    display: block;
    width: 100%;
    max-height: 240px;
    object-fit: contain;
    border: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface-sunken);
  }

  .steps,
  .risks,
  .reasons {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .steps li {
    display: grid;
    grid-template-columns: 28px minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--md-space-1) var(--md-space-2);
    min-height: var(--md-target);
    padding: var(--md-space-1) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    font-size: var(--md-font-size-14);
  }

  .num {
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    font-size: var(--md-font-size-12);
  }

  .steps li[data-outcome='done'] .num {
    border-color: var(--md-state-mowing);
  }

  .steps li[data-outcome='aborted'] .num {
    border-color: var(--md-state-error);
  }

  .step-detail {
    grid-column: 2 / -1;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .risks li {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    padding: var(--md-space-1) 0;
    color: var(--md-state-error);
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
  }

  .reasons li {
    padding: var(--md-space-1) 0;
    font-size: var(--md-font-size-14);
  }

  .outcome {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
  }

  .outcome[data-state='recovered'] {
    border-color: var(--md-state-mowing);
  }

  .outcome[data-state='not_recovered'],
  .outcome[data-state='aborted'],
  .outcome[data-state='failed'],
  .outcome[data-state='rejected_by_ai'] {
    border-color: var(--md-state-error);
  }

  .detail {
    color: var(--md-color-ink-muted);
  }

  .actions {
    display: grid;
    gap: var(--md-space-2);
  }
</style>
