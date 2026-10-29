<!--
  The error codes of a mower's vendor (ADR 0067), read-only: a ledger of the lexicon Telemetry
  serves for the mower's connection, searchable by code or text, filtered by category and
  severity, a page at a time. A row opens to the full explanation (ErrorExplainer). Codes this
  mower has reported carry their count, the current error is pinned on top and marked in the
  list, unconfirmed entries are marked. No vendor knowledge here: the server picks the lexicon
  and spells the codes.
-->
<script lang="ts">
  import { Button, Field, Select, Switch } from '@mowdeck/ui';
  import { untrack } from 'svelte';

  import { asProblem } from '#lib/api/problem.js';
  import ErrorExplainer from '#lib/components/ErrorExplainer.svelte';
  import { formatNumber } from '#lib/i18n.js';
  import {
    explainMowerCodes,
    severityLabel,
    severityTone,
    type ErrorExplanation,
    type ErrorSeverity,
  } from '#lib/mower/error-explain.js';
  import {
    NO_FILTERS,
    NO_MARKS,
    SEVERITIES,
    appendPage,
    browseLexicon,
    isFiltered,
    loadMarks,
    seenList,
    type ErrorCategory,
    type LexiconFilters,
    type LexiconPage,
    type MowerMarks,
  } from '#lib/mower/error-lexicon.js';
  import { errorCategoryLabel } from '#lib/mower/reception-labels.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';

  interface Props {
    mowerId: string;
    /** `MowerState.error_code` while the mower is in error, else null. */
    currentCode?: string | null;
    /** The mower's error counts by code (Telemetry health), null before they arrive. */
    errorCounts?: Record<string, number> | null;
  }
  let { mowerId, currentCode = null, errorCounts = null }: Props = $props();

  const locale = $derived(getLocale());

  let filters = $state<LexiconFilters>({ ...NO_FILTERS });
  /** The search text the list follows, a moment after typing stops. */
  let query = $state('');
  let page = $state.raw<LexiconPage | null>(null);
  let entries = $state.raw<ErrorExplanation[]>([]);
  let status = $state<'loading' | 'ready' | 'unavailable' | 'no-telemetry'>('loading');
  let more = $state(false);
  let marks = $state.raw<MowerMarks>(NO_MARKS);
  let current = $state.raw<ErrorExplanation | null>(null);
  let request = 0;

  $effect(() => {
    const text = filters.query;
    const timer = setTimeout(() => (query = text), 250);
    return () => clearTimeout(timer);
  });

  // What the mower has had: its counted codes and the current error, in the lexicon's spelling.
  const countsKey = $derived(JSON.stringify(errorCounts ?? {}));
  $effect(() => {
    const id = mowerId;
    const lang = locale;
    const counts = JSON.parse(countsKey) as Record<string, number>;
    const code = currentCode;
    let live = true;
    void loadMarks(id, counts, code, lang)
      .then((found) => {
        if (live) marks = found;
      })
      .catch(() => {
        if (live) marks = NO_MARKS;
      });
    if (code) {
      void explainMowerCodes(id, [code], lang)
        .then(([found]) => {
          if (live) current = found ?? null;
        })
        .catch(() => {
          if (live) current = null;
        });
    } else {
      current = null;
    }
    return () => {
      live = false;
    };
  });

  const seen = $derived(seenList(marks));
  const active = $derived<LexiconFilters>({ ...filters, query });

  $effect(() => {
    const id = mowerId;
    const lang = locale;
    const wanted = active;
    const codes = seen;
    const mine = ++request;
    if (wanted.onlySeen && codes.length === 0) {
      entries = [];
      untrack(() => {
        if (page) page = { ...page, entries: [], total: 0, offset: 0 };
      });
      status = 'ready';
      return;
    }
    // Keep the shown page while a filter loads; only the first load says "loading".
    untrack(() => {
      if (!page) status = 'loading';
    });
    void browseLexicon(id, wanted, codes, lang)
      .then((found) => {
        if (mine !== request) return;
        page = found;
        entries = found.entries ?? [];
        status = found.lexicon ? 'ready' : 'unavailable';
      })
      .catch((error: unknown) => {
        if (mine !== request) return;
        status = asProblem(error).status === 404 ? 'no-telemetry' : 'unavailable';
      });
  });

  async function loadMore(): Promise<void> {
    if (!page || more) return;
    more = true;
    const mine = request;
    try {
      const next = await browseLexicon(mowerId, active, seen, locale, entries.length);
      if (mine === request) {
        entries = appendPage(entries, next);
        page = { ...next, entries };
      }
    } catch {
      // the button stays; the next tap tries again
    } finally {
      more = false;
    }
  }

  const total = $derived(page?.total ?? 0);
  const lexicon = $derived(page?.lexicon ?? null);
  const categoryOptions = $derived.by(() => {
    const counts = page?.categories ?? {};
    const all = Object.values(counts).reduce((sum, n) => sum + (n ?? 0), 0);
    const options = (Object.keys(counts) as ErrorCategory[])
      .map((category) => ({
        value: category,
        label: `${errorCategoryLabel(category)} (${formatNumber(counts[category] ?? 0)})`,
      }))
      .sort((a, b) => a.label.localeCompare(b.label, locale));
    return [{ value: '', label: `${m.lexi_category_all()} (${formatNumber(all)})` }, ...options];
  });
  const severityCounts = $derived(page?.severities ?? {});

  function pickSeverity(value: ErrorSeverity | ''): void {
    filters.severity = filters.severity === value ? '' : value;
  }

  function reset(): void {
    filters = { ...NO_FILTERS };
    query = '';
  }
