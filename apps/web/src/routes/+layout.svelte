<script lang="ts">
  import '@mowdeck/ui/styles/fonts.css';
  import '@mowdeck/ui/styles/tokens.css';
  import '@mowdeck/ui/styles/base.css';

  import { ToastRegion } from '@mowdeck/ui';
  import { onMount, type Snippet } from 'svelte';

  import { session } from '#lib/auth/session.svelte.js';
  import StepUpSheet from '#lib/components/StepUpSheet.svelte';
  import { startLive } from '#lib/live.svelte.js';
  import { nativeShell } from '#lib/native/runtime.js';
  import { navigatePath } from '#lib/navigate.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import { appUpdate, install, toasts } from '#lib/stores/ui.svelte.js';

  let { children }: { children: Snippet } = $props();

  onMount(() => {
    document.documentElement.lang = getLocale();
    install.init();
    startLive();
    // No service worker inside the iOS/Android app shell (it loads each server release as is).
    void appUpdate.register(install.native);
    if (session.status === 'unknown') void session.restore().catch(() => undefined);
    // Inside the iOS/Android app shell: deep links and notification taps (inert in a browser).
    return nativeShell.start((path) => void navigatePath(path));
  });
</script>

{#if appUpdate.waiting && !install.native}
  <div class="update" role="status">
    <span>{m.update_available()}</span>
    <button type="button" onclick={() => appUpdate.apply()}>{m.update_reload()}</button>
  </div>
{/if}

{@render children()}

<StepUpSheet />

<ToastRegion
  toasts={toasts.items}
  dismissLabel={m.dismiss()}
  ondismiss={(id) => toasts.dismiss(id)}
/>

<style>
  .update {
    position: sticky;
    top: var(--md-safe-top);
    z-index: 40;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-3);
    padding: var(--md-space-1) var(--md-gutter);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
    font-size: var(--md-font-size-14);
  }

  .update button {
    min-height: 40px;
    border: var(--md-hairline) solid var(--md-color-ink-inverse);
    border-radius: var(--md-radius-2);
    background: none;
    color: inherit;
    font-weight: var(--md-weight-semibold);
    cursor: pointer;
  }
</style>
