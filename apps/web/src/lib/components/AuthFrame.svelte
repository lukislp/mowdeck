<!--
  Frame of the sign-in and registration pages: a nameplate on the metric grid (the same grid
  the map uses as background) and the form column. No hero, no illustration.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';

  import { m } from '$lib/paraglide/messages';

  let { title, children }: { title: string; children: Snippet } = $props();
</script>

<div class="frame">
  <aside class="plate" aria-hidden="true">
    <span class="mark">{m.brand_mark()}</span>
    <span class="tagline">{m.app_tagline()}</span>
    <span class="scale"><i></i><i></i><i></i><i></i><i></i></span>
  </aside>
  <main class="column">
    <h1>{title}</h1>
    {@render children()}
  </main>
</div>

<style>
  .frame {
    display: grid;
    grid-template-rows: auto 1fr;
    min-height: var(--md-viewport-height);
    background: var(--md-color-surface);
  }

  .plate {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    padding: var(--md-space-6) var(--md-gutter) var(--md-space-4);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background-color: var(--md-color-surface-sunken);
    background-image:
      linear-gradient(var(--md-color-line) var(--md-hairline), transparent var(--md-hairline)),
      linear-gradient(
        90deg,
        var(--md-color-line) var(--md-hairline),
        transparent var(--md-hairline)
      );
    background-size: 24px 24px;
    background-position: -1px -1px;
  }

  .mark {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-medium);
    letter-spacing: 0.32em;
  }

  .tagline {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .scale {
    display: flex;
    align-items: flex-end;
    gap: 0;
    margin-top: var(--md-space-3);
    width: 120px;
    height: 10px;
    border-bottom: var(--md-border-control) solid var(--md-color-ink);
  }

  .scale i {
    flex: 1;
    height: 6px;
    border-inline-start: var(--md-border-control) solid var(--md-color-ink);
  }

  .scale i:first-child {
    height: 10px;
  }

  .column {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-4);
    width: 100%;
    max-width: 440px;
    padding: var(--md-space-6) var(--md-gutter) var(--md-space-12);
  }

  @media (min-width: 900px) {
    .frame {
      grid-template-rows: none;
      grid-template-columns: minmax(320px, 5fr) 7fr;
    }

    .plate {
      justify-content: flex-end;
      padding: var(--md-space-8);
      border-bottom: 0;
      border-inline-end: var(--md-hairline) solid var(--md-color-line);
    }

    .mark {
      font-size: var(--md-font-size-32);
    }

    .column {
      place-self: center center;
      padding: var(--md-space-8);
    }
  }
</style>
