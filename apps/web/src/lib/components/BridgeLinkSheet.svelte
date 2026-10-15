<!--
  Adding a Bluetooth bridge to a mower (ADR 0038, design 3), a non-modal sheet in three steps:
  the bridge (host, key, the port under "erweitert"), the mower the bridge saw (the expected
  name preselected, every other one marked "anderer Mäher", the blade switch for manual driving)
  and the saved link. The flow (`$lib/mower/bridge-link.svelte`) holds every state; this file is
  the markup only. The key is typed into a password field and never shown again.
-->
<script lang="ts">
  import { Button, Field, Icon, ProblemNotice, Sheet, Switch } from '@mowdeck/ui';

  import { isExpected, issueText, rssiText } from '$lib/mower/bridge-link';
  import type { BridgeLinkFlow, BridgeStep } from '$lib/mower/bridge-link.svelte';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    flow: BridgeLinkFlow;
  }
  let { flow }: Props = $props();

  const STEPS: { key: 'form' | 'pick' | 'saved'; label: () => string }[] = [
    { key: 'form', label: () => m.bridge_step_form() },
    { key: 'pick', label: () => m.bridge_step_pick() },
    { key: 'saved', label: () => m.bridge_step_done() },
  ];
  const ORDER: Record<BridgeStep, number> = { form: 0, scanning: 0, pick: 1, saving: 1, saved: 2 };

  const stage = $derived(ORDER[flow.step]);
  const busy = $derived(flow.step === 'scanning' || flow.step === 'saving');
  const scanningText = $derived(
    flow.expectedName ? m.bridge_scanning({ name: flow.expectedName }) : m.bridge_scanning_any(),
  );

  function fieldError(field: 'host' | 'port' | 'key'): string | null {
    const issue = flow.issues[field];
    return issue ? issueText(field, issue) : null;
  }

  function submit(event: SubmitEvent): void {
    event.preventDefault();
    void flow.scan();
  }
</script>

<Sheet
  open={flow.open}
  title={m.bridge_title()}
  closeLabel={m.close()}
  onclose={() => flow.close()}
  testid="bridge-sheet"
