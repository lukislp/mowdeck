<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  The sheet of the safety acknowledgement: shows `safety` (`gate.svelte.ts`) when a feature asks.
  One for the whole app, mounted once in the app layout. Non-modal like the other sheets, so it
  stops above the emergency stop (which publishes its clearance on the root element) and never
  covers or delays it; Escape and the close button cancel. The box starts unticked every time.
-->
<script lang="ts">
  import { Button, Sheet } from '@mowdeck/ui';
  import { tick, untrack } from 'svelte';

  import { page } from '$app/state';

  import { m } from '#lib/paraglide/messages.js';

  import { safety } from './gate.svelte';
  import SafetyCheck from './SafetyCheck.svelte';

  let checked = $state(false);

  // Leaving the page cancels every pending question: a start cannot fire later on another screen.
  $effect(() => {
    void page.url.pathname;
    untrack(() => safety.cancelAll());
  });

  // Each question starts unticked (also when one follows another) and puts the keyboard on the
  // box (like the tour's primary).
  $effect(() => {
    void safety.shown;
    checked = false;
    if (!safety.open) return;
    void tick().then(() =>
      document.querySelector<HTMLElement>('[data-testid="safety-check-box"]')?.focus(),
    );
  });
</script>

<Sheet
  open={safety.open}
  title={m.safety_title()}
  closeLabel={m.close()}
  onclose={() => safety.cancel()}
  testid="safety-sheet"
>
  <SafetyCheck variant={safety.variant} bind:checked />
  {#snippet footer()}
    <div class="footer">
      <Button variant="ghost" testid="safety-cancel" onclick={() => safety.cancel()}
        >{m.cancel()}</Button
      >
      <Button
        variant="primary"
        icon="check"
        disabled={!checked}
        testid="safety-confirm"
        onclick={() => safety.confirm()}>{m.safety_go()}</Button
      >
    </div>
  {/snippet}
</Sheet>

<style>
  .footer {
    display: flex;
    justify-content: flex-end;
    gap: var(--md-space-2);
  }
</style>
