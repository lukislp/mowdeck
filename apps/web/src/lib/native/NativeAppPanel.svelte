<!--
  Notifications of the iOS/Android app shell on the settings page (replaces the Web Push panel
  inside the app): the server the app loads, Apple Push on iOS, local notifications, and on
  Android the way to ntfy for the background.
-->
<script lang="ts">
  import { Button, Panel, ProblemNotice, Readout } from '@mowdeck/ui';

  import { m } from '#lib/paraglide/messages.js';

  import type { ApnsState } from './apns';
  import { nativeShell } from './runtime';

  let {
    apnsOnServer,
    ntfyTarget,
  }: {
    /** The server has an APNs key (`apns` is among the available channels). */
    apnsOnServer: boolean;
    /** The user's ntfy target, for the subscribe link of the ntfy app. */
    ntfyTarget: { server_url: string; topic: string } | null;
  } = $props();

  let busy = $state(false);

  const ntfyLink = $derived(
    ntfyTarget
      ? `ntfy://${ntfyTarget.server_url.replace(/^https?:\/\//, '')}/${encodeURIComponent(ntfyTarget.topic)}`
      : null,
  );

  function apnsLabel(state: ApnsState): string {
    switch (state) {
      case 'on':
        return m.native_apns_on();
      case 'pending':
        return m.native_apns_pending();
      case 'denied':
        return m.native_apns_denied();
      case 'error':
        return m.native_apns_error();
      case 'idle':
        return m.native_apns_off();
    }
  }

  function localLabel(): string {
    if (nativeShell.platform === 'android' && nativeShell.ntfy) return m.native_local_ntfy();
    if (nativeShell.platform === 'ios' && nativeShell.apns === 'on') return m.native_apns_on();
    return nativeShell.local ? m.native_local_on() : m.native_local_off();
  }

  async function enableApns(): Promise<void> {
    busy = true;
    try {
      await nativeShell.enableApns();
    } finally {
      busy = false;
    }
  }
</script>

<Panel label={m.native_title()} testid="native-panel">
  <div class="stack">
    <div class="readouts">
      <Readout
        label={m.native_server()}
        value={nativeShell.server}
        size="inline"
        emptyText={m.no_data()}
        testid="native-server"
      />
      {#if nativeShell.apnsAvailable}
        <Readout
          label={m.native_apns()}
          value={apnsLabel(nativeShell.apns)}
          size="inline"
          emptyText={m.no_data()}
          testid="native-apns"
        />
      {/if}
      <Readout
        label={m.native_local()}
        value={localLabel()}
        size="inline"
        emptyText={m.no_data()}
        testid="native-local"
      />
    </div>

    {#if nativeShell.apnsAvailable}
      {#if !apnsOnServer}
        <ProblemNotice tone="info" title={m.native_apns_no_server()} />
      {:else if nativeShell.apns === 'denied'}
        <ProblemNotice tone="info" title={m.native_apns_denied_hint()} />
      {:else if nativeShell.apns === 'error'}
        <ProblemNotice title={m.native_apns_error_hint()} />
      {/if}
      {#if apnsOnServer && nativeShell.apns !== 'on'}
        <div>
          <Button
            variant="primary"
            icon="bell"
            pending={busy}
            testid="native-apns-enable"
            onclick={enableApns}>{m.native_apns_enable()}</Button
          >
        </div>
      {/if}
    {/if}

    {#if nativeShell.platform === 'android'}
      <p class="quiet small">{m.native_android_hint()}</p>
      <div class="links">
        {#if ntfyLink}
          <a class="link" href={ntfyLink} data-testid="native-ntfy-open">{m.native_ntfy_open()}</a>
        {/if}
        <a class="link" href="https://ntfy.sh/docs/subscribe/phone/" rel="external noopener"
          >{m.native_ntfy_get()}</a
        >
      </div>
    {/if}

    <div>
      <Button
        variant="secondary"
        icon="link"
        testid="native-change-server"
        onclick={() => void nativeShell.changeServer()}>{m.native_change_server()}</Button
      >
    </div>
  </div>
</Panel>

<style>
  .stack {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
  }

  .readouts {
    display: grid;
    gap: var(--md-space-2);
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .small {
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  .links {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-4);
  }

  .link {
    color: var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
    text-underline-offset: 3px;
  }
</style>
