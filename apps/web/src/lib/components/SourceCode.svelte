<!--
  The build and where its source code is (AGPL-3.0 section 13, LICENSING.md). Two forms:

  * `line`: one quiet row under the sign-in and registration forms, so anyone who reaches the
    installation finds the source without an account: the version as a stamped mono tag, the
    licence and the link.
  * `sheet`: the About section of the settings: version, licence and the full address of the
    source of this exact build as labelled readouts, with one sentence on the right to it.

  Reads `/config.json` (the image's build values) unless `source` is given (tests).
-->
<script lang="ts">
  import { loadConfig } from '$lib/config';
  import {
    DEFAULT_SOURCE,
    LICENSE_NAME,
    LICENSE_SHORT,
    LICENSE_URL,
    displayUrl,
    sourceCodeUrl,
    versionLabel,
    type SourceInfo,
  } from '$lib/legal/source';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    variant?: 'line' | 'sheet';
    source?: SourceInfo;
  }
  let { variant = 'line', source }: Props = $props();

  let loaded = $state<SourceInfo>(DEFAULT_SOURCE);
  $effect(() => {
    if (source) return;
    void loadConfig().then((config) => (loaded = config.source));
  });

  const build = $derived(source ?? loaded);
  const href = $derived(sourceCodeUrl(build));
  const version = $derived(versionLabel(build));
</script>

{#if variant === 'line'}
  <nav class="line" aria-label={m.source_nav()} data-testid="source-code">
    <span class="stamp" data-testid="source-version">{version ?? 'dev'}</span>
    <span class="licence">{LICENSE_SHORT}</span>
    <a class="link" {href} target="_blank" rel="noopener noreferrer" data-testid="source-code-link">
      {m.source_code()}<span class="arrow" aria-hidden="true">↗</span><span class="sr"
        >({m.source_new_tab()})</span
      >
    </a>
  </nav>
{:else}
  <div class="sheet" data-testid="source-code">
    <p class="note">{m.source_about_text()}</p>
    <dl>
      <div class="row">
        <dt>{m.source_version()}</dt>
        <dd class="mono" data-testid="source-version">
          {#if version}{version}{:else}{m.source_dev_build()}{/if}
        </dd>
      </div>
      <div class="row">
        <dt>{m.source_licence()}</dt>
        <dd>
          <a class="mono" href={LICENSE_URL} target="_blank" rel="noopener noreferrer"
            >{LICENSE_NAME}<span class="arrow" aria-hidden="true">↗</span><span class="sr"
              >({m.source_new_tab()})</span
            ></a
          >
        </dd>
      </div>
      <div class="row">
        <dt>{m.source_code()}</dt>
        <dd>
          <a
            class="mono address"
            {href}
            target="_blank"
            rel="noopener noreferrer"
            data-testid="source-code-link"
            >{displayUrl(href)}<span class="arrow" aria-hidden="true">↗</span><span class="sr"
              >({m.source_new_tab()})</span
            ></a
          >
        </dd>
      </div>
    </dl>
  </div>
{/if}

<style>
  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .arrow {
    margin-inline-start: var(--md-space-1);
    font-family: var(--md-font-mono);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums slashed-zero;
  }

  a {
    color: inherit;
    text-decoration-color: var(--md-color-line-strong);
    text-underline-offset: 3px;
  }

  a:hover {
    text-decoration-color: currentcolor;
  }

  a:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  /* line: a hairline row like the legal links, the version stamped in a mono tag. */
  .line {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0 var(--md-space-3);
    padding-top: var(--md-space-2);
    border-top: var(--md-hairline) solid var(--md-color-line);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .stamp {
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums slashed-zero;
    line-height: var(--md-line-20);
  }

  .licence {
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
  }

  .line .link {
    display: inline-flex;
    align-items: center;
    min-height: var(--md-target);
    margin-inline-start: auto;
    font-size: var(--md-font-size-13);
  }

  /* sheet: labelled readouts on hairlines, values in mono. */
  .sheet {
    padding: 0 var(--md-gutter);
  }

  .note {
    max-width: 60ch;
    margin: 0 0 var(--md-space-4);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  dl {
    margin: 0;
  }

  .row {
    display: grid;
    grid-template-columns: 112px minmax(0, 1fr);
    gap: var(--md-space-3);
    align-items: baseline;
    min-height: var(--md-target);
    padding: var(--md-space-3) 0;
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  dd {
    margin: 0;
    font-size: var(--md-font-size-14);
  }

  .address {
    overflow-wrap: anywhere;
  }
</style>
