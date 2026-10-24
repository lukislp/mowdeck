<!--
  Confirm and report a bulk command ("pause all", "all to dock"): the sheet lists the mowers it
  affects, sends one request per target (a site or a group; Fleet checks every mower on its
  own) and then shows each mower's outcome: accepted, not allowed, not published, or not sent
  (Fleet left it out). A refused target (403 on the site or group itself) marks its mowers as
  not allowed. The command is never sent without the confirm tap.
-->
<script lang="ts">
  import { Button, ProblemNotice, Sheet } from '@mowdeck/ui';

  import { fleetApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import ToneBadge from '#lib/components/ToneBadge.svelte';
  import {
    bulkCommand,
    lineStates,
    mergeAnswers,
    outcomeCounts,
    type BulkAnswer,
    type BulkKind,
    type BulkLineState,
    type BulkTarget,
  } from '#lib/fleet/bulk.js';
  import { bulkKindLabel, bulkLine } from '#lib/fleet/data.js';
  import { problemMessage } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Affected {
    id: string;
    name: string;
    site_id: string;
    group_ids: string[];
  }

  interface Props {
    kind: BulkKind | null;
    targets: BulkTarget[];
    mowers: Affected[];
    /** Where the command goes, e.g. "Standort Hof" or "alle sichtbaren Mäher". */
    scope: string;
    onclose: () => void;
  }
  let { kind, targets, mowers, scope, onclose }: Props = $props();

  let phase = $state<'confirm' | 'sending' | 'done'>('confirm');
  let states = $state<Record<string, BulkLineState>>({});
  let problem = $state<ApiProblem | null>(null);

  // A new command (or a reopened sheet) starts at the confirmation.
  $effect(() => {
    void kind;
    phase = 'confirm';
    states = {};
    problem = null;
  });

  const counts = $derived(outcomeCounts(states));

  function inTarget(mower: Affected, target: BulkTarget): boolean {
    return target.type === 'site'
      ? mower.site_id === target.id
      : mower.group_ids.includes(target.id);
  }

  async function send(): Promise<void> {
    if (!kind) return;
    phase = 'sending';
    problem = null;
    const command = bulkCommand(kind);
    const results = await Promise.allSettled(
      targets.map((target) =>
        target.type === 'site'
          ? unwrap(
              fleetApi.POST('/api/v1/fleet/sites/{site_id}/commands', {
                params: { path: { site_id: target.id } },
                body: { command },
              }),
            )
          : unwrap(
              fleetApi.POST('/api/v1/fleet/groups/{group_id}/commands', {
                params: { path: { group_id: target.id } },
                body: { command },
              }),
            ),
      ),
    );
    const answers: BulkAnswer[] = [];
    const refused = new Set<string>();
    let failed = false;
    results.forEach((result, index) => {
      if (result.status === 'fulfilled') {
        answers.push({ items: result.value.items ?? [] });
        return;
      }
      const found = asProblem(result.reason);
      const target = targets[index];
      if (found.status === 403 && target) {
        for (const mower of mowers) if (inTarget(mower, target)) refused.add(mower.id);
      } else {
        failed = true;
        problem = found;
      }
    });
    const next = lineStates(
      mowers.map((mower) => mower.id),
      mergeAnswers(answers),
      failed,
    );
    for (const id of refused) if (next[id] !== 'accepted') next[id] = 'forbidden';
    states = next;
    phase = 'done';
  }
</script>

<Sheet
  open={kind !== null}
  title={kind ? bulkKindLabel(kind) : ''}
  closeLabel={m.close()}
  {onclose}
  testid="bulk-sheet"
>
  {#if phase === 'confirm' || phase === 'sending'}
    <p class="lead">{m.bulk_confirm_body({ count: mowers.length, scope })}</p>
  {:else}
    <p class="lead" data-testid="bulk-summary" role="status">
      {m.bulk_summary({
        accepted: counts.accepted,
        refused: counts.forbidden,
        other: counts.not_published + counts.missing + counts.failed,
      })}
    </p>
  {/if}
  {#if mowers.length}
    <ul class="lines">
      {#each mowers as mower (mower.id)}
        {@const state = states[mower.id]}
        <li data-testid="bulk-line-{mower.id}" data-outcome={state ?? ''}>
          <span class="name">{mower.name}</span>
          {#if state}
            {@const look = bulkLine(state)}
            <ToneBadge tone={look.tone} glyph={look.glyph} label={look.label} value={state} />
          {/if}
        </li>
      {/each}
    </ul>
  {:else}
    <p class="hint">{m.bulk_none()}</p>
  {/if}
  {#if problem}
    <ProblemNotice
      title={problemMessage(problem)}
      detail={problem.detail}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  {/if}
  {#snippet footer()}
    {#if phase === 'done'}
      <Button full onclick={onclose} testid="bulk-done">{m.close()}</Button>
    {:else}
      <div class="actions">
        <Button variant="ghost" onclick={onclose}>{m.cancel()}</Button>
        <Button
          variant="primary"
          icon={kind === 'pause_mow' ? 'pause' : 'dock'}
          pending={phase === 'sending'}
          disabled={!mowers.length || !targets.length}
          testid="bulk-confirm"
          onclick={send}>{kind ? bulkKindLabel(kind) : ''}</Button
        >
      </div>
    {/if}
  {/snippet}
</Sheet>

<style>
  .lead {
    margin: 0 0 var(--md-space-3);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .hint {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .lines {
    margin: 0 0 var(--md-space-3);
    padding: 0;
    border-top: var(--md-hairline) solid var(--md-color-line);
    list-style: none;
  }

  .lines li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-1) var(--md-space-3);
    min-height: var(--md-target);
    padding: var(--md-space-1) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .name {
    font-weight: var(--md-weight-semibold);
  }

  .actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--md-space-2);
  }
</style>
