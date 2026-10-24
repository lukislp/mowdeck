<!--
  The "Bluetooth-Bridge" panel of the mower settings (ADR 0038): the empty state with "Bluetooth-
  Bridge hinzufügen", or the stored link with what the telemetry says about it (connected or not,
  the signal, the bridge firmware with an "veraltet" badge that names the reason and links to the
  guide), "Erneut prüfen" (a scan with the stored key) and "Entfernen" with a confirmation. The
  status comes from `MowerState.links.ble_bridge`; until the gateway reported it the panel says so.
-->
<script lang="ts">
  import { Button, Icon } from '@mowdeck/ui';

  import { asProblem } from '#lib/api/problem.js';
  import {
    BRIDGE_GUIDE_URL,
    type BridgeLink,
    type BridgeScanReply,
    type LinkStatus,
  } from '#lib/fleet/links.js';
  import { formatDateTime, problemMessage } from '#lib/i18n.js';
  import {
    outdatedText,
    rssiText,
    scanErrorText,
    scanProblemText,
  } from '#lib/mower/bridge-link.js';
  import { m } from '#lib/paraglide/messages.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  interface Props {
    link: BridgeLink | null;
    /** `state.links.ble_bridge` of the latest telemetry, or null before the gateway reported. */
    status: LinkStatus | null;
    /** "Bluetooth-Bridge hinzufügen". */
    onadd: () => void;
    /** `POST .../links/ble_bridge/check`. */
    check: () => Promise<BridgeScanReply>;
    /** `DELETE .../links/ble_bridge`. */
    remove: () => Promise<void>;
    onremoved: () => void;
  }
  let { link, status, onadd, check, remove, onremoved }: Props = $props();

  let checking = $state(false);
  let removing = $state(false);
  let confirmRemove = $state(false);

  const connected = $derived(status?.connected === true);
  const firmware = $derived(status?.esphome_version ?? link?.proxy_info?.esphome_version ?? null);
  const compiled = $derived(status?.compiled_at ?? link?.proxy_info?.compiled_at ?? null);
  const outdated = $derived(status ? status.outdated : (link?.proxy_info?.outdated ?? false));
  const outdatedReason = $derived(
    status ? status.outdated_reason : (link?.proxy_info?.outdated_reason ?? null),
  );

  async function recheck(): Promise<void> {
    if (!link || checking) return;
    checking = true;
    try {
      const reply = await check();
      if (reply.error) {
        toasts.push('error', scanErrorText(reply.error, link.host, link.port), reply.error.detail);
      } else {
        toasts.push(
          'ok',
          m.bridge_recheck_ok(),
          reply.proxy
            ? m.bridge_recheck_detail({
                name: reply.proxy.proxy_name,
                version: reply.proxy.esphome_version ?? '?',
              })
            : undefined,
        );
      }
    } catch (error) {
      toasts.push('error', scanProblemText(asProblem(error)));
    } finally {
      checking = false;
    }
  }

  async function removeLink(): Promise<void> {
    if (!confirmRemove) {
      confirmRemove = true;
      return;
    }
    removing = true;
    try {
      await remove();
      confirmRemove = false;
      toasts.push('ok', m.bridge_removed());
      onremoved();
    } catch (error) {
      const problem = asProblem(error);
      toasts.push('error', problemMessage(problem), problem.detail ?? undefined);
    } finally {
      removing = false;
    }
  }
</script>

