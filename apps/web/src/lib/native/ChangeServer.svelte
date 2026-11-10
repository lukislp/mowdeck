<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  "Change server" inside the iOS/Android app: the server the app loads and the way to leave it
  (a two-step button, as everywhere in the app). Renders nothing in a browser. Wiring only: the
  hand-over (push token, widget credential, session) is `nativeShell.changeServer()`.

  - `bare`: the settings page wraps it in its own panel.
  - `line`: one quiet row under the sign-in and registration forms.
-->
<script lang="ts">
  import { Button } from '@mowdeck/ui';

  import { m } from '#lib/paraglide/messages.js';

  import { nativeShell } from './runtime';

  let { testid, variant = 'bare' }: { testid: string; variant?: 'bare' | 'line' } = $props();

  let confirming = $state(false);
  let busy = $state(false);

  const host = $derived.by(() => {
    const server = nativeShell.server;
    if (!server) return null;
    try {
      return new URL(server).host;
    } catch {
      return server;
    }
  });

  async function change(): Promise<void> {
    busy = true;
    try {
      await nativeShell.changeServer();
    } finally {
      // The shell restarts on its first-launch screen; this only matters if it did not.
      busy = false;
      confirming = false;
    }
  }
</script>

{#if nativeShell.active}
  <div class="server {variant}" data-testid="{testid}-block">
    <p class="current">
      <span class="label">{m.native_server()}</span>
      <span class="host" data-testid="{testid}-host">{host ?? m.no_data()}</span>
    </p>
    {#if confirming}
      <p class="confirm" role="alert">{m.native_change_server_confirm()}</p>
      <div class="row">
        <Button
          variant="danger"
          icon="link"
          pending={busy}
          testid="{testid}-confirm"
          onclick={change}>{m.native_change_server_confirm_action()}</Button
        >
        <Button variant="ghost" disabled={busy} onclick={() => (confirming = false)}
          >{m.cancel()}</Button
        >
      </div>
    {:else}
      <div>
        <Button variant="secondary" icon="link" {testid} onclick={() => (confirming = true)}
          >{m.native_change_server()}</Button
        >
      </div>
    {/if}
  </div>
{/if}

<style>
  .server {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
  }

  .line {
    padding-top: var(--md-space-3);
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .current {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0 var(--md-space-2);
    margin: 0;
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .host {
    min-width: 0;
    overflow-wrap: anywhere;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-14);
  }

  .confirm {
    margin: 0;
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }
</style>
