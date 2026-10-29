<!--
  A vendor error code in plain words (ADR 0065): severity and category, the vendor and its code,
  title, what happened, the likely cause and the steps as a numbered ladder. The words come from
  the vendor's error lexicon (Telemetry); this component only lays them out. A critical error
  (lifted, tilted) carries a hazard stripe; a code the lexicon does not know says so and shows
  the mower's own message next to the generic help; an unverified entry is marked.
-->
<script lang="ts">
  import { errorCategoryLabel } from '#lib/mower/reception-labels.js';
  import { severityLabel, severityTone, type ErrorExplanation } from '#lib/mower/error-explain.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    explanation: ErrorExplanation;
    /** The mower's own message (`MowerState.error_message`), shown for an unknown code. */
    vendorMessage?: string | null;
    /** Fewer words: no description and cause (lists, the health page). */
    compact?: boolean;
  }
  let { explanation, vendorMessage = null, compact = false }: Props = $props();

  const tone = $derived(severityTone(explanation.severity));
  const steps = $derived(explanation.steps ?? []);
  const links = $derived(explanation.links ?? []);
</script>

<section
  class="errx"
  class:compact
  data-tone={tone}
  data-severity={explanation.severity}
  data-category={explanation.category}
  data-known={explanation.known ? 'true' : 'false'}
  data-testid="error-explainer"
  aria-label={m.errx_label()}
  lang={explanation.locale}
>
  <header class="head">
    <span class="sev" data-testid="error-severity">
      <span class="pip" aria-hidden="true"></span>
      {severityLabel(explanation.severity)}
      <span class="sep" aria-hidden="true">/</span>
      <span class="cat">{errorCategoryLabel(explanation.category)}</span>
    </span>
    <span class="code mono" data-testid="error-code">
      {#if explanation.vendor_name}<span class="vendor">{explanation.vendor_name}</span>{/if}
      {explanation.code}
    </span>
  </header>

  <h3 class="title" data-testid="error-title">{explanation.title}</h3>

  {#if !compact && explanation.description}
    <p class="desc">{explanation.description}</p>
  {/if}

  {#if !compact && explanation.cause}
    <p class="cause"><span class="key">{m.errx_cause()}</span>{explanation.cause}</p>
  {/if}

  {#if steps.length > 0}
    <ol class="steps" aria-label={m.errx_steps()} data-testid="error-steps">
      {#each steps as step, i (i)}
        <li>
          <span class="n mono" aria-hidden="true">{i + 1}</span>
          <span class="text">{step}</span>
        </li>
      {/each}
    </ol>
  {/if}

  {#if !explanation.user_action}
    <p class="note calm">{m.errx_no_action()}</p>
  {/if}
  {#if !explanation.known}
    <p class="note" data-testid="error-unknown">{m.errx_unknown()}</p>
    {#if vendorMessage}
      <p class="note mono">{m.errx_vendor_message({ message: vendorMessage })}</p>
    {/if}
  {:else if !explanation.verified}
    <p class="note" data-testid="error-unverified">{m.errx_unverified()}</p>
  {/if}

  {#if links.length > 0}
    <p class="links">
      {#each links as link (link.url)}
        <a href={link.url} target="_blank" rel="noopener noreferrer">{link.label}</a>
      {/each}
    </p>
  {/if}
</section>

<style>
  .errx {
    --tone: var(--md-state-unknown);
    --tone-bg: var(--md-state-unknown-bg);

    position: relative;
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-3) var(--md-space-3) var(--md-space-3) calc(var(--md-space-3) + 2px);
    border-left: 4px solid var(--tone);
    background: var(--md-color-surface-sunken);
    color: var(--md-color-ink);
  }

  .errx[data-tone='error'] {
    --tone: var(--md-state-error);
    --tone-bg: var(--md-state-error-bg);
  }

  .errx[data-tone='paused'] {
    --tone: var(--md-state-paused);
    --tone-bg: var(--md-state-paused-bg);
  }

  /* Critical: a hazard stripe along the top edge, the one loud element of the card. */
  .errx[data-severity='critical']::before {
    content: '';
    position: absolute;
    inset: 0 0 auto;
    height: 4px;
    background: repeating-linear-gradient(-45deg, var(--tone) 0 6px, transparent 6px 12px);
  }

  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-1) var(--md-space-3);
  }

  .sev,
  .key {
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .sev {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--tone);
  }

  .pip {
    width: 8px;
    height: 8px;
    background: var(--tone);
  }

  .sep {
    color: var(--md-color-line-strong);
  }

  .cat {
    color: var(--md-color-ink-muted);
  }

  .mono {
    font-family: var(--md-font-mono);
  }

  .code {
    display: inline-flex;
    gap: 6px;
    padding: 1px 6px;
    border: 1px solid var(--md-color-line);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    color: var(--md-color-ink);
    font-variant-numeric: tabular-nums;
  }

  .vendor {
    color: var(--md-color-ink-muted);
    text-transform: uppercase;
    letter-spacing: var(--md-label-tracking);
  }

  .title {
    margin: 0;
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-24);
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  .desc,
  .cause,
  .note,
  .links {
    margin: 0;
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .desc {
    color: var(--md-color-ink);
  }

  .cause {
    display: grid;
    gap: 2px;
    color: var(--md-color-ink);
  }

  .key {
    color: var(--md-color-ink-muted);
  }

  /* The steps as a ladder: square numerals on a rail in the tone of the error. */
  .steps {
    display: grid;
    gap: var(--md-space-2);
    margin: var(--md-space-1) 0 0;
    padding: 0;
    list-style: none;
  }

  .steps li {
    position: relative;
    display: grid;
    grid-template-columns: 22px minmax(0, 1fr);
    gap: var(--md-space-2);
    align-items: start;
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .steps li:not(:last-child)::after {
    content: '';
    position: absolute;
    top: 22px;
    bottom: calc(-1 * var(--md-space-2));
    left: 10px;
    width: 2px;
    background: var(--md-color-line);
  }

  .n {
    display: grid;
    place-items: center;
    width: 22px;
    height: 22px;
    border: 2px solid var(--tone);
    background: var(--md-color-surface);
    color: var(--tone);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
  }

  .text {
    padding-top: 1px;
    overflow-wrap: anywhere;
  }

  .note {
    color: var(--md-color-ink-muted);
  }

  .note.calm {
    padding-left: var(--md-space-2);
    border-left: 2px solid var(--md-state-mowing);
  }

  .links {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-3);
  }

  .compact {
    gap: var(--md-space-1);
    padding-block: var(--md-space-2);
  }

  .compact .title {
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }
</style>
