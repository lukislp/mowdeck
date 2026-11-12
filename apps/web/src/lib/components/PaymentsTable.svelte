<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  A ledger table of the payments dashboard: mono, tabular figures, amounts aligned right,
  scrolling sideways inside its box on a phone. With `fallback` it is the text alternative of a
  chart and folds away behind a "Show as table" summary (still read by assistive technology once
  opened, and always present in the document).
-->
<script lang="ts">
  import { TableScroll } from '@mowdeck/ui';
  import type { Snippet } from 'svelte';

  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    caption: string;
    children: Snippet;
    fallback?: boolean;
    testid?: string;
  }
  let { caption, children, fallback = false, testid }: Props = $props();
</script>

{#if fallback}
  <details class="ledger-table" data-testid={testid}>
    <summary>{m.op_pay_table()}</summary>
    <TableScroll label={caption}>
      <table>
        <caption class="sr">{caption}</caption>
        {@render children()}
      </table>
    </TableScroll>
  </details>
{:else}
  <div class="ledger-table" data-testid={testid}>
    <TableScroll label={caption}>
      <table>
        <caption class="sr">{caption}</caption>
        {@render children()}
      </table>
    </TableScroll>
  </div>
{/if}

<style>
  .ledger-table {
    min-width: 0;
  }

  summary {
    width: fit-content;
    padding: var(--md-space-1) 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
  }

  summary:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--md-font-size-13);
  }

  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .ledger-table :global(th) {
    padding: var(--md-space-1) var(--md-space-2);
    border-bottom: var(--md-hairline) solid var(--md-color-ink);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-align: start;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .ledger-table :global(td) {
    padding: var(--md-space-1) var(--md-space-2);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    color: var(--md-color-ink);
    white-space: nowrap;
  }

  .ledger-table :global(.num) {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
    text-align: end;
  }

  .ledger-table :global(th.num) {
    font-family: var(--md-font-sans);
  }

  .ledger-table :global(.negative) {
    font-weight: var(--md-weight-semibold);
  }

  .ledger-table :global(tbody tr:last-child td) {
    border-bottom-color: var(--md-color-line-strong);
  }
</style>
