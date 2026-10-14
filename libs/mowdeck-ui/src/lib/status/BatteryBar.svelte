<!--
  Ten segment battery gauge under the battery readout. Low charge (below 20 %) turns the lit
  segments to the error tone; charging adds the charging tone.
-->
<script lang="ts">
  interface Props {
    percent: number | null;
    charging?: boolean;
  }
  let { percent, charging = false }: Props = $props();
  const lit = $derived(percent === null ? 0 : Math.round(Math.max(0, Math.min(100, percent)) / 10));
  const tone = $derived(charging ? 'charging' : percent !== null && percent < 20 ? 'error' : 'ink');
</script>

<div class="md-battery" data-tone={tone} aria-hidden="true">
  {#each Array.from({ length: 10 }, (_, i) => i) as index (index)}
    <span class:on={index < lit}></span>
  {/each}
</div>

<style>
  .md-battery {
    --lit: var(--md-color-ink);

    display: grid;
    grid-template-columns: repeat(10, 1fr);
    gap: 2px;
    height: 6px;
  }

  .md-battery span {
    background: var(--md-color-line);
  }

  .md-battery span.on {
    background: var(--lit);
  }

  [data-tone='error'] {
    --lit: var(--md-state-error);
  }

  [data-tone='charging'] {
    --lit: var(--md-state-charging);
  }
</style>
