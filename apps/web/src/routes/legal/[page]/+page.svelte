<!--
  A legal page of the installation (imprint, terms, privacy): the operator's Markdown from
  `/legal/<page>.md` (static/legal, replaceable with a volume), rendered with the tiny safe subset
  of `#lib/legal/markdown.js` as plain text nodes (no HTML from the file is ever interpreted). The
  shipped files are placeholders, so the page always says so.
-->
<script lang="ts">
  import { ProblemNotice } from '@mowdeck/ui';

  import { page } from '$app/state';

  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import {
    isLegalPage,
    parseMarkdown,
    type Block,
    type Inline,
    type LegalPage,
  } from '#lib/legal/markdown.js';
  import { m } from '#lib/paraglide/messages.js';

  const key = $derived(page.params.page ?? '');
  const known = $derived(isLegalPage(key));

  let blocks = $state<Block[] | null>(null);
  let failed = $state(false);

  function title(which: LegalPage): string {
    switch (which) {
      case 'imprint':
        return m.legal_imprint();
      case 'terms':
        return m.legal_terms();
      case 'privacy':
        return m.legal_privacy();
    }
  }

  async function load(which: LegalPage): Promise<void> {
    blocks = null;
    failed = false;
    try {
      const response = await fetch(`/legal/${which}.md`, {
        cache: 'no-cache',
        credentials: 'omit',
      });
      const type = response.headers.get('content-type') ?? '';
      // A missing file in `vite dev` answers with the SPA fallback (HTML).
      if (!response.ok || type.includes('html')) throw new Error(`HTTP ${response.status}`);
      blocks = parseMarkdown(await response.text());
    } catch {
      failed = true;
    }
  }

  $effect(() => {
    if (isLegalPage(key)) void load(key);
  });
</script>

<svelte:head
  ><title>{isLegalPage(key) ? title(key) : m.legal_nav()} · {m.app_name()}</title></svelte:head
>

{#snippet inline(content: Inline[])}
  {#each content as part, index (index)}
    {#if part.kind === 'strong'}<strong>{part.text}</strong>{:else if part.kind === 'link'}<a
        href={part.href}>{part.text}</a
      >{:else}{part.text}{/if}
  {/each}
{/snippet}

<main class="legal" data-testid="legal-page" data-page={key}>
  <nav class="top">
    <a href="/login">{m.legal_back()}</a>
  </nav>

  {#if !known}
    <h1>{m.legal_nav()}</h1>
    <p data-testid="legal-not-found">{m.legal_not_found()}</p>
  {:else}
    <p class="banner" role="note" data-testid="legal-placeholder">{m.legal_placeholder()}</p>
    {#if failed}
      <ProblemNotice title={m.legal_unavailable()} />
    {:else if blocks === null}
      <p class="muted" role="status"><SlowLoading /></p>
    {:else}
      <article data-testid="legal-content">
        {#each blocks as block, index (index)}
          {#if block.kind === 'heading'}
            <svelte:element this={`h${block.level}`}>{@render inline(block.content)}</svelte:element
            >
          {:else if block.kind === 'paragraph'}
            <p>{@render inline(block.content)}</p>
          {:else if block.ordered}
            <ol>
              {#each block.items as item, itemIndex (itemIndex)}<li>
                  {@render inline(item)}
                </li>{/each}
            </ol>
          {:else}
            <ul>
              {#each block.items as item, itemIndex (itemIndex)}<li>
                  {@render inline(item)}
                </li>{/each}
            </ul>
          {/if}
        {/each}
      </article>
    {/if}
  {/if}
</main>

<style>
  .legal {
    max-width: 760px;
    margin: 0 auto;
    padding: var(--md-space-6) var(--md-gutter) var(--md-space-12);
    line-height: var(--md-line-24);
  }

  .top {
    margin-bottom: var(--md-space-4);
  }

  .top a {
    display: inline-flex;
    align-items: center;
    min-height: var(--md-target);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .banner {
    margin: 0 0 var(--md-space-6);
    padding: var(--md-space-3);
    border: var(--md-hairline) dashed var(--md-color-line-strong);
    border-inline-start: 4px solid var(--md-state-paused);
    background: var(--md-state-paused-bg);
    font-weight: var(--md-weight-semibold);
  }

  .muted {
    color: var(--md-color-ink-muted);
  }

  article :global(h1) {
    font-size: var(--md-font-size-24);
  }

  article :global(h2) {
    margin-top: var(--md-space-6);
    font-size: var(--md-font-size-20);
  }

  article :global(h3),
  article :global(h4) {
    margin-top: var(--md-space-4);
    font-size: var(--md-font-size-16);
  }
</style>
