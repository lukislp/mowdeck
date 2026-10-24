<!--
  "Loading …" that does not spin forever: after `after` ms (15 s) it says that loading takes
  unusually long and offers to try again (a reload, which starts every request and the session
  afresh). Inline content, so it fits into the pages' existing `<p role="status">` lines.
-->
<script lang="ts">
  import { onMount } from 'svelte';

  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    /** Milliseconds until the retry appears. */
    after?: number;
    /** What "try again" does (default: reload the page). */
    onretry?: () => void;
  }
  let { after = 15_000, onretry }: Props = $props();

  let slow = $state(false);

  onMount(() => {
    const timer = setTimeout(() => (slow = true), after);
    return () => clearTimeout(timer);
  });

  function retry(): void {
    if (onretry) onretry();
    else window.location.reload();
  }
</script>

{#if slow}
  <span class="slow" data-testid="loading-slow">
    <span>{m.loading_slow()}</span>
    <button type="button" class="retry" data-testid="loading-retry" onclick={retry}
      >{m.loading_retry()}</button
    >
  </span>
{:else}
  {m.loading()}
{/if}

<style>
  .slow {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
  }

  .retry {
    min-height: var(--md-target);
    padding: 0 var(--md-space-4);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font: inherit;
    font-weight: var(--md-weight-semibold);
    cursor: pointer;
  }

  .retry:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }
</style>
