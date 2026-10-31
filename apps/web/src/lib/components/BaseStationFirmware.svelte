<!--
  The firmware of an RTK base station (ADR 0047): installed and offered version, the state with
  the last check, a progress bar while it installs, "Nach Updates suchen" (through a linked
  mower) and "Update installieren". The install asks first in a sheet that says what happens
  (the base restarts, every mower that uses it loses its corrections meanwhile) and lets the user
  pick the mower that relays the request; a mower that cannot relay now is named with the reason.
  In the public demo both buttons are locked (`DemoLock`); the versions and the state stay.
-->
<script lang="ts">
  import { Button, Select, Sheet } from '@mowdeck/ui';

  import {
    updateProgress,
    type BaseUpdateBlocker,
    type FirmwareInfo,
  } from '#lib/fleet/base-stations.js';
  import DemoLock from '#lib/demo/DemoLock.svelte';
  import { baseUpdateBlockerText, firmwareStateLabel } from '#lib/fleet/base-station-labels.js';
  import { formatAge, formatNumber, outcomeLabel } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import type { CommandOutcome } from '#lib/stores/commands.svelte.js';

  interface Relay {
    id: string;
    name: string;
    blocker: BaseUpdateBlocker | null;
  }
  interface Props {
    info: FirmwareInfo | null;
    /** `BaseStationState.firmware`, shown when `info` has no installed version. */
    reported: string | null;
    /** The linked mowers that could relay, in name order. */
    relays: Relay[];
    /** Why nobody may update at all (rights, plan); null when it depends on the mower. */
    blocker: BaseUpdateBlocker | null;
    checking: boolean;
    /** The update request's command, once sent. */
    request: { status: CommandOutcome; reason: string | null } | null;
    now: number;
    oncheck: () => void;
    oninstall: (version: string, mowerId: string) => void;
  }
  let { info, reported, relays, blocker, checking, request, now, oncheck, oninstall }: Props =
    $props();

  let confirming = $state(false);
  let relayId = $state('');

  const installed = $derived(info?.installed ?? reported ?? null);
  const progress = $derived(updateProgress(info));
  const offered = $derived(info?.update_state === 'available' && !!info.available);
  /** The first mower that may relay now, else the first one. */
  const preferred = $derived(relays.find((r) => r.blocker === null) ?? relays[0] ?? null);
  const chosen = $derived(relays.find((r) => r.id === relayId) ?? preferred);
  const shownBlocker = $derived<BaseUpdateBlocker | null>(
    blocker ?? (relays.length === 0 ? 'no_mower' : (preferred?.blocker ?? null)),
  );
  const chosenBlocker = $derived<BaseUpdateBlocker | null>(
    blocker ?? (chosen ? chosen.blocker : 'no_mower'),
  );
  const stateTone = $derived(
    info?.update_state === 'idle'
      ? 'mowing'
      : info?.update_state === 'available'
        ? 'paused'
        : info?.update_state === 'installing'
          ? 'charging'
          : info?.update_state === 'failed'
            ? 'error'
            : 'unknown',
  );

  function open(): void {
    relayId = preferred?.id ?? '';
    confirming = true;
  }

  function install(): void {
    const version = info?.available;
    if (!version || !chosen || chosenBlocker) return;
    confirming = false;
    oninstall(version, chosen.id);
  }
</script>

