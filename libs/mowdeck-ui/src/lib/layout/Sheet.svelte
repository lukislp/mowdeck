<!--
  A non-modal sheet (area picker and similar). On phones it rises from the bottom but stops
  above the command bar (`--md-sheet-bottom`), so the emergency stop stays visible and usable.
  A sheet outside the page (mounted in a layout: "Was ist neu", step-up) does not see the page's
  `--md-sheet-bottom`; it falls back to `--md-stop-clearance` and `--md-stop-inset-start`, which a
  page with a fixed emergency stop publishes on the root element (bar height, rail width).
-->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from '../icons/Icon.svelte';

  interface Props {
    open: boolean;
    title: string;
    closeLabel: string;
    onclose: () => void;
    children: Snippet;
    footer?: Snippet;
    testid?: string;
    /** Desktop: about 760 px wide (two columns inside) instead of 400 px. */
    wide?: boolean;
  }
  let {
    open,
    title,
    closeLabel,
    onclose,
    children,
    footer,
    testid,
    wide = false,
  }: Props = $props();

  function keydown(event: KeyboardEvent): void {
    if (open && event.key === 'Escape') onclose();
  }
</script>

<svelte:window onkeydown={keydown} />

{#if open}
  <div class="md-sheet" class:wide role="dialog" aria-label={title} data-testid={testid}>
    <div class="head">
      <h2>{title}</h2>
      <button type="button" class="close" aria-label={closeLabel} onclick={onclose}>
        <Icon name="close" size={20} />
      </button>
    </div>
    <div class="body">{@render children()}</div>
    {#if footer}<div class="footer">{@render footer()}</div>{/if}
  </div>
{/if}

<style>
  .md-sheet {
    --sheet-bottom: var(--md-sheet-bottom, var(--md-stop-clearance, 0px));

    position: fixed;
    inset-inline: var(--md-stop-inset-start, 0) 0;
    bottom: var(--sheet-bottom);
    z-index: 25;
    display: flex;
    flex-direction: column;
    max-height: calc(var(--md-viewport-height) - var(--sheet-bottom) - 64px);
    border-top: var(--md-border-control) solid var(--md-color-ink);
    background: var(--md-color-surface);
    box-shadow: var(--md-shadow-float);
    animation: rise var(--md-motion-base) var(--md-ease);
  }

  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--md-space-2) var(--md-gutter) 0;
  }

  .close {
    display: grid;
    place-items: center;
    width: var(--md-target);
    height: var(--md-target);
    margin-inline-end: calc(-1 * var(--md-space-3));
    border: 0;
    background: none;
    color: var(--md-color-ink);
    cursor: pointer;
  }

  .body {
    overflow-y: auto;
    padding: 0 var(--md-gutter) var(--md-space-2);
  }

  .footer {
    padding: var(--md-space-2) var(--md-gutter) var(--md-space-3);
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  @keyframes rise {
    from {
      transform: translateY(16px);
      opacity: 0;
    }
  }

  @media (min-width: 1024px) {
    .md-sheet {
      --sheet-bottom: var(
        --md-sheet-bottom,
        calc(var(--md-stop-clearance, 0px) + var(--md-space-6))
      );

      inset-inline: auto var(--md-space-6);
      width: 400px;
      border: var(--md-hairline) solid var(--md-color-line-strong);
      border-radius: var(--md-radius-3);
    }

    .md-sheet.wide {
      width: min(760px, calc(100vw - 2 * var(--md-space-6)));
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .md-sheet {
      animation: none;
    }
  }
</style>
