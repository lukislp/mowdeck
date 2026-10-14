<!--
  Permission gate: renders `children` when `allowed`, otherwise `fallback` or nothing. The app
  computes `allowed`; the gate only avoids offering what the server would refuse and never
  replaces the server check.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    allowed: boolean;
    children: Snippet;
    fallback?: Snippet;
  }
  let { allowed, children, fallback }: Props = $props();
</script>

{#if allowed}
  {@render children()}
{:else if fallback}
  {@render fallback()}
{/if}