<div class="firmware" data-testid="bs-firmware" data-state={info?.update_state ?? 'unknown'}>
  <dl class="versions">
    <div>
      <dt>{m.bs_fw_installed()}</dt>
      <dd class="mono" data-testid="bs-fw-installed">{installed ?? m.no_data()}</dd>
    </div>
    <div>
      <dt>{m.bs_fw_available()}</dt>
      <dd class="mono" data-testid="bs-fw-available">
        {#if info?.available}{info.available}{:else}<span class="dim"
            >{info ? m.bs_fw_none() : m.no_data()}</span
          >{/if}
      </dd>
    </div>
  </dl>
  <p class="state" data-tone={stateTone} data-testid="bs-fw-state">
    <span class="lamp" aria-hidden="true"></span>
    <span>{firmwareStateLabel(info?.update_state)}</span>
    {#if info?.checked_at}
      <span class="dim">· {m.bs_fw_checked({ age: formatAge(info.checked_at, now) })}</span>
    {/if}
  </p>
  {#if progress !== null}
    <div class="progress" data-testid="bs-fw-progress">
      <div
        class="track"
        role="progressbar"
        aria-label={m.bs_fw_progress()}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
      >
        <span class="fill" style:width="{progress}%"></span>
      </div>
      <span class="mono">{formatNumber(progress)} %</span>
    </div>
  {/if}
  {#if request}
    <p class="request mono" data-testid="bs-fw-request" data-status={request.status}>
      {m.bs_fw_result({ result: outcomeLabel(request.status) })}{request.reason
        ? ` · ${request.reason}`
        : ''}
    </p>
  {/if}
  <div class="actions">
    <!-- Fleet refuses the firmware check and update of RTK base stations in the demo. -->
    <DemoLock variant="inline" testid="demo-lock-bs-fw">
      <Button
        icon="signal"
        pending={checking}
        disabled={relays.length === 0}
        testid="bs-fw-check"
        onclick={oncheck}>{m.bs_fw_check()}</Button
      >
      {#if offered}
        <Button
          variant="primary"
          icon="download"
          disabled={shownBlocker !== null}
          testid="bs-fw-install"
          onclick={open}>{m.bs_fw_install()}</Button
        >
      {/if}
    </DemoLock>
  </div>
  {#if (offered || relays.length === 0) && shownBlocker}
    <p class="hint" data-testid="bs-fw-blocker" data-reason={shownBlocker}>
      {baseUpdateBlockerText(shownBlocker)}
    </p>
  {/if}
</div>

<Sheet
  open={confirming}
  title={m.bs_fw_confirm_title()}
  closeLabel={m.close()}
  onclose={() => (confirming = false)}
  testid="bs-fw-confirm"
>
  <div class="confirm">
    <p class="body">{m.bs_fw_confirm_body({ version: info?.available ?? '' })}</p>
    {#if relays.length > 1}
      <Select
        id="bs-fw-relay"
        label={m.bs_fw_relay()}
        options={relays.map((r) => ({ value: r.id, label: r.name }))}
        bind:value={relayId}
      />
    {:else if chosen}
      <p class="hint">{m.bs_fw_relay()}: {chosen.name}</p>
    {/if}
    {#if chosenBlocker}
      <p class="warn" data-testid="bs-fw-confirm-blocker">{baseUpdateBlockerText(chosenBlocker)}</p>
    {/if}
  </div>
  {#snippet footer()}
    <div class="footer">
      <Button variant="ghost" onclick={() => (confirming = false)}>{m.cancel()}</Button>
      <Button
        variant="danger"
        icon="download"
        disabled={chosenBlocker !== null}
        testid="bs-fw-go"
        onclick={install}>{m.bs_fw_go()}</Button
      >
    </div>
  {/snippet}
</Sheet>

<style>
  .firmware,
  .confirm {
    display: grid;
    gap: var(--md-space-3);
  }

  .versions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--md-space-2) var(--md-space-4);
    margin: 0;
  }

  dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  dd {
    margin: 0;
    font-size: var(--md-font-size-20);
    overflow-wrap: anywhere;
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .dim {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .state {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
    margin: 0;
    font-size: var(--md-font-size-14);
  }

  .lamp {
    width: 8px;
    height: 8px;
    border-radius: var(--md-radius-round);
    background: var(--md-state-unknown);
  }

  .state[data-tone='mowing'] .lamp {
    background: var(--md-state-mowing);
  }

  .state[data-tone='paused'] .lamp {
    background: var(--md-state-paused);
  }

  .state[data-tone='charging'] .lamp {
    background: var(--md-state-charging);
  }

  .state[data-tone='error'] .lamp {
    background: var(--md-state-error);
  }

  .progress {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--md-space-3);
  }

  .track {
    height: 8px;
    background: var(--md-color-surface-sunken);
  }

  .fill {
    display: block;
    height: 100%;
    background: var(--md-state-charging);
  }

  .request {
    margin: 0;
    font-size: var(--md-font-size-12);
  }

  .actions,
  .footer {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .footer {
    justify-content: flex-end;
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .body {
    margin: 0;
    line-height: var(--md-line-24);
  }

  .warn {
    margin: 0;
    padding-left: var(--md-space-3);
    border-left: 3px solid var(--md-state-paused);
    font-size: var(--md-font-size-14);
  }
</style>