</script>

<div class="lexi" data-testid="error-lexicon" data-status={status}>
  {#if status === 'no-telemetry'}
    <p class="hint">{m.lexi_no_telemetry()}</p>
  {:else if status === 'unavailable' && !lexicon}
    <p class="hint" data-testid="lexicon-unavailable">
      {page ? m.lexi_no_lexicon() : m.lexi_unavailable()}
    </p>
  {:else}
    {#if lexicon}
      <p class="intro">
        {m.lexi_intro({ vendor: lexicon.vendor_name })}
        <span class="tally mono" data-testid="lexicon-summary"
          >{m.lexi_summary({
            entries: formatNumber(lexicon.entries),
            unverified: formatNumber(lexicon.unverified),
          })}</span
        >
      </p>
    {/if}

    {#if current?.known}
      <div class="pinned" data-testid="lexicon-current">
        <span class="flag">{m.lexi_current()}</span>
        <ErrorExplainer explanation={current} compact />
      </div>
    {/if}

    <div class="controls">
      <div class="search">
        <Field
          id="lexicon-search"
          type="search"
          label={m.lexi_search()}
          placeholder={m.lexi_search_placeholder()}
          maxlength={100}
          testid="lexicon-search"
          bind:value={filters.query}
        />
      </div>
      <div class="category">
        <Select
          id="lexicon-category"
          label={m.lexi_category()}
          options={categoryOptions}
          bind:value={filters.category}
        />
      </div>
      <div class="sevs" role="group" aria-label={m.lexi_severity()} data-testid="lexicon-severity">
        {#each SEVERITIES as severity (severity)}
          <button
            type="button"
            class="sev"
            data-tone={severityTone(severity)}
            data-value={severity}
            aria-pressed={filters.severity === severity}
            onclick={() => pickSeverity(severity)}
          >
            <span class="pip" aria-hidden="true"></span>
            {severityLabel(severity)}
            <span class="n mono">{formatNumber(severityCounts[severity] ?? 0)}</span>
          </button>
        {/each}
      </div>
      {#if seen.length > 0}
        <div class="mine">
          <Switch
            id="lexicon-only-seen"
            label={m.lexi_only_seen({ count: formatNumber(seen.length) })}
            states={{ on: m.switch_on(), off: m.switch_off() }}
            testid="lexicon-only-seen"
            bind:checked={filters.onlySeen}
          />
        </div>
      {/if}
    </div>

    {#if status === 'loading'}
      <p class="hint" role="status">…</p>
    {:else if entries.length === 0}
      <p class="hint" data-testid="lexicon-empty">{m.lexi_none()}</p>
    {:else}
      <ol class="ledger" data-testid="lexicon-list" aria-live="polite">
        {#each entries as entry (entry.code)}
          {@const count = marks.seen[entry.code] ?? 0}
          {@const isCurrent = marks.current === entry.code}
          <li
            class="row"
            data-tone={severityTone(entry.severity)}
            data-severity={entry.severity}
            data-code={entry.code}
            data-seen={count > 0 || isCurrent ? 'true' : 'false'}
            data-current={isCurrent ? 'true' : 'false'}
            data-testid="lexicon-entry"
          >
            <details>
              <summary>
                <span class="code mono">{entry.code}</span>
                <span class="title">{entry.title}</span>
                <span class="marks">
                  <span class="cat">{errorCategoryLabel(entry.category)}</span>
                  {#if isCurrent}<span class="badge now">{m.lexi_current()}</span>{/if}
                  {#if count > 0}
                    <span class="badge seen" data-testid="lexicon-seen"
                      >{m.lexi_seen({ count: formatNumber(count) })}</span
                    >
                  {/if}
                  {#if !entry.verified}
                    <span class="badge unverified" data-testid="lexicon-unverified"
                      >{m.lexi_unverified()}</span
                    >
                  {/if}
                </span>
              </summary>
              <div class="body">
                <ErrorExplainer explanation={entry} />
              </div>
            </details>
          </li>
        {/each}
      </ol>
    {/if}

    <div class="foot">
      <span class="tally mono" data-testid="lexicon-count"
        >{m.lexi_count({ shown: formatNumber(entries.length), total: formatNumber(total) })}</span
      >
      <span class="actions">
        {#if isFiltered(filters)}
          <Button variant="ghost" onclick={reset} testid="lexicon-reset">{m.lexi_reset()}</Button>
        {/if}
        {#if entries.length < total}
          <Button pending={more} onclick={() => void loadMore()} testid="lexicon-more"
            >{m.lexi_more()}</Button
          >
        {/if}
      </span>
    </div>
  {/if}
</div>

<style>
  .lexi {
    display: grid;
    gap: var(--md-space-4);
    min-width: 0;
  }

  .hint,
  .intro {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .intro {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-3);
    align-items: baseline;
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .tally {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .pinned {
    display: grid;
    gap: var(--md-space-1);
  }

  .flag,
  .cat,
  .badge {
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .flag {
    color: var(--md-state-error);
  }

  .controls {
    display: grid;
    gap: var(--md-space-3);
    min-width: 0;
  }

  /* Severity as a row of square toggles, each with its count: the mildest left. */
  .sevs {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .sev {
    --tone: var(--md-state-unknown);

    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-height: 40px;
    padding: 0 var(--md-space-3);
    border: 1px solid var(--md-color-line-strong);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font: inherit;
    font-size: var(--md-font-size-13);
    cursor: pointer;
  }

  .sev[data-tone='error'] {
    --tone: var(--md-state-error);
  }

  .sev[data-tone='paused'] {
    --tone: var(--md-state-paused);
  }

  .sev[aria-pressed='true'] {
    border-color: var(--tone);
    box-shadow: inset 0 -3px 0 var(--tone);
    font-weight: var(--md-weight-semibold);
  }

  .sev:focus-visible {
    outline: 2px solid var(--md-color-focus, var(--md-color-ink));
    outline-offset: 2px;
  }

  .pip {
    width: 8px;
    height: 8px;
    background: var(--tone);
  }

  .n {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  /* The ledger: one ruled line per code, the severity as a tick on the left edge. */
  .ledger {
    margin: 0;
    padding: 0;
    list-style: none;
    border-top: 1px solid var(--md-color-line);
  }

  .row {
    --tone: var(--md-state-unknown);

    position: relative;
    border-bottom: 1px solid var(--md-color-line);
  }

  .row[data-tone='error'] {
    --tone: var(--md-state-error);
  }

  .row[data-tone='paused'] {
    --tone: var(--md-state-paused);
  }

  .row::before {
    content: '';
    position: absolute;
    inset: 0 auto 0 0;
    width: 3px;
    background: var(--tone);
    opacity: 0.35;
  }

  .row[data-seen='true']::before {
    opacity: 1;
  }

  .row[data-current='true'] {
    background: var(--md-state-error-bg);
  }

  .row[data-current='true']::after {
    content: '';
    position: absolute;
    inset: 0 0 auto;
    height: 3px;
    background: repeating-linear-gradient(
      -45deg,
      var(--md-state-error) 0 6px,
      transparent 6px 12px
    );
  }

  summary {
    display: grid;
    grid-template-columns: minmax(4.5ch, auto) minmax(0, 1fr);
    gap: 2px var(--md-space-3);
    align-items: baseline;
    min-height: 48px;
    padding: var(--md-space-2) var(--md-space-3) var(--md-space-2) calc(var(--md-space-3) + 3px);
    cursor: pointer;
    list-style: none;
  }

  summary::-webkit-details-marker {
    display: none;
  }

  summary:focus-visible {
    outline: 2px solid var(--md-color-focus, var(--md-color-ink));
    outline-offset: -2px;
  }

  .code {
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
    color: var(--md-color-ink);
    overflow-wrap: anywhere;
  }

  .title {
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
    overflow-wrap: anywhere;
  }

  .marks {
    grid-column: 2;
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-2);
    align-items: center;
  }

  .cat {
    color: var(--md-color-ink-muted);
  }

  .badge {
    padding: 0 6px;
    border: 1px solid currentcolor;
  }

  .badge.now {
    color: var(--md-state-error);
  }

  .badge.seen {
    border-color: var(--md-color-ink);
    background: var(--md-color-ink);
    color: var(--md-color-surface);
  }

  .badge.unverified {
    color: var(--md-color-ink-muted);
    border-style: dashed;
  }

  details[open] summary .title {
    font-weight: var(--md-weight-semibold);
  }

  .body {
    padding: 0 var(--md-space-3) var(--md-space-3) calc(var(--md-space-3) + 3px);
  }

  .foot {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .actions {
    display: flex;
    gap: var(--md-space-2);
  }

  @media (min-width: 768px) {
    .controls {
      grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
    }

    .sevs,
    .mine {
      grid-column: 1 / -1;
    }

    summary {
      grid-template-columns: 9ch minmax(0, 1fr) auto;
    }

    .marks {
      grid-column: 3;
      justify-content: flex-end;
    }
  }
</style>
