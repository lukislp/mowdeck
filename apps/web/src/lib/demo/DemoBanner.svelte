<!--
  The demo band (ADR 0068): pinned above every page of a demo installation, in the browser and
  inside the app shells, and never dismissible. It sits in the strip the page frame reserves at
  the top (`--md-safe-top` grows by the band's height while `<html>` has the demo class), so no
  layout below has to know about it. A hatched "calibration tape" tag, as on a test rig. With demo
  sandboxes (ADR 0076) it says the visitor's sandbox is their own and when it is deleted.
-->
<script lang="ts">
  import { session } from '#lib/auth/session.svelte.js';
  import { navigatePath } from '#lib/navigate.js';
  import { m } from '#lib/paraglide/messages.js';

  import { markVisitor } from './sandbox';

  let {
    resetHours,
    href,
    explorer = false,
    idleMinutes = null,
  }: {
    resetHours: number;
    href: string;
    explorer?: boolean;
    /** Demo sandboxes (ADR 0076): minutes until an idle sandbox is deleted; null: shared demo. */
    idleMinutes?: number | null;
  } = $props();

  /** Signed out (the sign-in page): start the demo session first, then open the explorer,
   * instead of landing on the sign-in page again. A failed demo sign-in falls back to the
   * link, which the sign-in page then explains. */
  async function openExplorer(event: MouseEvent): Promise<void> {
    if (session.status === 'authenticated') return;
    event.preventDefault();
    try {
      await session.tryDemo();
      markVisitor();
    } catch {
      // The sign-in page shows why the demo is unavailable.
    }
    await navigatePath('/api-explorer');
  }
</script>

<div
  class="band"
  role="note"
  aria-label={m.demo_badge()}
  data-testid="demo-banner"
  data-mode={idleMinutes === null ? 'shared' : 'sandbox'}
>
  <span class="tag"><span class="tag-text">{m.demo_badge()}</span></span>
  <span class="text">
    {#if idleMinutes === null}
      <span class="long">{m.demo_banner({ hours: resetHours })}</span>
      <span class="short">{m.demo_banner_short({ hours: resetHours })}</span>
    {:else}
      <span class="long">{m.demo_banner_sandbox({ minutes: idleMinutes, hours: resetHours })}</span>
      <span class="short">{m.demo_banner_sandbox_short({ minutes: idleMinutes })}</span>
    {/if}
  </span>
  {#if explorer}
    <a class="get" href="/api-explorer" data-testid="demo-api-explorer" onclick={openExplorer}>
      <span class="long">{m.api_explorer_open()}</span>
      <span class="short">{m.api_explorer_open_short()}</span>
    </a>
  {/if}
  <a class="get" {href} target="_blank" rel="noopener">{m.demo_get()}</a>
</div>

<style>
  :global(:root.md-demo) {
    --md-demo-band: 32px;
    --md-safe-top: calc(env(safe-area-inset-top, 0px) + var(--md-demo-band));
  }

  .band {
    position: fixed;
    inset: env(safe-area-inset-top, 0) 0 auto;
    z-index: 60;
    display: flex;
    align-items: stretch;
    gap: var(--md-space-3);
    height: var(--md-demo-band);
    padding-inline: env(safe-area-inset-left, 0) env(safe-area-inset-right, 0);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    line-height: var(--md-demo-band);
    letter-spacing: var(--md-label-tracking);
    white-space: nowrap;
  }

  /* The tag: amber plate with hatched tape running off to the right. */
  .tag {
    display: flex;
    flex: none;
    align-items: center;
    padding-inline-end: 22px;
    background:
      linear-gradient(var(--md-hud-lit), var(--md-hud-lit)) left / calc(100% - 22px) 100% no-repeat,
      repeating-linear-gradient(-55deg, var(--md-hud-lit) 0 5px, transparent 5px 10px);
  }

  .tag-text {
    padding-inline: var(--md-space-3);
    color: var(--md-hud-ink-inverse);
    font-weight: var(--md-weight-semibold);
    letter-spacing: 0.24em;
    text-transform: uppercase;
  }

  .text {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .short {
    display: none;
  }

  .get {
    flex: none;
    padding-inline: var(--md-space-3);
    border-inline-start: var(--md-hairline) solid currentcolor;
    color: inherit;
    font-weight: var(--md-weight-semibold);
    text-decoration: none;
  }

  .get:hover,
  .get:focus-visible {
    text-decoration: underline;
  }

  @media (max-width: 640px) {
    .long {
      display: none;
    }

    .short {
      display: inline;
    }

    /* Phones: tighter, so the short text keeps its numbers whole next to the two links. */
    .band {
      gap: var(--md-space-2);
    }

    .tag-text {
      padding-inline: var(--md-space-2);
      letter-spacing: 0.16em;
    }

    .text {
      letter-spacing: 0;
    }

    .get {
      padding-inline: var(--md-space-2);
    }
  }
</style>
