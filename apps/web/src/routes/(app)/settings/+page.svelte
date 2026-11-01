<!--
  The one settings page: every settings module the user may see as a section under its anchor
  (account and profile, security, notifications, users, AI provider, Matter, linked accounts, API tokens, API access; the
  order of `module.ts`), then Neuigkeiten (the "Was ist neu" history) and About for everyone (the build and its source code, AGPL-3.0). A numbered section index sits on the left on the desktop and as a strip
  under the top bar on phones; it marks the section in view. Old routes forward here
  (`#lib/settings/sections.js`).
-->
<script lang="ts">
  import { TopBar } from '@mowdeck/ui';
  import { tick, type Component } from 'svelte';

  import { page } from '$app/state';

  import { session } from '#lib/auth/session.svelte.js';
  import { operatorAccess } from '#lib/operator/access.svelte.js';
  import { m } from '#lib/paraglide/messages.js';
  import { settingsSections, type SettingsSection } from '#lib/settings/sections.js';
  import AboutSection from '#lib/settings/sections/AboutSection.svelte';
  import AiProviderSection from '#lib/settings/sections/AiProviderSection.svelte';
  import ApiAccessSection from '#lib/settings/sections/ApiAccessSection.svelte';
  import ApiTokensSection from '#lib/settings/sections/ApiTokensSection.svelte';
  import LinkedAccountsSection from '#lib/settings/sections/LinkedAccountsSection.svelte';
  import MatterSection from '#lib/settings/sections/MatterSection.svelte';
  import NotificationsSection from '#lib/settings/sections/NotificationsSection.svelte';
  import ProfileSection from '#lib/settings/sections/ProfileSection.svelte';
  import SecuritySection from '#lib/settings/sections/SecuritySection.svelte';
  import SetupSection from '#lib/settings/sections/SetupSection.svelte';
  import UsersSection from '#lib/settings/sections/UsersSection.svelte';
  import WhatsNewSection from '#lib/settings/sections/WhatsNewSection.svelte';
  import { builtIn, registry, visibleModules } from '#lib/shell/registry.svelte.js';

  /** The component of each settings module (by module id). */
  const VIEWS: Record<string, Component> = {
    account: ProfileSection,
    security: SecuritySection,
    'notification-settings': NotificationsSection,
    users: UsersSection,
    'ai-provider': AiProviderSection,
    matter: MatterSection,
    'linked-accounts': LinkedAccountsSection,
    'api-tokens': ApiTokensSection,
    setup: SetupSection,
    'api-access': ApiAccessSection,
    'whats-new': WhatsNewSection,
    about: AboutSection,
  };

  /** Not a module: every user sees the build and its source code (AGPL-3.0 section 13). */
  const ABOUT: SettingsSection = { id: 'about', anchor: 'about', label: () => m.source_about() };
  /** Not a module either: the way back into the setup tour (ADR 0062), for every user. */
  const SETUP: SettingsSection = {
    id: 'setup',
    anchor: 'setup',
    label: () => m.onboarding_title(),
  };
  /** Not a module either: the history of "Was ist neu", just before About. */
  const WHATS_NEW: SettingsSection = {
    id: 'whats-new',
    anchor: 'whats-new',
    label: () => m.whatsnew_section(),
  };

  const sections = $derived(
    settingsSections(
      visibleModules(
        builtIn,
        session.me?.permissions ?? [],
        registry.areas,
        operatorAccess.allowed,
      ),
    )
      .filter(
        (section) => section.id in VIEWS && section.id !== ABOUT.id && section.id !== SETUP.id,
      )
      .concat(SETUP, WHATS_NEW, ABOUT),
  );
  const anchors = $derived(sections.map((section) => section.anchor).join(' '));
  let current = $state('');

  // A link with an anchor (`/settings#users`): scroll there once the sections are rendered, and
  // again when the set of sections changes underneath (the ui_areas arrive later).
  $effect(() => {
    const hash = decodeURIComponent(page.url.hash.replace(/^#/, ''));
    void anchors;
    if (!hash) return;
    void tick().then(() => document.getElementById(hash)?.scrollIntoView({ block: 'start' }));
  });

  // The section in view is marked in the index: the last section whose top has passed the
  // upper third of the scroll box (main on desktop, the window on phones). Read on scroll and
  // resize rather than through an IntersectionObserver, whose viewport-based bands lagged one
  // section behind once main became the scroll container.
  $effect(() => {
    void anchors;
    const nodes = sections
      .map((section) => document.getElementById(section.anchor))
      .filter((node): node is HTMLElement => node !== null);
    if (!nodes.length) return;
    const first = nodes[0];
    if (!first) return;
    const box = scrollParent(first);
    const update = () => {
      const top = box ? box.getBoundingClientRect().top : 0;
      const height = box ? box.clientHeight : window.innerHeight;
      const line = top + height / 3;
      let active = nodes[0]?.id ?? '';
      for (const node of nodes) {
        if (node.getBoundingClientRect().top <= line + 1) active = node.id;
      }
      // At the very end of the scroll box the last section counts even when it is short.
      const end = box
        ? box.scrollTop + box.clientHeight >= box.scrollHeight - 2
        : window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
      if (end) active = nodes[nodes.length - 1]?.id ?? active;
      current = active;
    };
    const target: EventTarget = box ?? window;
    target.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    update();
    return () => {
      target.removeEventListener('scroll', update);
      window.removeEventListener('resize', update);
    };
  });

  /** The nearest ancestor that scrolls vertically, or null for the window. */
  function scrollParent(node: HTMLElement): HTMLElement | null {
    let el: HTMLElement | null = node.parentElement;
    while (el && el !== document.body) {
      const overflow = getComputedStyle(el).overflowY;
      if ((overflow === 'auto' || overflow === 'scroll') && el.scrollHeight > el.clientHeight) {
        return el;
      }
      el = el.parentElement;
    }
    return null;
  }

  const marked = $derived(current || sections[0]?.anchor || '');
</script>

<svelte:head><title>{m.settings_title()} · {m.app_name()}</title></svelte:head>

<TopBar title={m.settings_title()} meta={session.me?.username ?? undefined} />

<div class="settings" data-testid="settings-page">
  <nav
    class="index"
    aria-label={m.settings_index()}
    data-testid="settings-index"
    data-tour="settings"
  >
    <ol>
      {#each sections as section, index (section.anchor)}
        <li>
          <a
            href="#{section.anchor}"
            aria-current={marked === section.anchor ? 'location' : undefined}
            data-testid="settings-index-{section.anchor}"
          >
            <span class="num" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
            <span class="name">{section.label()}</span>
          </a>
        </li>
      {/each}
    </ol>
  </nav>

  <div class="sections">
    {#each sections as section, index (section.anchor)}
      {@const View = VIEWS[section.id]}
      <section
        id={section.anchor}
        class="section"
        aria-labelledby="{section.anchor}-title"
        data-testid="settings-section-{section.anchor}"
      >
        <h2 class="title" id="{section.anchor}-title">
          <span class="num" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
          <span>{section.label()}</span>
        </h2>
        <View />
      </section>
    {/each}
  </div>
</div>

<style>
  .num {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    font-variant-numeric: tabular-nums slashed-zero;
    letter-spacing: 0;
  }

  /* Phones: the index is a strip under the pinned top bar that scrolls sideways on its own. */
  .index {
    position: sticky;
    top: var(--md-sticky-top);
    z-index: 5;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .index ol {
    display: flex;
    margin: 0;
    padding: 0 var(--md-gutter);
    overflow-x: auto;
    list-style: none;
    scrollbar-width: none;
  }

  .index a {
    position: relative;
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-decoration: none;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .index a[aria-current='location'] {
    color: var(--md-color-ink);
  }

  .index a[aria-current='location']::after {
    content: '';
    position: absolute;
    inset-inline: var(--md-space-3);
    bottom: 0;
    height: 3px;
    background: var(--md-color-ink);
  }

  .index a:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .section {
    scroll-margin-top: calc(var(--md-target) + var(--md-space-2));
    padding-bottom: var(--md-space-8);
    border-bottom: var(--md-hairline) solid var(--md-color-line-strong);
  }

  .section:last-child {
    border-bottom: 0;
  }

  .title {
    display: flex;
    align-items: baseline;
    gap: var(--md-space-3);
    margin: 0;
    padding: var(--md-space-6) var(--md-gutter) var(--md-space-2);
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-semibold);
    line-height: var(--md-line-28);
  }

  /* Desktop: a numbered index column on the left that stays in view, the sections beside it. */
  @media (min-width: 1024px) {
    .settings {
      display: grid;
      grid-template-columns: 224px minmax(0, 880px);
      align-items: start;
    }

    .index {
      border-bottom: 0;
      border-inline-end: var(--md-hairline) solid var(--md-color-line);
      background: none;
    }

    .index ol {
      display: block;
      padding: var(--md-space-4) 0;
    }

    .index a {
      padding: 0 var(--md-space-4) 0 var(--md-gutter);
      white-space: normal;
    }

    .index a[aria-current='location']::after {
      inset: 20% auto 20% 0;
      width: 3px;
      height: auto;
    }

    .section {
      scroll-margin-top: 0;
    }
  }
</style>
