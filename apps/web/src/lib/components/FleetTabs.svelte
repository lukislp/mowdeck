<!-- The sections of the fleet module: map, sites, groups (a tab strip under the top bar). -->
<script lang="ts">
  import { Icon, type IconName } from '@mowdeck/ui';

  import { page } from '$app/state';

  import { m } from '$lib/paraglide/messages';

  const tabs: { href: string; label: () => string; icon: IconName; id: string }[] = [
    { id: 'map', href: '/fleet', label: () => m.fleet_tab_map(), icon: 'fleet' },
    { id: 'sites', href: '/fleet/sites', label: () => m.fleet_tab_sites(), icon: 'site' },
    { id: 'groups', href: '/fleet/groups', label: () => m.fleet_tab_groups(), icon: 'group' },
  ];
</script>

<nav class="tabs" aria-label={m.fleet_tabs()}>
  {#each tabs as tab (tab.id)}
    <a
      href={tab.href}
      aria-current={page.url.pathname === tab.href ? 'page' : undefined}
      data-testid="fleet-tab-{tab.id}"
    >
      <Icon name={tab.icon} size={18} />
      <span>{tab.label()}</span>
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
