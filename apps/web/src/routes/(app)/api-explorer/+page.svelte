<!--
  The interactive API explorer (ADR 0072): Scalar in a same-origin frame (/_explorer/, pinned
  self-hosted bundle with its own CSP), fed with the services' OpenAPI documents and the access
  token of this session (#lib/api-explorer/bridge.ts). "Try it" calls go straight to /api/v1 on
  this origin. Only where /config.json turns it on (+page.ts); in the demo the note says what the
  calls reach.
-->
<script lang="ts">
  import { Icon, TopBar } from '@mowdeck/ui';

  import { connectFrame, frameUrl } from '#lib/api-explorer/bridge.js';
  import { session } from '#lib/auth/session.svelte.js';
  import { m } from '#lib/paraglide/messages.js';
  import { API_REFERENCE_URL, SCRIPTING_GUIDE_URL } from '#lib/settings/personal-tokens.js';

  let { data }: { data: { resetHours: number | null } } = $props();

  let frame = $state<HTMLIFrameElement | null>(null);

  function prefersDark(): boolean {
    const choice = document.documentElement.dataset.theme;
    if (choice === 'dark' || choice === 'light') return choice === 'dark';
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  const src = frameUrl(prefersDark());

  $effect(() => {
    if (!frame) return;
    return connectFrame(frame, session);
  });
</script>

<svelte:head><title>{m.api_explorer_title()} · {m.app_name()}</title></svelte:head>

<TopBar title={m.api_explorer_title()} meta={m.api_explorer_meta()} />

<div class="explorer">
  <section class="brief" data-testid="api-explorer-note">
    <p class="note" class:demo={data.resetHours !== null}>
      {#if data.resetHours !== null}
        <span class="tag">{m.demo_badge()}</span>
        {m.api_explorer_demo_note({ hours: data.resetHours })}
      {:else}
        {m.api_explorer_live_note()}
      {/if}
    </p>
    <p class="scripts">
      <span>{m.api_explorer_scripts()}</span>
      <a href={SCRIPTING_GUIDE_URL} rel="external noopener" target="_blank">
        <Icon name="route" size={14} />{m.pat_docs_guide()}
      </a>
      <a href={API_REFERENCE_URL} rel="external noopener" target="_blank">
        <Icon name="list" size={14} />{m.pat_docs_reference()}
      </a>
    </p>
  </section>

  <iframe
    bind:this={frame}
    class="frame"
    title={m.api_explorer_frame()}
    {src}
    referrerpolicy="same-origin"
    data-testid="api-explorer-frame"
  ></iframe>
</div>

<style>
  .explorer {
    display: grid;
    gap: var(--md-space-3);
    padding: var(--md-space-3) var(--md-space-4) var(--md-space-4);
  }

  .brief {
    display: grid;
    gap: var(--md-space-2);
  }

  .note {
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 3px solid var(--md-color-ink-muted);
    background: var(--md-color-surface);
    font-size: var(--md-font-size-14);
  }

  .note.demo {
    border-inline-start-color: var(--md-hud-lit);
  }

  .tag {
    margin-inline-end: var(--md-space-2);
    padding: 0 var(--md-space-2);
    background: var(--md-hud-lit);
    color: var(--md-hud-ink-inverse);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: 0.2em;
    text-transform: uppercase;
  }

  .scripts {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-3);
    align-items: center;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .scripts a {
    display: inline-flex;
    gap: var(--md-space-1);
    align-items: center;
    color: var(--md-color-ink);
  }

  .frame {
    width: 100%;
    height: max(480px, calc(var(--md-viewport-height) - 220px));
    border: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-bg);
  }
</style>