{#if !link}
  <div class="empty" data-testid="bridge-empty">
    <p class="hint">{m.bridge_empty()}</p>
    <div class="row">
      <Button variant="primary" icon="plus" testid="bridge-add" onclick={onadd}
        >{m.bridge_add()}</Button
      >
      <a class="guide" href={BRIDGE_GUIDE_URL} target="_blank" rel="noopener noreferrer"
        >{m.bridge_guide()}</a
      >
    </div>
  </div>
{:else}
  <div class="link" data-testid="bridge-status" data-connected={status ? connected : 'unknown'}>
    <div class="chips">
      <span class="chip state" data-tone={status ? (connected ? 'mowing' : 'offline') : 'unknown'}>
        <span class="lamp" aria-hidden="true"></span>
        {#if !status}
          {m.activity_unknown()}
        {:else if connected}
          {m.bridge_connected()}
        {:else}
          {m.bridge_disconnected()}
        {/if}
      </span>
      {#if status?.rssi != null}
        <span class="chip mono" data-testid="bridge-rssi">
          <Icon name="signal" size={14} />
          {rssiText(status.rssi)}
        </span>
      {/if}
      {#if firmware}
        <span class="chip mono" data-testid="bridge-firmware">
          {m.bridge_firmware({ version: firmware })}{#if compiled}<span class="dim"
              >&nbsp;· {m.bridge_compiled({ date: compiled })}</span
            >{/if}
        </span>
      {:else}
        <span class="chip dim">{m.bridge_firmware_unknown()}</span>
      {/if}
      {#if outdated}
        <span class="chip badge" data-testid="bridge-outdated">
          <Icon name="alert" size={14} />
          {m.bridge_outdated()}
        </span>
      {/if}
      {#if status?.since}
        <span class="chip dim">{m.bridge_since({ time: formatDateTime(status.since) })}</span>
      {/if}
    </div>
    {#if outdated}
      <p class="outdated" data-testid="bridge-outdated-reason">
        {outdatedText(outdatedReason)}
        <a href={BRIDGE_GUIDE_URL} target="_blank" rel="noopener noreferrer">{m.bridge_guide()}</a>
      </p>
    {:else if !status}
      <p class="hint">{m.bridge_status_pending()}</p>
    {/if}
    <dl class="facts">
      <div>
        <dt>{m.conn_ble_host()}</dt>
        <dd class="mono" data-testid="bridge-host-value">{link.host}:{link.port}</dd>
      </div>
      <div>
        <dt>{m.conn_ble_device_name()}</dt>
        <dd class="mono" data-testid="bridge-device-value">
          {link.device_name}<span class="dim">&nbsp;· {link.ble_address}</span>
        </dd>
      </div>
      <div>
        <dt>{m.bridge_blades()}</dt>
        <dd>{link.allow_manual_blades ? m.switch_on() : m.switch_off()}</dd>
      </div>
    </dl>
    <div class="row">
      <Button icon="signal" pending={checking} testid="bridge-recheck" onclick={recheck}
        >{m.bridge_recheck()}</Button
      >
      <Button
        variant={confirmRemove ? 'danger' : 'ghost'}
        icon="trash"
        pending={removing}
        testid="bridge-remove"
        onclick={removeLink}>{confirmRemove ? m.bridge_remove_confirm() : m.bridge_remove()}</Button
      >
      {#if confirmRemove}
        <Button variant="ghost" onclick={() => (confirmRemove = false)}>{m.cancel()}</Button>
      {/if}
    </div>
    {#if confirmRemove}
      <p class="hint" data-testid="bridge-remove-body">{m.bridge_remove_body()}</p>
    {/if}
  </div>
{/if}

<style>
  .hint {
    margin: 0 0 var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2) var(--md-space-4);
  }

  .guide {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
  }

  .link {
    display: grid;
    gap: var(--md-space-3);
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: 28px;
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-13);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .dim {
    color: var(--md-color-ink-muted);
  }

  .state {
    --tone: var(--md-state-unknown);
    --tone-bg: var(--md-state-unknown-bg);

    border-color: var(--tone);
    background: var(--tone-bg);
    color: var(--tone);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .state[data-tone='mowing'] {
    --tone: var(--md-state-mowing);
    --tone-bg: var(--md-state-mowing-bg);
  }

  .state[data-tone='offline'] {
    --tone: var(--md-state-offline);
    --tone-bg: var(--md-state-offline-bg);

    border-style: dashed;
  }

  .state[data-tone='unknown'] {
    border-style: dashed;
  }

  .lamp {
    width: 8px;
    height: 8px;
    border: var(--md-hairline) solid currentcolor;
    border-radius: var(--md-radius-1);
  }

  .state[data-tone='mowing'] .lamp {
    background: currentcolor;
  }

  .badge {
    border-color: var(--md-state-paused);
    background: var(--md-state-paused-bg);
    color: var(--md-state-paused);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .outdated {
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 4px solid var(--md-state-paused);
    background: var(--md-state-paused-bg);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .outdated a {
    margin-inline-start: var(--md-space-1);
    color: var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
  }

  .facts {
    display: grid;
    gap: var(--md-space-2) var(--md-space-4);
    margin: 0;
  }

  .facts div {
    min-width: 0;
  }

  .facts dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .facts dd {
    margin: 0;
    font-size: var(--md-font-size-14);
    overflow-wrap: anywhere;
  }

  @media (min-width: 768px) {
    .facts {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
  }
</style>
