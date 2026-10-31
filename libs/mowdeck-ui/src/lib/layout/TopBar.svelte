<!--
  Page header: optional back link, title, a short monospaced meta line and actions.

  On phones the bar is pinned under the status bar while the page scrolls (sticky against the
  document; a page that wraps it in a smaller box must let that box span the page, for example
  with `display: contents`). On the desktop it scrolls with the shell's content area.

  `stackActions`: on a phone (below 560 px) the actions always take their own line under the
  title, for a page with several actions whose number or status width changes after loading (the
  mower page): the bar keeps one height instead of wrapping later and moving the page under it.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from '../icons/Icon.svelte';

  interface Props {
    title: string;
    meta?: string;
    backHref?: string;
    backLabel?: string;
    actions?: Snippet;
    status?: Snippet;
    stackActions?: boolean;
  }
  let { title, meta, backHref, backLabel, actions, status, stackActions = false }: Props = $props();
</script>

<header class="md-top" class:stack={stackActions && !!actions}>
  {#if backHref}
    <a class="back" href={backHref} aria-label={backLabel}><Icon name="back" size={22} /></a>
  {/if}
  <div class="titles">
    <h1>{title}</h1>
    {#if meta}<span class="meta">{meta}</span>{/if}
  </div>
  {#if status}<div class="status">{@render status()}</div>{/if}
  {#if actions}<div class="actions">{@render actions()}</div>{/if}
</header>

<style>
  .md-top {
    position: sticky;
    top: var(--md-safe-top);
    z-index: 15;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-topbar-height);
    padding: var(--md-space-2) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  @media (min-width: 1024px) {
    .md-top {
      position: static;
    }
  }

  .back {
    display: grid;
    place-items: center;
    width: var(--md-target);
    height: var(--md-target);
    margin-inline-start: calc(-1 * var(--md-space-3));
    color: var(--md-color-ink);
  }

  /* The title grows into the free space but never below about ten characters: when the back
     link, the status and the actions leave less, the actions wrap to a second line instead of
     squeezing the name to a letter (a mower page at 390 px). */
  .titles {
    display: flex;
    flex-direction: column;
    flex: 1 1 0;
    min-width: min(9rem, 100%);
  }

  h1 {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .meta {
    overflow-wrap: anywhere;
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .actions,
  .status {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
  }

  /* When the back link, the status and the actions no longer fit one line on a narrow phone (four
     actions at 320 px, or a long status such as "Offline, letzte Werte"), the actions move to a
     second line, right-aligned, instead of pushing the page wider than the screen. The title
     claims only its minimum width (flex-basis 0, `min-width` above), so the bar only wraps when
     the name would otherwise be cut to a few letters. */
  .actions {
    flex-wrap: wrap;
    justify-content: flex-end;
    margin-inline-start: auto;
  }

  @media (max-width: 559px) {
    .stack .actions {
      flex-basis: 100%;
    }
  }
</style>
