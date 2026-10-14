<!--
  Primary navigation: icon plus short label per module; the brand mark on desktop.

  Phones keep a bottom bar of at most `phoneSlots` targets (default 5: each a fifth of a 390 px
  screen and 64 px tall). With more items and a `moreLabel`, the first `phoneSlots - 1` stay in
  the bar and the rest move into a "more" sheet that opens above the bar; the more button is
  marked current when the active page is one of them. The desktop rail shows every item.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon, { type IconName } from '../icons/Icon.svelte';

  interface NavItem {
    href: string;
    label: string;
    icon: IconName;
    active: boolean;
    id: string;
    /** A count shown on the icon (unread notifications); hidden when 0 or missing. */
    badge?: number;
    /** Accessible text of the badge, e.g. "3 ungelesen". */
    badgeLabel?: string;
  }
  interface Props {
    items: NavItem[];
    label: string;
    brand: string;
    footer?: Snippet;
    /** Label of the phone overflow button ("Mehr"); without it every item stays in the bar. */
    moreLabel?: string;
    /** Targets in the phone bar, the more button included. */
    phoneSlots?: number;
  }
  let { items, label, brand, footer, moreLabel, phoneSlots = 5 }: Props = $props();

  const uid = $props.id();
  let open = $state(false);
  let moreButton: HTMLButtonElement | undefined = $state();
  let sheet: HTMLDivElement | undefined = $state();

  const overflowing = $derived(!!moreLabel && items.length > phoneSlots);
  const kept = $derived(overflowing ? Math.max(1, phoneSlots - 1) : items.length);
  const extra = $derived(items.slice(kept));
  const extraActive = $derived(extra.some((item) => item.active));
  const extraBadge = $derived(extra.reduce((sum, item) => sum + (item.badge ?? 0), 0));
  const activeKey = $derived(items.find((item) => item.active)?.id ?? '');

  // A navigation (another item became active) closes the sheet.
  $effect(() => {
    void activeKey;
    open = false;
  });

  function onkeydown(event: KeyboardEvent): void {
    if (open && event.key === 'Escape') {
      open = false;
      moreButton?.focus();
    }
  }

  function onpointerdown(event: PointerEvent): void {
    if (!open) return;
    const target = event.target as Node;
    if (!sheet?.contains(target) && !moreButton?.contains(target)) open = false;
  }
</script>

<svelte:window {onkeydown} {onpointerdown} />

