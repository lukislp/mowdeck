<!--
  The "Firmware" panel of the mower settings (ADR 0038): the installed version, the version the
  manufacturer offers with its release notes (folded), the state line, a progress bar while an
  update installs, "Nach Updates suchen" and "Update installieren". The install button is
  disabled with the unmet precondition written under it (`updateBlocker`) and asks once more in
  a sheet before the command goes out: the mower is unusable for 10 to 20 minutes.
-->
<script lang="ts">
  import { Button, Icon, Sheet } from '@mowdeck/ui';

  import {
    installProgress,
    updateBlocker,
    updateOffered,
    type FirmwareInfo,
    type UpdateBlocker,
  } from '#lib/fleet/firmware.js';
  import { formatAge, formatNumber } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    info: FirmwareInfo | null;
    /** `MowerState.firmware`, shown when `info` has no installed version. */
    reported: string | null;
    /** `capabilities.firmware_update`: the connection can check and install. */
    capable: boolean;
    activity: string | null;
    batteryPercent: number | null;
    stale: boolean;
    /** The user may control the mower. */
    allowed: boolean;
    checking: boolean;
    now: number;
    oncheck: () => void;
    oninstall: (version: string) => void;
  }
  let {
    info,
    reported,
    capable,
    activity,
    batteryPercent,
    stale,
    allowed,
    checking,
    now,
    oncheck,
    oninstall,
  }: Props = $props();

  let notesOpen = $state(false);
  let confirming = $state(false);

  const installed = $derived(info?.installed ?? reported ?? null);
  const updateState = $derived(info?.update_state ?? 'unknown');
  const progress = $derived(installProgress(info));
  const offered = $derived(updateOffered(info));
  const blocker = $derived(updateBlocker({ info, activity, batteryPercent, stale, allowed }));
  const stateText = $derived.by(() => {
    switch (updateState) {
      case 'idle':
        return m.firmware_state_idle();
      case 'available':
        return m.firmware_state_available();
      case 'installing':
        return m.firmware_state_installing();
      case 'failed':
        return m.firmware_state_failed();
      default:
        return m.firmware_state_unknown();
    }
  });
  const stateTone = $derived(
    updateState === 'idle'
      ? 'mowing'
      : updateState === 'available'
        ? 'paused'
        : updateState === 'installing'
          ? 'charging'
          : updateState === 'failed'
            ? 'error'
            : 'unknown',
  );

  function blockerText(reason: UpdateBlocker): string {
    switch (reason) {
      case 'no_permission':
        return m.firmware_block_no_permission();
      case 'no_update':
        return m.firmware_block_no_update();
      case 'installing':
        return m.firmware_block_installing();
      case 'offline':
        return m.firmware_block_offline();
      case 'job':
        return m.firmware_block_job();
      case 'not_docked':
        return m.firmware_block_not_docked();
      case 'battery':
        return m.firmware_block_battery({ percent: formatNumber(batteryPercent ?? 0) });
    }
  }

  function install(): void {
    const version = info?.available;
    if (!version || blocker) return;
    confirming = false;
    oninstall(version);
  }
</script>

