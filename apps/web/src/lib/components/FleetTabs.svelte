<!-- The sections of the fleet module: map, sites, groups, RTK base stations (a tab strip under the
  top bar). -->
<script lang="ts">
  import { Icon, type IconName } from '@mowdeck/ui';

  import { page } from '$app/state';

  import { m } from '#lib/paraglide/messages.js';

  const tabs: {
    href: string;
    label: () => string;
    /** A shorter label for phones, where four tabs share 390 px. */
    short?: () => string;
    icon: IconName;
    id: string;
  }[] = [
    { id: 'map', href: '/fleet', label: () => m.fleet_tab_map(), icon: 'fleet' },
    { id: 'sites', href: '/fleet/sites', label: () => m.fleet_tab_sites(), icon: 'site' },
    { id: 'groups', href: '/fleet/groups', label: () => m.fleet_tab_groups(), icon: 'group' },
    {
      id: 'base-stations',
      href: '/fleet/base-stations',
      label: () => m.fleet_tab_base_stations(),
      short: () => m.fleet_tab_base_stations_short(),
      icon: 'signal',
    },
  ];
</script>

<nav class="tabs" aria-label={m.fleet_tabs()}>
  {#each tabs as tab (tab.id)}
    <a
      href={tab.href}
      aria-current={page.url.pathname === tab.href ? 'page' : undefined}
      aria-label={tab.short ? tab.label() : undefined}
      data-testid="fleet-tab-{tab.id}"
    >
      <Icon name={tab.icon} size={18} />
      {#if tab.short}
        <span class="long" aria-hidden="true">{tab.label()}</span>
        <span class="short" aria-hidden="true">{tab.short()}</span>
      {:else}
        <span>{tab.label()}</span>
      {/if}
    </a>
  {/each}
</nav>

<style>
  .tabs {
    display: grid;
    grid-auto-columns: minmax(0, 1fr);
    grid-auto-flow: column;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  a {
    position: relative;
    display: flex;
    min-width: 0;
    overflow: hidden;
    align-items: center;
    justify-content: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: 0 var(--md-space-2);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-decoration: none;
    text-transform: uppercase;
  }

  a + a {
    border-inline-start: var(--md-hairline) solid var(--md-color-line);
  }

  a[aria-current='page'] {
    color: var(--md-color-ink);
  }

  a[aria-current='page']::after {
    content: '';
    position: absolute;
    inset-inline: 0;
    bottom: -1px;
    height: 3px;
    background: var(--md-color-ink);
  }

  a:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .long {
    display: none;
  }

  /* Phones: icon over label, so four tabs fit without the page scrolling sideways. */
  @media (max-width: 639px) {
    a {
      flex-direction: column;
      gap: var(--md-space-0_5);
      padding: var(--md-space-1) var(--md-space-1);
      font-size: var(--md-font-size-11);
      letter-spacing: 0.02em;
    }
  }

  @media (min-width: 640px) {
    .long {
      display: inline;
    }

    .short {
      display: none;
    }
  }

  @media (min-width: 1024px) {
    .tabs {
      grid-auto-columns: max-content;
      padding-inline: var(--md-gutter);
    }

    a {
      padding: 0 var(--md-space-4);
    }

    a + a {
      border-inline-start: 0;
    }
  }
</style>
