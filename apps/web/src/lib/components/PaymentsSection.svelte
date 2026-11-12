<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  One section of the payments dashboard, set like a page of a ledger: a large mono folio number,
  the title on a heavy rule, then the content on a grid that opens to two columns on wide
  screens.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    folio: string;
    label: string;
    children: Snippet;
    testid?: string;
  }
  let { folio, label, children, testid }: Props = $props();

  const id = $derived(`payments-${folio}`);
</script>

<section class="ledger" aria-labelledby={id} data-testid={testid}>
  <header>
    <span class="folio" aria-hidden="true">{folio}</span>
    <h2 {id}>{label}</h2>
  </header>
  <div class="grid">{@render children()}</div>
</section>

<style>
  .ledger {
    padding: var(--md-space-6) var(--md-gutter) var(--md-space-4);
  }

  header {
    display: flex;
    align-items: baseline;
    gap: var(--md-space-3);
    padding-bottom: var(--md-space-2);
    border-bottom: 3px solid var(--md-color-ink);
  }

  .folio {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-28);
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }

  h2 {
    margin: 0;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-semibold);
    letter-spacing: normal;
    text-transform: none;
  }

  .grid {
    display: grid;
    gap: var(--md-space-6);
    padding-top: var(--md-space-4);
  }

  @media (min-width: 1024px) {
    .grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      column-gap: var(--md-space-8);
    }
  }
</style>
