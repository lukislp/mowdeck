<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  The banner on every page of the app while the account is scheduled for deletion (ADR 0082,
  section 3): the date and "Cancel deletion". It has no close button; it goes when the deletion
  is cancelled. The state comes from `GET /me` (no request of its own).
-->
<script lang="ts">
  import { Button, Icon, ProblemNotice } from '@mowdeck/ui';

  import { problemMessage } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  import { accountDeletion, formatDeletionDate, type AccountDeletion } from './deletion.svelte.js';

  let { deletion = accountDeletion }: { deletion?: AccountDeletion } = $props();

  const view = $derived(deletion.state);

  async function cancel(): Promise<void> {
    if (await deletion.restore()) toasts.push('ok', m.deletion_cancelled());
  }
</script>

{#if view.kind === 'scheduled'}
  <section class="banner" aria-label={m.deletion_banner_label()} data-testid="deletion-banner">
    <p class="words" role="status">
      <Icon name="alert" size={16} />
      <span>
        {view.deleteAfter
          ? m.deletion_scheduled({ date: formatDeletionDate(view.deleteAfter, getLocale()) })
          : m.deletion_scheduled_unknown()}
      </span>
    </p>
    {#if view.canCancel}
      <Button
        variant="secondary"
        icon="back"
        pending={deletion.busy === 'restore'}
        testid="deletion-banner-cancel"
        onclick={cancel}>{m.deletion_cancel()}</Button
      >
    {/if}
    {#if deletion.problem && deletion.failed === 'restore'}
      <div class="problem">
        <ProblemNotice
          title={problemMessage(deletion.problem)}
          detail={deletion.problem.detail}
          reference={deletion.problem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      </div>
    {/if}
  </section>
{/if}

<style>
  .banner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2) var(--md-space-3);
    padding: var(--md-space-2) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-state-error);
    background: var(--md-state-error-bg);
    color: var(--md-color-ink);
  }

  .words {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-2);
    margin: 0;
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
    line-height: var(--md-line-20);
  }

  .words :global(svg) {
    flex: none;
    margin-top: 2px;
    color: var(--md-state-error);
  }

  .problem {
    flex-basis: 100%;
  }
</style>
