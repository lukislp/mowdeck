<!--
  The operator console (operator admins of the installation only): accounts, plans and system as
  tabs. The pages render once the operator probe answered 200; for everybody else a notice says
  the area is for operators (the services refuse the requests anyway). No page of the console
  reads or shows mower data.
-->
<script lang="ts">
  import { Icon, ProblemNotice, TopBar, type IconName } from '@mowdeck/ui';
  import type { Snippet } from 'svelte';

  import { page } from '$app/state';

  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { operatorAccess } from '#lib/operator/access.svelte.js';
  import { m } from '#lib/paraglide/messages.js';

  let { children }: { children: Snippet } = $props();

  const tabs: { id: string; href: string; label: () => string; icon: IconName }[] = [
    { id: 'accounts', href: '/operator', label: () => m.op_tab_accounts(), icon: 'users' },
    { id: 'plans', href: '/operator/plans', label: () => m.op_tab_plans(), icon: 'list' },
    { id: 'system', href: '/operator/system', label: () => m.op_tab_system(), icon: 'pulse' },
  ];

  function current(id: string): boolean {
    const path = page.url.pathname;
    if (id === 'plans') return path.startsWith('/operator/plans');
    if (id === 'system') return path.startsWith('/operator/system');
    return path === '/operator' || path.startsWith('/operator/accounts');
  }

  $effect(() => {
    void operatorAccess.load();
  });
</script>

{#if operatorAccess.allowed}
  <div class="console" data-testid="operator-console">
    <nav class="tabs" aria-label={m.op_tabs()}>
      <span class="badge" aria-hidden="true"><Icon name="shield" size={16} /></span>
      {#each tabs as tab (tab.id)}
        <a
          href={tab.href}
          aria-current={current(tab.id) ? 'page' : undefined}
          data-testid="operator-tab-{tab.id}"
        >
          <Icon name={tab.icon} size={18} />
          <span>{tab.label()}</span>
        </a>
      {/each}
    </nav>
    {@render children()}
  </div>
{:else if operatorAccess.checked}
  <TopBar title={m.nav_operator()} />
  <div class="pad" data-testid="operator-forbidden">
    <ProblemNotice title={m.op_forbidden_title()} detail={m.op_forbidden_body()} />
  </div>
{:else}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .console {
    display: flex;
    flex-direction: column;
  }

  /* The console's tab strip sits on top, inverted, so an operator always sees where they are. */
  .tabs {
    display: grid;
    grid-template-columns: var(--md-target);
    grid-auto-columns: minmax(0, 1fr);
    grid-auto-flow: column;
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .badge {
    display: grid;
    place-items: center;
    width: var(--md-target);
    border-inline-end: var(--md-hairline) solid var(--md-color-ink-muted);
  }

  a {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: 0 var(--md-space-2);
    color: var(--md-color-ink-inverse);
    opacity: 0.72;
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-decoration: none;
    text-transform: uppercase;
  }

  a[aria-current='page'] {
    opacity: 1;
  }

  a[aria-current='page']::after {
    content: '';
    position: absolute;
    inset-inline: var(--md-space-2);
    bottom: 0;
    height: 3px;
    background: var(--md-color-ink-inverse);
  }

  a:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  @media (min-width: 1024px) {
    .tabs {
      grid-auto-columns: max-content;
      padding-inline-end: var(--md-gutter);
    }

    a {
      padding: 0 var(--md-space-4);
    }
  }
</style>