>
  <ol class="steps" data-testid="bridge-steps" data-step={flow.step}>
    {#each STEPS as step, index (step.key)}
      <li
        data-state={index < stage ? 'done' : index === stage ? 'current' : 'todo'}
        aria-current={index === stage ? 'step' : undefined}
      >
        <span class="dot" aria-hidden="true">
          {#if index < stage}<Icon name="check" size={12} />{:else}{index + 1}{/if}
        </span>
        <span class="word">{step.label()}</span>
      </li>
    {/each}
  </ol>

  {#if stage === 0}
    <form class="form" onsubmit={submit} novalidate>
      <Field
        id="bridge-host"
        label={m.conn_ble_host()}
        hint={m.bridge_host_help()}
        error={fieldError('host')}
        mono
        required
        disabled={busy}
        inputmode="url"
        autocomplete="off"
        testid="bridge-host"
        bind:value={flow.host}
      />
      <Field
        id="bridge-key"
        type="password"
        label={m.bridge_key_label()}
        hint={flow.keyStored
          ? `${m.conn_secret_set()} · ${m.bridge_key_help()}`
          : m.bridge_key_help()}
        error={fieldError('key')}
        mono
        required={!flow.keyStored}
        disabled={busy}
        autocomplete="off"
        testid="bridge-key"
        bind:value={flow.key}
      />
      <button
        type="button"
        class="disclosure"
        aria-expanded={flow.advanced}
        data-testid="bridge-advanced"
        onclick={() => (flow.advanced = !flow.advanced)}
      >
        <Icon name="chevron" size={14} />
        {m.bridge_advanced()}
      </button>
      {#if flow.advanced}
        <Field
          id="bridge-port"
          type="number"
          label={m.conn_ble_port()}
          hint={m.bridge_port_help()}
          error={fieldError('port')}
          mono
          disabled={busy}
          inputmode="numeric"
          testid="bridge-port"
          bind:value={flow.port}
        />
      {/if}
      {#if flow.error && flow.step === 'form'}
        <div data-testid="bridge-scan-error" data-code={flow.errorCode}>
          <ProblemNotice title={flow.error} />
        </div>
      {/if}
      {#if flow.step === 'scanning'}
        <p class="scanning" role="status" data-testid="bridge-scanning">
          <span class="pulse" aria-hidden="true"></span>
          {scanningText}
        </p>
      {/if}
      <!-- Enter in a field searches; the visible button sits in the footer. -->
      <button type="submit" class="hidden-submit" tabindex="-1" aria-hidden="true"></button>
    </form>
  {:else if stage === 1}
    {#if flow.proxy}
      <p class="proxy mono" data-testid="bridge-proxy">
        {m.bridge_proxy({
          name: flow.proxy.proxy_name,
          version: flow.proxy.esphome_version ?? '?',
        })}
      </p>
    {/if}
    {#if flow.error}
      <div data-testid="bridge-scan-error" data-code={flow.errorCode}>
        <ProblemNotice tone={flow.errorCode === 'timeout' ? 'info' : 'error'} title={flow.error} />
      </div>
    {/if}
    <p class="hint">{m.bridge_pick_hint()}</p>
    <div
      class="devices"
      role="radiogroup"
      aria-label={m.bridge_pick_title()}
      data-testid="bridge-device-list"
    >
      {#each flow.sorted as device (device.address)}
        {@const expected = isExpected(device, flow.expectedName)}
        <button
          type="button"
          role="radio"
          class="device"
          aria-checked={flow.chosen === device.address}
          disabled={busy}
          data-testid="bridge-device-{device.address}"
          data-expected={expected}
          onclick={() => flow.choose(device.address)}
        >
          <span class="mark" aria-hidden="true"></span>
          <span class="text">
            <span class="name">{device.name}</span>
            <span class="meta mono">{device.address} · {rssiText(device.rssi)}</span>
          </span>
          <span class="tag" data-kind={expected ? 'expected' : 'other'}>
            {expected ? m.bridge_expected_mower() : m.bridge_other_mower()}
          </span>
        </button>
      {/each}
    </div>
    <Switch
      id="bridge-blades"
      label={m.bridge_blades()}
      hint={m.bridge_blades_hint()}
      states={{ on: m.switch_on(), off: m.switch_off() }}
      disabled={busy}
      testid="bridge-blades"
      bind:checked={flow.allowBlades}
    />
  {:else}
    <div class="saved" data-testid="bridge-saved">
      <span class="check" aria-hidden="true"><Icon name="check" size={22} /></span>
      <div>
        <p class="title">{m.bridge_saved()}</p>
        <p class="hint">{m.bridge_saved_body()}</p>
        {#if flow.saved}
          <p class="meta mono">
            {m.bridge_mower({ name: flow.saved.device_name, address: flow.saved.ble_address })}
          </p>
          <p class="meta mono">{m.bridge_at({ host: `${flow.saved.host}:${flow.saved.port}` })}</p>
        {/if}
      </div>
    </div>
  {/if}

  {#snippet footer()}
    <div class="footer">
      {#if stage === 0}
        <Button
          variant="primary"
          icon="signal"
          pending={flow.step === 'scanning'}
          testid="bridge-scan"
          onclick={() => void flow.scan()}>{m.bridge_scan()}</Button
        >
      {:else if stage === 1}
        <Button
          variant="ghost"
          icon="back"
          disabled={busy}
          testid="bridge-back"
          onclick={() => flow.back()}>{m.back()}</Button
        >
        <Button
          variant="ghost"
          disabled={busy}
          testid="bridge-rescan"
          onclick={() => void flow.scan()}>{m.bridge_rescan()}</Button
        >
        <Button
          variant="primary"
          icon="check"
          pending={flow.step === 'saving'}
          disabled={flow.chosenDevice === null}
          testid="bridge-save"
          onclick={() => void flow.save()}>{m.save()}</Button
        >
      {:else}
        <Button variant="primary" testid="bridge-done" onclick={() => flow.close()}
          >{m.bridge_done()}</Button
        >
      {/if}
    </div>
  {/snippet}
</Sheet>

<style>
  .steps {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--md-space-2);
    margin: 0 0 var(--md-space-4);
    padding: 0;
    list-style: none;
  }

  .steps li {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    padding-bottom: var(--md-space-2);
    border-bottom: var(--md-border-control) solid var(--md-color-line);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .steps li[data-state='current'] {
    border-bottom-color: var(--md-color-ink);
    color: var(--md-color-ink);
  }

  .steps li[data-state='done'] {
    border-bottom-color: var(--md-state-mowing);
    color: var(--md-state-mowing);
  }

  .dot {
    display: grid;
    place-items: center;
    width: 20px;
    height: 20px;
    border: var(--md-hairline) solid currentcolor;
    border-radius: var(--md-radius-1);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
  }

  .form {
    display: grid;
    gap: var(--md-space-4);
  }

  .disclosure {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    justify-self: start;
    min-height: var(--md-target);
    padding: 0;
    border: 0;
    background: none;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
    cursor: pointer;
  }

  .disclosure :global(svg) {
    transition: transform var(--md-motion-fast) var(--md-ease);
  }

  .disclosure[aria-expanded='true'] :global(svg) {
    transform: rotate(90deg);
  }

  .hidden-submit {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    border: 0;
    padding: 0;
    clip-path: inset(50%);
  }

  .scanning {
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
    margin: 0;
    padding: var(--md-space-3);
    background: var(--md-color-surface-sunken);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
  }

  .pulse {
    flex: none;
    width: 12px;
    height: 12px;
    border: var(--md-border-control) solid var(--md-state-returning);
    background: var(--md-state-returning-bg);
    animation: pulse 1.2s var(--md-ease) infinite;
  }

  @keyframes pulse {
    0%,
    100% {
      box-shadow: 0 0 0 0 var(--md-state-returning-bg);
    }

    50% {
      box-shadow: 0 0 0 6px var(--md-state-returning-bg);
    }
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .proxy {
    margin: 0 0 var(--md-space-2);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    overflow-wrap: anywhere;
  }

  .hint {
    margin: 0 0 var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .devices {
    display: grid;
    gap: var(--md-space-2);
    margin-bottom: var(--md-space-4);
  }

  .device {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--md-space-3);
    min-height: 56px;
    padding: var(--md-space-2) var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    text-align: start;
    cursor: pointer;
  }

  .device[aria-checked='true'] {
    border-color: var(--md-color-ink);
    background: var(--md-color-surface-sunken);
  }

  .device:disabled {
    cursor: wait;
  }

  .mark {
    width: 14px;
    height: 14px;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
  }

  .device[aria-checked='true'] .mark {
    border-color: var(--md-color-ink);
    background: radial-gradient(circle, var(--md-color-ink) 45%, transparent 50%);
  }

  .text {
    display: grid;
    min-width: 0;
  }

  .name {
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  .meta {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .tag {
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid currentcolor;
    border-radius: var(--md-radius-1);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    white-space: nowrap;
  }

  .tag[data-kind='expected'] {
    color: var(--md-state-mowing);
    background: var(--md-state-mowing-bg);
  }

  .saved {
    display: flex;
    gap: var(--md-space-3);
    padding: var(--md-space-3) 0;
  }

  .check {
    display: grid;
    flex: none;
    place-items: center;
    width: 40px;
    height: 40px;
    border: var(--md-border-control) solid var(--md-state-mowing);
    border-radius: var(--md-radius-2);
    background: var(--md-state-mowing-bg);
    color: var(--md-state-mowing);
  }

  .title {
    margin: 0 0 var(--md-space-1);
    font-weight: var(--md-weight-semibold);
  }

  .footer {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: var(--md-space-2);
  }
</style>
