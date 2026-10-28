<!-- Toasts above the command bar (never over the emergency stop); newest last. -->
<script lang="ts">
  import Icon from '../icons/Icon.svelte';
  import type { ToastItem } from '../types.js';

  interface Props {
    toasts: ToastItem[];
    dismissLabel: string;
    ondismiss: (id: string) => void;
  }
  let { toasts, dismissLabel, ondismiss }: Props = $props();
</script>

<div class="md-toasts" aria-live="polite">
  {#each toasts as toast (toast.id)}
    <div class="toast" data-tone={toast.tone} role={toast.tone === 'error' ? 'alert' : 'status'}>
      <span class="mark" aria-hidden="true"></span>
      <div class="body">
        <span class="title">{toast.title}</span>
        {#if toast.detail}<span class="detail">{toast.detail}</span>{/if}
        {#if toast.code}<code>{toast.code}</code>{/if}
      </div>
      <button type="button" aria-label={dismissLabel} onclick={() => ondismiss(toast.id)}>
        <Icon name="close" size={16} />
      </button>
    </div>
  {/each}
</div>

<style>
  .md-toasts {
    position: fixed;
    inset-inline: calc(var(--md-gutter) + var(--md-stop-inset-start, 0px)) var(--md-gutter);
    bottom: var(--md-toast-offset, var(--md-space-4));
    z-index: 30;
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    pointer-events: none;
  }

  .toast {
    --tone: var(--md-color-ink-muted);

    display: grid;
    grid-template-columns: 4px 1fr var(--md-target);
    align-items: stretch;
    max-width: 480px;
    margin-inline-start: auto;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    box-shadow: var(--md-shadow-float);
    pointer-events: auto;
  }

  .mark {
    background: var(--tone);
  }

  [data-tone='ok'] {
    --tone: var(--md-state-mowing);
  }

  [data-tone='error'] {
    --tone: var(--md-state-error);
  }

  .body {
    display: flex;
    flex-direction: column;
    padding: var(--md-space-2) var(--md-space-3);
    min-width: 0;
  }

  .title {
    font-weight: var(--md-weight-semibold);
  }

  .detail {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  code {
    overflow-wrap: anywhere;
    color: var(--md-color-ink-muted);
  }

  button {
    border: 0;
    background: none;
    color: var(--md-color-ink-muted);
    cursor: pointer;
  }

  /* Desktop: the stop sits at the foot of the instrument column on the right, where the toasts
     are, too: they keep clear of it (--md-stop-clearance, published by such a page). */
  @media (min-width: 1024px) {
    .md-toasts {
      bottom: calc(var(--md-stop-clearance, 0px) + var(--md-space-4));
    }
  }
</style>
