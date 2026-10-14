<!--
  The devices of a linked account as its last device list showed them: name, model, IoT id,
  online state, and a checkbox for every mower that can be added. Mowers already added, mowers
  of another MowDeck account and non-mowers (RTK base stations) are greyed out with the reason.
  "Read account again" and the time of the last read sit in the head. No requests here.
-->
<script lang="ts">
  import { Button } from '@mowdeck/ui';

  import ToneBadge from '$lib/components/ToneBadge.svelte';
  import { formatDateTime } from '$lib/i18n';
  import {
    deviceState,
    idPart,
    onlineState,
    sortDevices,
    toggleSelection,
    type DeviceState,
    type LinkedDevice,
    type OnlineState,
  } from '$lib/linked/accounts';
  import { m } from '$lib/paraglide/messages';
  import type { Glyph, Tone } from '$lib/schedules/labels';

  interface Props {
    devices: LinkedDevice[];
    selected?: string[];
    lastSyncedAt: string | null;
    syncing?: boolean;
    onsync?: () => void;
    /** Messages of devices the last attempt could not add, by device name. */
    failures?: Record<string, string>;
    disabled?: boolean;
  }
  let {
    devices,
    selected = $bindable([]),
    lastSyncedAt,
    syncing = false,
    onsync,
    failures = {},
    disabled = false,
  }: Props = $props();

  const rows = $derived(sortDevices(devices));

  const ONLINE: Record<OnlineState, { tone: Tone; glyph: Glyph; label: () => string }> = {
    online: { tone: 'mowing', glyph: 'check', label: m.linked_device_online },
    offline: { tone: 'offline', glyph: 'dash', label: m.linked_device_offline },
    unknown: { tone: 'unknown', glyph: 'question', label: m.linked_device_unknown },
  };

  function why(state: DeviceState): string | null {
    switch (state) {
      case 'registered':
        return m.linked_device_registered();
      case 'elsewhere':
        return m.linked_device_elsewhere();
      case 'not_mower':
        return m.linked_device_not_mower();
      case 'addable':
        return null;
    }
  }

  function toggle(name: string, event: Event): void {
    selected = toggleSelection(selected, name, (event.currentTarget as HTMLInputElement).checked);
  }
</script>

<div class="devices" data-testid="linked-device-list">
  <div class="head">
    <span class="sync">
      <span class="label">{m.linked_last_sync()}</span>
      <span class="mono" data-testid="linked-last-sync"
        >{lastSyncedAt ? formatDateTime(lastSyncedAt) : m.linked_never_synced()}</span
      >
    </span>
    {#if onsync}
      <Button icon="signal" pending={syncing} {disabled} onclick={onsync} testid="linked-resync"
        >{syncing ? m.linked_syncing() : m.linked_resync()}</Button
      >
    {/if}
  </div>

  {#if rows.length === 0}
    <p class="empty" data-testid="linked-devices-empty">{m.linked_devices_empty()}</p>
  {:else}
    <fieldset>
      <legend>
        <span>{m.linked_devices_label()}</span>
        <span class="count mono" data-testid="linked-selected-count"
          >{m.linked_selected_count({ count: selected.length })}</span
        >
      </legend>
      <ul>
        {#each rows as device (device.device_name)}
          {@const state = deviceState(device)}
          {@const online = ONLINE[onlineState(device)]}
          {@const key = idPart(device.device_name)}
          {@const reason = why(state)}
          {@const failure = failures[device.device_name]}
          <li
            class="row"
            class:off={state !== 'addable'}
            class:failed={!!failure}
            data-testid="linked-device-{key}"
            data-state={state}
          >
            {#if state === 'addable'}
              <input
                id="linked-check-{key}"
                type="checkbox"
                checked={selected.includes(device.device_name)}
                {disabled}
                aria-describedby="linked-meta-{key}{failure ? ` linked-fail-${key}` : ''}"
                data-testid="linked-device-check-{key}"
                onchange={(event) => toggle(device.device_name, event)}
              />
            {:else}
              <span class="slot" aria-hidden="true"></span>
            {/if}
            <span class="body">
              {#if state === 'addable'}
                <label class="name" for="linked-check-{key}">{device.device_name}</label>
              {:else}
                <span class="name">{device.device_name}</span>
              {/if}
              <span class="meta" id="linked-meta-{key}">
                {#if device.model}<span>{device.model}</span>{/if}
                {#if device.iot_id}
                  <span class="mono">{m.linked_device_iot()} {device.iot_id}</span>
                {/if}
                {#if reason}
                  <span class="reason" data-testid="linked-device-reason-{key}">{reason}</span>
                {/if}
              </span>
              {#if failure}
                <span class="fail" id="linked-fail-{key}" data-testid="linked-device-failure-{key}"
                  >{failure}</span
                >
              {/if}
            </span>
            <ToneBadge
              tone={online.tone}
              glyph={online.glyph}
              label={online.label()}
              value={onlineState(device)}
              testid="linked-device-online-{key}"
            />
          </li>
        {/each}
      </ul>
    </fieldset>
  {/if}
</div>

<style>
  .devices {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
  }

  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .sync {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
  }

  .label,
  legend {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .mono {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  .empty {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  fieldset {
    margin: 0;
    padding: 0;
    border: 0;
  }

  legend {
    display: flex;
    justify-content: space-between;
    gap: var(--md-space-2);
    width: 100%;
    padding: 0 0 var(--md-space-2);
  }

  .count {
    letter-spacing: 0;
    text-transform: none;
  }

  ul {
    margin: 0;
    padding: 0;
    border: var(--md-hairline) solid var(--md-color-line);
    list-style: none;
  }

  .row {
    display: grid;
    grid-template-columns: var(--md-target) minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: var(--md-space-2) var(--md-space-3) var(--md-space-2) 0;
    border-left: 3px solid var(--md-color-ink);
    background: var(--md-color-surface);
  }

  .row + .row {
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .row.off {
    border-left-color: var(--md-color-line);
    background: var(--md-color-surface-sunken);
    color: var(--md-color-ink-muted);
  }

  .row.failed {
    border-left-color: var(--md-state-error);
  }

  input[type='checkbox'] {
    justify-self: center;
    width: var(--md-space-6);
    height: var(--md-space-6);
    margin: 0;
    accent-color: var(--md-color-ink);
    cursor: pointer;
  }

  input[type='checkbox']:focus-visible {
    outline: var(--md-border-control) solid var(--md-color-focus);
    outline-offset: var(--md-space-1);
  }

  .slot {
    justify-self: center;
    width: var(--md-space-3);
    border-top: var(--md-border-control) solid var(--md-color-line-strong);
  }

  .body {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .name {
    font-family: var(--md-font-mono);
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  label.name {
    cursor: pointer;
  }

  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .reason {
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .fail {
    color: var(--md-state-error);
    font-size: var(--md-font-size-13);
  }
</style>