<div class="firmware" data-testid="firmware-panel" data-state={updateState}>
  <dl class="versions">
    <div>
      <dt>{m.firmware_installed()}</dt>
      <dd class="mono" data-testid="firmware-installed">{installed ?? m.no_data()}</dd>
    </div>
    <div>
      <dt>{m.firmware_available()}</dt>
      <dd class="mono" data-testid="firmware-available">
        {#if info?.available}
          {info.available}
        {:else}
          <span class="dim">{capable ? m.firmware_none_offered() : m.no_data()}</span>
        {/if}
      </dd>
    </div>
  </dl>

  {#if info?.release_notes}
    <div class="notes">
      <button
        type="button"
        class="disclosure"
        aria-expanded={notesOpen}
        data-testid="firmware-notes-toggle"
        onclick={() => (notesOpen = !notesOpen)}
      >
        <Icon name="chevron" size={14} />
        {m.firmware_release_notes()}
      </button>
      {#if notesOpen}
        <pre class="notes-body" data-testid="firmware-notes">{info.release_notes}</pre>
      {/if}
    </div>
  {/if}

  {#if capable}
    <p class="state" data-tone={stateTone} data-testid="firmware-state">
      <span class="lamp" aria-hidden="true"></span>
      <span class="word">{stateText}</span>
      <span class="dim">
        · {info?.checked_at
          ? m.firmware_checked({ age: formatAge(info.checked_at, now) })
          : m.firmware_never_checked()}
      </span>
    </p>
    {#if updateState === 'failed' && info?.failure_reason}
      <p class="failed" data-testid="firmware-failed">
        {m.firmware_failed_reason({ reason: info.failure_reason })}
      </p>
    {/if}
    {#if progress !== null}
      <div class="progress" data-testid="firmware-progress">
        <span class="label">{m.firmware_progress()}</span>
        <div
          class="track"
          role="progressbar"
          aria-label={m.firmware_progress()}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress}
        >
          <span class="fill" style:width="{progress}%"></span>
        </div>
        <span class="mono value">{formatNumber(progress)} %</span>
      </div>
    {/if}
    <div class="actions">
      <Button icon="signal" pending={checking} testid="firmware-check" onclick={oncheck}
        >{m.firmware_check()}</Button
      >
      {#if offered}
        <Button
          variant="primary"
          icon="download"
          disabled={blocker !== null}
          testid="firmware-install"
          onclick={() => (confirming = true)}>{m.firmware_install()}</Button
        >
      {/if}
    </div>
    {#if offered && blocker}
      <p class="hint" data-testid="firmware-blocker" data-reason={blocker}>
        {blockerText(blocker)}
      </p>
    {/if}
  {:else}
    <p class="hint" data-testid="firmware-unsupported">{m.firmware_unsupported()}</p>
  {/if}
</div>

<Sheet
  open={confirming}
  title={m.firmware_confirm_title()}
  closeLabel={m.close()}
  onclose={() => (confirming = false)}
  testid="firmware-confirm"
>
  <p class="body">{m.firmware_confirm_body({ version: info?.available ?? '' })}</p>
  <p class="hint">{m.firmware_confirm_requirements()}</p>
  {#snippet footer()}
    <div class="footer">
      <Button variant="ghost" onclick={() => (confirming = false)}>{m.cancel()}</Button>
      <Button
        variant="primary"
        icon="download"
        disabled={blocker !== null}
        testid="firmware-confirm-go"
        onclick={install}>{m.firmware_confirm_go()}</Button
      >
    </div>
  {/snippet}
</Sheet>

<style>
  .firmware {
    display: grid;
    gap: var(--md-space-3);
  }

  .versions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--md-space-2) var(--md-space-4);
    margin: 0;
  }

  .versions dt,
  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .versions dd {
    margin: 0;
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-28);
    overflow-wrap: anywhere;
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .dim {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .disclosure {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: var(--md-target);
    padding: 0;
    border: 0;
    background: none;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
    cursor: pointer;
  }

  .disclosure :global(svg) {
    transition: transform var(--md-motion-fast) var(--md-ease);
  }

  .disclosure[aria-expanded='true'] :global(svg) {
    transform: rotate(90deg);
  }

  .notes-body {
    max-height: 240px;
    margin: 0;
    padding: var(--md-space-3);
    overflow: auto;
    background: var(--md-color-surface-sunken);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .state {
    --tone: var(--md-state-unknown);

    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
    margin: 0;
    font-size: var(--md-font-size-14);
  }

  .state[data-tone='mowing'] {
    --tone: var(--md-state-mowing);
  }

  .state[data-tone='paused'] {
    --tone: var(--md-state-paused);
  }

  .state[data-tone='charging'] {
    --tone: var(--md-state-charging);
  }

  .state[data-tone='error'] {
    --tone: var(--md-state-error);
  }

  .lamp {
    width: 10px;
    height: 10px;
    border: var(--md-border-control) solid var(--tone);
    background: var(--tone);
  }

  .state[data-tone='unknown'] .lamp {
    border-style: dashed;
    background: transparent;
  }

  .state[data-tone='charging'] .lamp {
    animation: blink 1.2s steps(2, end) infinite;
  }

  @keyframes blink {
    50% {
      background: transparent;
    }
  }

  .word {
    color: var(--tone);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .failed {
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 4px solid var(--md-state-error);
    background: var(--md-state-error-bg);
    font-size: var(--md-font-size-14);
  }

  .progress {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--md-space-3);
  }

  .track {
    height: 10px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface-sunken);
  }

  .fill {
    display: block;
    height: 100%;
    background: var(--md-state-charging);
    transition: width var(--md-motion-base) var(--md-ease);
  }

  .value {
    font-size: var(--md-font-size-14);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .body {
    margin: 0 0 var(--md-space-3);
    font-size: var(--md-font-size-16);
    line-height: var(--md-line-24);
  }

  .footer {
    display: flex;
    justify-content: flex-end;
    gap: var(--md-space-2);
  }

  @media (prefers-reduced-motion: reduce) {
    .state[data-tone='charging'] .lamp {
      animation: none;
    }
  }
</style>
