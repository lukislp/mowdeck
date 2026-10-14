<!-- An API problem in place: translated title, optional detail and the correlation id to quote
when reporting it. -->
<script lang="ts">
  import Icon from '../icons/Icon.svelte';

  interface Props {
    title: string;
    detail?: string | null;
    /** Label before the correlation id, e.g. "Referenz". */
    referenceLabel?: string;
    reference?: string | null;
    tone?: 'error' | 'info';
  }
  let { title, detail = null, referenceLabel, reference = null, tone = 'error' }: Props = $props();
</script>

<div class="md-problem" data-tone={tone} role={tone === 'error' ? 'alert' : 'status'}>
  <Icon name={tone === 'error' ? 'alert' : 'signal'} size={18} />
  <div class="body">
    <span class="title">{title}</span>
    {#if detail}<span class="detail">{detail}</span>{/if}
    {#if reference}
      <span class="ref">{referenceLabel} <code>{reference}</code></span>
    {/if}
  </div>
</div>

<style>
  .md-problem {
    --tone: var(--md-state-error);
    --tone-bg: var(--md-state-error-bg);

    display: flex;
    gap: var(--md-space-2);
    padding: var(--md-space-3);
    border-inline-start: 4px solid var(--tone);
    background: var(--tone-bg);
    color: var(--md-color-ink);
  }

  [data-tone='info'] {
    --tone: var(--md-color-line-strong);
    --tone-bg: var(--md-color-surface-sunken);
  }

  .md-problem > :global(svg) {
    color: var(--tone);
    margin-top: 2px;
  }

  .body {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .title {
    font-weight: var(--md-weight-semibold);
  }

  .detail,
  .ref {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  code {
    overflow-wrap: anywhere;
  }
</style>