<nav class="md-nav" aria-label={label}>
  <span class="brand" aria-hidden="true">{brand}</span>
  <ul>
    {#each items as item, index (item.id)}
      <li class:overflow={index >= kept}>
        <a
          class="item"
          href={item.href}
          aria-current={item.active ? 'page' : undefined}
          data-testid="nav-{item.id}"
        >
          <span class="glyph">
            <Icon name={item.icon} size={22} />
            {#if item.badge}
              <span class="badge" data-testid="nav-{item.id}-badge">
                <span aria-hidden="true">{item.badge > 99 ? '99+' : item.badge}</span>
                {#if item.badgeLabel}<span class="sr">{item.badgeLabel}</span>{/if}
              </span>
            {/if}
          </span>
          <span class="label">{item.label}</span>
        </a>
      </li>
    {/each}
    {#if overflowing}
      <li class="more">
        <button
          bind:this={moreButton}
          type="button"
          class="item"
          aria-expanded={open}
          aria-controls="{uid}-more"
          data-current={extraActive || undefined}
          data-testid="nav-more"
          onclick={() => (open = !open)}
        >
          <span class="glyph">
            <Icon name="more" size={22} />
            {#if extraBadge}
              <span class="badge dot" aria-hidden="true"></span>
            {/if}
          </span>
          <span class="label">{moreLabel}</span>
        </button>
      </li>
    {/if}
  </ul>
  {#if overflowing && open}
    <div class="sheet" id="{uid}-more" bind:this={sheet} data-testid="nav-more-sheet">
      <ul>
        {#each extra as item (item.id)}
          <li>
            <a
              href={item.href}
              aria-current={item.active ? 'page' : undefined}
              data-testid="nav-more-{item.id}"
              onclick={() => (open = false)}
            >
              <Icon name={item.icon} size={22} />
              <span class="sheet-label">{item.label}</span>
              {#if item.badge}
                <span class="count" aria-hidden="true">{item.badge > 99 ? '99+' : item.badge}</span>
                {#if item.badgeLabel}<span class="sr">{item.badgeLabel}</span>{/if}
              {/if}
            </a>
          </li>
        {/each}
      </ul>
    </div>
  {/if}
  {#if footer}<div class="footer">{@render footer()}</div>{/if}
</nav>

<style>
  .md-nav {
    position: relative;
    display: flex;
    height: 100%;
    background: var(--md-color-surface);
    border-top: var(--md-hairline) solid var(--md-color-line);
    padding-bottom: env(safe-area-inset-bottom, 0);
  }

  .brand,
  .footer {
    display: none;
  }

  ul {
    display: grid;
    grid-auto-columns: minmax(0, 1fr);
    grid-auto-flow: column;
    flex: 1;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .item {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 2px;
    min-height: 64px;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-decoration: none;
    text-transform: uppercase;
  }

  button.item {
    width: 100%;
    padding: 0;
    border: 0;
    border-radius: var(--md-radius-0);
    background: none;
    font-family: inherit;
    cursor: pointer;
  }

  .item:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .item[aria-current='page'],
  .item[data-current],
  .item[aria-expanded='true'] {
    color: var(--md-color-ink);
  }

  /* Labels never run into their neighbours: at worst they are cut with an ellipsis. */
  .label {
    max-width: 100%;
    padding-inline: var(--md-space-0_5);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Five or more targets on a phone: plain case without tracking, so German labels fit. */
  ul:has(li:nth-child(5)) .item {
    letter-spacing: 0;
    text-transform: none;
  }

  /* Phones: the items past the slots live in the more sheet. */
  .overflow {
    display: none;
  }

  .glyph {
    position: relative;
    display: block;
  }

  .badge {
    position: absolute;
    top: -6px;
    left: 14px;
    min-width: 18px;
    height: 18px;
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid var(--md-color-surface);
    border-radius: var(--md-radius-1);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-medium);
    line-height: 16px;
    letter-spacing: 0;
    text-align: center;
    font-variant-numeric: tabular-nums;
  }

  .badge.dot {
    top: -2px;
    left: 18px;
    min-width: 10px;
    height: 10px;
    padding: 0;
  }

  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .item[aria-current='page']::before,
  .item[data-current]::before {
    content: '';
    position: absolute;
    inset-inline: 25%;
    top: 0;
    height: 3px;
    background: var(--md-color-ink);
  }

  .sheet {
    position: absolute;
    inset-inline: 0;
    bottom: 100%;
    max-height: 60dvh;
    overflow-y: auto;
    border-top: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
    box-shadow: var(--md-shadow-float);
  }

  .sheet ul {
    display: block;
  }

  .sheet li + li {
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .sheet a {
    position: relative;
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
    min-height: var(--md-target-command);
    padding: 0 var(--md-gutter);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
    text-decoration: none;
  }

  .sheet a[aria-current='page'] {
    color: var(--md-color-ink);
  }

  .sheet a:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .sheet a[aria-current='page']::before {
    content: '';
    position: absolute;
    inset-block: 20%;
    inset-inline-start: 0;
    width: 3px;
    background: var(--md-color-ink);
  }

  .sheet-label {
    flex: 1;
  }

  .count {
    min-width: 22px;
    padding: 0 var(--md-space-1);
    border-radius: var(--md-radius-1);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    text-align: center;
  }

  /* Desktop: the brand stays pinned at the top, the items scroll inside the rail when the
     window is too low for them (never the page), compact 60 px tiles. */
  @media (min-width: 1024px) {
    .md-nav {
      flex-direction: column;
      min-height: 0;
      border-top: 0;
      border-inline-end: var(--md-hairline) solid var(--md-color-line);
      padding-bottom: 0;
    }

    .brand {
      display: block;
      flex: none;
      align-self: center;
      padding: var(--md-space-4) 0 var(--md-space-3);
      font-family: var(--md-font-mono);
      font-size: var(--md-font-size-11);
      font-weight: var(--md-weight-medium);
      letter-spacing: 0.2em;
    }

    ul {
      grid-auto-flow: row;
      grid-auto-rows: min-content;
      flex: 1 1 auto;
      align-content: start;
      min-height: 0;
      overflow-y: auto;
      overscroll-behavior: contain;
      scrollbar-width: thin;
    }

    .overflow {
      display: block;
    }

    .more,
    .sheet {
      display: none;
    }

    .item,
    ul:has(li:nth-child(5)) .item {
      gap: var(--md-space-1);
      min-height: 60px;
      padding: var(--md-space-1) 0;
      letter-spacing: var(--md-label-tracking);
      text-transform: uppercase;
    }

    .label {
      max-width: 84px;
      font-size: var(--md-font-size-11);
      line-height: var(--md-line-16);
      text-align: center;
      overflow-wrap: anywhere;
      white-space: normal;
    }

    .item[aria-current='page']::before {
      inset: 20% auto 20% 0;
      width: 3px;
      height: auto;
    }

    .footer {
      display: flex;
      flex: none;
      flex-direction: column;
      align-items: center;
      gap: var(--md-space-2);
      margin-top: auto;
      padding-bottom: var(--md-space-4);
    }
  }
</style>
