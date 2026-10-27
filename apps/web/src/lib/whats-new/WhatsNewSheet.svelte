<!--
  The sheet after an update: "Was ist neu in vX.Y.Z" with the changes since the version this
  device last saw (at most a handful; the rest is in Settings → Neuigkeiten). Non-modal like the
  other sheets, so the page below stays usable; closing it, "Verstanden" and following a link all
  count as seen (`whatsNew.dismiss`).
-->
<script lang="ts">
  import { Sheet } from '@mowdeck/ui';

  import { m } from '#lib/paraglide/messages.js';

  import { limitEntries } from './model.js';
  import { whatsNew } from './whats-new.svelte.js';
  import WhatsNewList from './WhatsNewList.svelte';

  /** Entries shown in the sheet; the Settings section has the rest. */
  const LIMIT = 6;

  const shown = $derived(limitEntries(whatsNew.unseen, LIMIT));
  const title = $derived(
    whatsNew.current
      ? m.whatsnew_title_version({ version: `v${whatsNew.current}` })
      : m.whatsnew_title(),
  );
</script>

<Sheet
  open={whatsNew.open}
  {title}
  closeLabel={m.whatsnew_close()}
  onclose={() => whatsNew.dismiss()}
  testid="whats-new-sheet"
>
  <WhatsNewList releases={shown.releases} onfollow={() => whatsNew.dismiss()} />
  {#snippet footer()}
    <div class="actions">
      <a class="all" href="/settings#whats-new" onclick={() => whatsNew.dismiss()}>
        {shown.hidden > 0 ? m.whatsnew_more({ count: shown.hidden }) : m.whatsnew_all()}
      </a>
      <button type="button" class="done" onclick={() => whatsNew.dismiss()}>
        {m.whatsnew_done()}
      </button>
    </div>
  {/snippet}
</Sheet>

<style>
  .actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-3);
  }

  .all {
    display: inline-flex;
    align-items: center;
    min-height: var(--md-target);
    color: inherit;
    font-size: var(--md-font-size-14);
    text-decoration-color: var(--md-color-line-strong);
    text-underline-offset: 3px;
  }

  .all:focus-visible,
  .done:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .done {
    min-height: var(--md-target);
    padding: 0 var(--md-space-4);
    border: var(--md-border-control) solid var(--md-color-ink);
    border-radius: var(--md-radius-2);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
    cursor: pointer;
  }
</style>
