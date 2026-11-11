<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  The safety acknowledgement, as words and a checkbox: the area must be clear of people, children
  and animals, the user supervises the mower per its manual, and ticks the box to say so. Purely
  presentational (the rule which actions need it is `rule.ts`, the question a feature asks is
  `gate.svelte.ts`); the box is the same one the spot run and the calibration use.
-->
<script lang="ts">
  import { m } from '#lib/paraglide/messages.js';

  import type { SafetyVariant } from './rule';

  interface Props {
    variant?: SafetyVariant;
    checked: boolean;
    testid?: string;
  }
  let { variant = 'now', checked = $bindable(), testid = 'safety-check' }: Props = $props();
</script>

<div class="safety" data-testid={testid} data-variant={variant}>
  <p class="statement">{m.safety_statement()}</p>
  {#if variant === 'unattended'}
    <p class="statement">{m.safety_unattended()}</p>
  {/if}
  <label class="confirm">
    <input type="checkbox" bind:checked data-testid="{testid}-box" />
    <span>{variant === 'unattended' ? m.safety_confirm_unattended() : m.safety_confirm()}</span>
  </label>
</div>

<style>
  .safety {
    display: grid;
    gap: var(--md-space-3);
  }

  .statement {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .confirm {
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
    min-height: var(--md-target);
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 3px solid var(--md-state-error);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
    cursor: pointer;
  }

  .confirm input {
    flex: none;
    width: 20px;
    height: 20px;
    margin: 0;
    accent-color: var(--md-state-error);
  }
</style>
