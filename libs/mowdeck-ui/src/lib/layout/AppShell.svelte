<!--
  Page frame: navigation (rail on desktop, bottom bar on phones) and the content. Pages with
  their own bottom command bar pass `navOnPhone={false}` so the command bar owns the thumb zone.

  On the desktop the frame is bound to the viewport: the document never scrolls, the content
  area is the only scroll container (pages with their own scroll boxes, such as the mower
  detail, fill it exactly) and the rail scrolls inside itself when it does not fit.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    nav: Snippet;
    children: Snippet;
    navOnPhone?: boolean;
  }
  let { nav, children, navOnPhone = true }: Props = $props();
</script>

<div class="md-shell" class:phone-nav={navOnPhone}>
  <div class="nav">{@render nav()}</div>
  <main class="main" id="main">{@render children()}</main>
</div>

<style>
  .md-shell {
    min-height: 100dvh;
  }

  .nav {
    display: none;
  }

  .phone-nav .nav {
    display: block;
    position: fixed;
    inset-inline: 0;
    bottom: 0;
    z-index: 20;
  }

  .phone-nav .main {
    padding-bottom: calc(64px + env(safe-area-inset-bottom, 0px));
  }

  @media (min-width: 1024px) {
    .md-shell {
      display: grid;
      grid-template-columns: 88px minmax(0, 1fr);
      height: 100dvh;
      min-height: 0;
      overflow: hidden;
    }

    .nav,
    .phone-nav .nav {
      display: block;
      position: static;
      height: 100dvh;
      min-height: 0;
      overflow: hidden;
    }

    .main {
      height: 100dvh;
      min-height: 0;
      overflow-y: auto;
      overscroll-behavior: contain;
    }

    .phone-nav .main {
      padding-bottom: 0;
    }
  }
</style>
