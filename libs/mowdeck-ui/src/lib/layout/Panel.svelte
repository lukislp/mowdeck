<!-- A section of a page: uppercase label on a hairline, optional actions, content. -->
<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    label: string;
    children: Snippet;
    actions?: Snippet;
    flush?: boolean;
    testid?: string;
  }
  let { label, children, actions, flush = false, testid }: Props = $props();
</script>

<section class="md-panel" class:flush data-testid={testid}>
  <div class="head">
    <h2>{label}</h2>
    {#if actions}<div class="actions">{@render actions()}</div>{/if}
  </div>
  <div class="body">{@render children()}</div>
</section>

<style>
  .md-panel {
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
    min-height: 40px;
    padding: var(--md-space-2) var(--md-gutter) 0;
  }

  .actions {
    display: flex;
    gap: var(--md-space-2);
  }

  .body {
    padding: var(--md-space-2) var(--md-gutter) var(--md-space-4);
  }

  .flush .body {
    padding-inline: 0;
    padding-bottom: 0;
  }
</style>
