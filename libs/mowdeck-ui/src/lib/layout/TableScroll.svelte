<!--
  Sideways scroll box for a wide table (or any wide row of content): the table keeps its natural
  width and scrolls inside the box, the page never scrolls sideways. A soft shade at an edge
  shows that more columns wait there; it disappears once that edge is reached.

  The box contains its inline size, so the table's width never leaks into the parent: an `auto`
  grid track or a grid item without `min-width: 0` would otherwise grow to the table's width
  and push the whole page sideways on a phone. Inside a flex row, give the box a width (it has
  none of its own). The box is transparent; its edge covers take `--md-table-scroll-bg`
  (default: the page background), set it when the box sits on a surface.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    children: Snippet;
    /** Names the scroll region for assistive technology (e.g. the table's caption). */
    label?: string;
    testid?: string;
  }
  let { children, label, testid }: Props = $props();
</script>

<div
  class="md-table-scroll"
  role={label ? 'region' : undefined}
  aria-label={label}
  data-testid={testid}
>
  {@render children()}
</div>

<style>
  .md-table-scroll {
    --cover: var(--md-table-scroll-bg, var(--md-color-bg));
    --shade: color-mix(in srgb, var(--md-color-ink) 18%, transparent);

    contain: inline-size;
    max-inline-size: 100%;

    /* Sideways only: the box never becomes a second vertical scroller of the page. */
    overflow: auto hidden;
    overscroll-behavior-x: contain;
    scrollbar-width: thin;

    /* Scroll shades: the covers move with the content (local) and hide the shades (scroll) once
       the content's edge reaches the box's edge. */
    background:
      linear-gradient(to right, var(--cover) 40%, transparent) left / 32px 100% no-repeat local,
      linear-gradient(to left, var(--cover) 40%, transparent) right / 32px 100% no-repeat local,
      radial-gradient(farthest-side at 0 50%, var(--shade), transparent) left / 12px 100% no-repeat
        scroll,
      radial-gradient(farthest-side at 100% 50%, var(--shade), transparent) right / 12px 100%
        no-repeat scroll;
  }

  .md-table-scroll > :global(table) {
    min-inline-size: 100%;
  }
</style>
