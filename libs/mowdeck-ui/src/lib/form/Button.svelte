<!-- A command button (48 px target). `primary` is ink filled; `danger` uses the error tone for
destructive account actions (never the stop colours). -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon, { type IconName } from '../icons/Icon.svelte';

  interface Props {
    variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
    type?: 'button' | 'submit';
    href?: string;
    icon?: IconName;
    disabled?: boolean;
    pending?: boolean;
    full?: boolean;
    label?: string;
    /** For a disclosure toggle: `aria-expanded` on the button. */
    expanded?: boolean;
    testid?: string;
    onclick?: (event: MouseEvent) => void;
    children?: Snippet;
  }
  let {
    variant = 'secondary',
    type = 'button',
    href,
    icon,
    disabled = false,
    pending = false,
    full = false,
    label,
    expanded,
    testid,
    onclick,
    children,
  }: Props = $props();
</script>

{#if href}
  <a class="md-button {variant}" class:full {href} aria-label={label} data-testid={testid}>
    {#if icon}<Icon name={icon} size={18} />{/if}
    {#if children}<span>{@render children()}</span>{/if}
  </a>
{:else}
  <button
    class="md-button {variant}"
    class:full
    {type}
    disabled={disabled || pending}
    aria-busy={pending ? 'true' : undefined}
    aria-label={label}
    aria-expanded={expanded}
    data-testid={testid}
    {onclick}
  >
    {#if icon}<Icon name={icon} size={18} />{/if}
    {#if children}<span>{@render children()}</span>{/if}
  </button>
{/if}

<style>
  .md-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--md-space-2);
    min-width: var(--md-target);
    min-height: var(--md-target);
    padding: 0 var(--md-space-4);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
    text-decoration: none;
    cursor: pointer;
    transition: background-color var(--md-motion-fast) var(--md-ease);
  }

  .md-button:hover:not(:disabled) {
    background: var(--md-color-surface-sunken);
  }

  .md-button.primary {
    border-color: var(--md-color-ink);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .md-button.primary:hover:not(:disabled) {
    background: var(--md-color-ink-muted);
  }

  .md-button.ghost {
    border-color: transparent;
    background: none;
  }

  .md-button.danger {
    border-color: var(--md-state-error);
    color: var(--md-state-error);
  }

  .md-button:disabled {
    border-style: dashed;
    color: var(--md-color-ink-muted);
    cursor: not-allowed;
  }

  .md-button.primary:disabled {
    border-color: var(--md-color-line-strong);
    background: var(--md-color-surface-sunken);
  }

  .md-button.full {
    width: 100%;
  }
</style>
