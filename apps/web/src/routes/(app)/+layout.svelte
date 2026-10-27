<script lang="ts">
  import { AppShell, NavRail } from '@mowdeck/ui';
  import type { Snippet } from 'svelte';

  import { goto } from '$app/navigation';
  import { page } from '$app/state';

  import { session } from '#lib/auth/session.svelte.js';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { nativeShell } from '#lib/native/runtime.js';
  import { navigatePath } from '#lib/navigate.js';
  import { inbox, pushDevice } from '#lib/notifications/runtime.js';
  import { operatorAccess } from '#lib/operator/access.svelte.js';
  import { m } from '#lib/paraglide/messages.js';
  import { accountPlan } from '#lib/plan/plan.svelte.js';
  import { builtIn, navModules, registry, visibleModules } from '#lib/shell/registry.svelte.js';
  import { whatsNew } from '#lib/whats-new/whats-new.svelte.js';
  import WhatsNewSheet from '#lib/whats-new/WhatsNewSheet.svelte';

  let { children }: { children: Snippet } = $props();

  $effect(() => {
    if (session.status === 'anonymous') {
      const next = page.url.pathname + page.url.search;
      void goto(`/login?next=${encodeURIComponent(next)}`, { replace: true });
    } else if (session.status === 'authenticated') {
      void registry.load();
      void operatorAccess.load();
      void accountPlan.load();
      void whatsNew.load();
      if (session.me?.must_change_password && page.url.pathname !== '/settings') {
        void goto('/settings?password=1#profile', { replace: true });
      }
    }
  });

  const modules = $derived(
    visibleModules(builtIn, session.me?.permissions ?? [], registry.areas, operatorAccess.allowed),
  );
  const notifications = $derived(modules.some((module) => module.id === 'notifications'));
  // Settings sections (account, users, tokens, ...) live on the one settings page, not in the rail.
  const items = $derived(
    navModules(modules).map((module) => ({
      id: module.id,
      href: module.route,
      label: module.label(),
      icon: module.icon,
      active: page.url.pathname.startsWith(module.route),
      ...(module.id === 'notifications' && inbox.unread > 0
        ? { badge: inbox.unread, badgeLabel: m.notif_unread_count({ count: inbox.unread }) }
        : {}),
    })),
  );

  // The unread count in the navigation: polled while the app is open (no Realtime message).
  $effect(() => {
    if (session.status !== 'authenticated' || !notifications) return;
    return inbox.start();
  });

  $effect(() => {
    if (session.status === 'anonymous') {
      inbox.clear();
      operatorAccess.reset();
      accountPlan.reset();
    }
  });

  // Inside the app shell: APNs registration, local notifications from the inbox, icon badge.
  $effect(() => {
    if (session.status === 'authenticated') void nativeShell.signedIn();
  });

  $effect(() => {
    if (session.status !== 'authenticated' || !inbox.loaded) return;
    void nativeShell.inboxChanged(inbox.items, inbox.unread);
  });

  // Messages of the service worker: a click on a notification, a renewed push subscription.
  $effect(() => {
    if (session.status !== 'authenticated' || !('serviceWorker' in navigator)) return;
    const sync = () => void pushDevice.syncPending().catch(() => undefined);
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { type?: string; url?: string } | null;
      if (data?.type === 'navigate' && typeof data.url === 'string' && data.url.startsWith('/')) {
        void navigatePath(data.url);
      } else if (data?.type === 'push-renewed') {
        sync();
      }
    };
    navigator.serviceWorker.addEventListener('message', onMessage);
    sync();
    return () => navigator.serviceWorker.removeEventListener('message', onMessage);
  });
  // On a mower's page the command bar owns the bottom of a phone screen.
  const navOnPhone = $derived(!/^\/mowers\/[^/]+$/.test(page.url.pathname));
</script>

{#if session.status === 'authenticated'}
  <AppShell {navOnPhone}>
    {#snippet nav()}
      <NavRail {items} label={m.nav_label()} brand={m.brand_mark()} moreLabel={m.nav_more()} />
    {/snippet}
    {@render children()}
  </AppShell>
  <WhatsNewSheet />
{:else}
  <div class="boot" role="status" aria-live="polite">
    <span class="mark">{m.brand_mark()}</span>
    <span class="text"><SlowLoading /></span>
  </div>
{/if}

<style>
  .boot {
    display: grid;
    place-content: center;
    justify-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-viewport-height);
    color: var(--md-color-ink-muted);
  }

  .mark {
    color: var(--md-color-ink);
    font-family: var(--md-font-mono);
    letter-spacing: 0.32em;
  }

  .text {
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }
</style>
