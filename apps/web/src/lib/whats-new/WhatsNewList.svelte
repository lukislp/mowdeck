<!--
  The notes of "Was ist neu", grouped by release: a stamped version with its date, then one row
  per change with a kind label (Neu / Behoben), the text in the user's language and, where the
  note has one, a "Jetzt ausprobieren" link into the app. Used by the sheet after an update and by
  the Settings section.
-->
<script lang="ts">
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';

  import { entryText, type WhatsNewRelease } from './model.js';

  interface Props {
    releases: readonly WhatsNewRelease[];
    /** Called when a "Jetzt ausprobieren" link is followed. */
    onfollow?: () => void;
  }
  let { releases, onfollow }: Props = $props();

  const locale = getLocale();

  function day(date: string): string {
    if (!date) return '';
    return new Intl.DateTimeFormat(locale, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      timeZone: 'UTC',
    }).format(new Date(`${date}T00:00:00Z`));
  }
</script>

<div class="releases" data-testid="whats-new-list">
  {#each releases as release (release.version)}
    <section class="release" aria-labelledby="wn-{release.version}">
      <h3 id="wn-{release.version}">
        <span class="stamp">{m.whatsnew_version({ version: release.version })}</span>
        {#if release.date}<time class="date" datetime={release.date}>{day(release.date)}</time>{/if}
      </h3>
      <ul>
        {#each release.entries as entry (entry.id)}
          <li data-testid="whats-new-entry">
            <span class="kind" class:fix={entry.kind === 'fix'}>
              {entry.kind === 'fix' ? m.whatsnew_kind_fix() : m.whatsnew_kind_feature()}
            </span>
            <span class="text">{entryText(entry, locale)}</span>
            {#if entry.href}
              <a class="try" href={entry.href} onclick={() => onfollow?.()}>
                {m.whatsnew_try()}<span class="arrow" aria-hidden="true">→</span>
              </a>
            {/if}
          </li>
        {/each}
      </ul>
    </section>
  {/each}
</div>

<style>
  .release + .release {
    margin-top: var(--md-space-4);
  }

  h3 {
    display: flex;
    align-items: baseline;
    gap: var(--md-space-3);
    margin: 0 0 var(--md-space-1);
    font-size: var(--md-font-size-14);
  }

  .stamp {
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums slashed-zero;
    line-height: var(--md-line-20);
  }

  .date {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-regular, 400);
  }

  ul {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  li {
    display: grid;
    grid-template-columns: 64px minmax(0, 1fr);
    column-gap: var(--md-space-3);
    padding: var(--md-space-2) 0;
    border-top: var(--md-hairline) solid var(--md-color-line);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .kind {
    padding-top: 2px;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .kind.fix {
    color: var(--md-color-ink-muted);
  }

  .text {
    overflow-wrap: anywhere;
  }

  .try {
    grid-column: 2;
    display: inline-flex;
    align-items: center;
    justify-self: start;
    min-height: var(--md-target);
    color: inherit;
    font-weight: var(--md-weight-semibold);
    text-decoration-color: var(--md-color-line-strong);
    text-underline-offset: 3px;
  }

  .try:hover {
    text-decoration-color: currentcolor;
  }

  .try:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .arrow {
    margin-inline-start: var(--md-space-1);
    font-family: var(--md-font-mono);
  }
</style>
