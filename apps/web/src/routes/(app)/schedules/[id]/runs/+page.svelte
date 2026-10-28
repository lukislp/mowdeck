<script lang="ts">
  import { Button, Icon, ProblemNotice, TopBar } from '@mowdeck/ui';

  import { page } from '$app/state';

  import { schedulerApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import ToneBadge from '#lib/components/ToneBadge.svelte';
  import { formatNumber, problemMessage } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import { followupLabel, outcomeLook } from '#lib/schedules/labels.js';
  import { formatFiring, recurrenceSummary } from '#lib/schedules/recurrence.js';
  import { angleText } from '#lib/schedules/rotation.js';
  import { loadTargets, type Targets } from '#lib/schedules/runtime.js';
  import type { Locale, Run, Schedule } from '#lib/schedules/types.js';

  const PAGE = 50;
  const id = $derived(page.params.id ?? '');

  let schedule = $state<Schedule | null>(null);
  let runs = $state<Run[] | null>(null);
  let next = $state<string | null>(null);
  let targets = $state<Targets>({ mowers: [], groups: [], sites: [] });
  let problem = $state<ApiProblem | null>(null);
  let more = $state(false);

  const locale = $derived(getLocale() as Locale);

  $effect(() => {
    const current = id;
    void (async () => {
      problem = null;
      try {
        const [found, list] = await Promise.all([
          unwrap(
            schedulerApi.GET('/api/v1/scheduler/schedules/{schedule_id}', {
              params: { path: { schedule_id: current } },
            }),
          ),
          unwrap(
            schedulerApi.GET('/api/v1/scheduler/schedules/{schedule_id}/runs', {
              params: { path: { schedule_id: current }, query: { limit: PAGE } },
            }),
          ),
        ]);
        schedule = found;
        runs = list.items;
        next = list.next_before ?? null;
      } catch (error) {
        problem = asProblem(error);
      }
      void loadTargets()
        .then((found) => (targets = found))
        .catch(() => undefined);
    })();
  });

  async function loadMore(): Promise<void> {
    if (!next) return;
    more = true;
    try {
      const list = await unwrap(
        schedulerApi.GET('/api/v1/scheduler/schedules/{schedule_id}/runs', {
          params: { path: { schedule_id: id }, query: { limit: PAGE, before: next } },
        }),
      );
      runs = [...(runs ?? []), ...list.items];
      next = list.next_before ?? null;
    } catch (error) {
      problem = asProblem(error);
    } finally {
      more = false;
    }
  }

  function mowerName(mowerId: string | null): string {
    return targets.mowers.find((x) => x.id === mowerId)?.name ?? m.notif_mower();
  }

  const zone = $derived(schedule?.timezone ?? null);
</script>

<svelte:head><title>{m.sched_runs_title()} · {m.app_name()}</title></svelte:head>

<TopBar
  title={m.sched_runs_title()}
  meta={schedule ? `${schedule.name} · ${recurrenceSummary(schedule, locale)}` : undefined}
  backHref="/schedules"
  backLabel={m.back()}
/>

{#if problem && !runs}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      detail={problem.detail}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !runs}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else if runs.length === 0}
  <div class="empty" data-testid="runs-empty">
    <Icon name="list" size={32} />
    <h2>{m.sched_runs_empty_title()}</h2>
    <p>{m.sched_runs_empty_body()}</p>
  </div>
{:else}
  <div class="table" role="table" aria-label={m.sched_runs_title()} data-testid="runs">
    <div class="tr th" role="row">
      <span role="columnheader">{m.sched_col_planned()}</span>
      <span role="columnheader">{m.sched_col_mower()}</span>
      <span role="columnheader">{m.sched_col_outcome()}</span>
      <span role="columnheader">{m.sched_col_details()}</span>
      <span role="columnheader">{m.sched_col_command()}</span>
    </div>
    {#each runs as run (run.id)}
      <div class="tr" role="row" data-testid="run-row" data-outcome={run.outcome}>
        <span class="when" role="cell">
          <span class="mono strong">{formatFiring(run.scheduled_for, zone, locale)}</span>
          <span class="trigger">
            {run.trigger === 'manual' ? m.sched_trigger_manual() : m.sched_trigger_schedule()}
          </span>
        </span>
        <span class="mower" role="cell">
          <Icon name="mower" size={16} />{mowerName(run.mower_id)}
        </span>
        <span role="cell" class="outcome">
          <ToneBadge {...outcomeLook(run.outcome)} value={run.outcome} testid="run-outcome" />
          {#if followupLabel(run.followup)}
            <span class="followup">{followupLabel(run.followup)}</span>
          {/if}
        </span>
        <!-- The server's English detail only where the badge alone does not explain the run. -->
        <span class="details" role="cell"
          >{#if run.outcome === 'failed'}{run.details ?? ''}{:else if run.angle_deg != null}<span
              class="run-angle"
              data-testid="run-angle"
              ><span class="stripes" style:transform="rotate({-run.angle_deg}deg)"
              ></span>{m.sched_run_angle({ angle: angleText(run.angle_deg, formatNumber) })}</span
            >{/if}</span
        >
        <span role="cell" class="command">
          {#if run.command_id && run.mower_id}
            <a
              class="link mono"
              href="/mowers/{run.mower_id}#recent-commands"
              data-testid="run-command"
              title={m.sched_command_link()}
            >
              <Icon name="link" size={14} />{run.command_id.slice(0, 8)}
            </a>
          {:else}
            <span class="quiet">–</span>
          {/if}
        </span>
      </div>
    {/each}
  </div>
  {#if next}
    <div class="pad">
      <Button pending={more} onclick={loadMore} testid="runs-more">{m.sched_runs_more()}</Button>
    </div>
  {/if}
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .empty {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-2);
    max-width: 480px;
    padding: var(--md-space-8) var(--md-gutter);
    color: var(--md-color-ink-muted);
  }

  .empty h2 {
    margin: var(--md-space-2) 0 0;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-28);
  }

  .empty p {
    margin: 0;
  }

  .table {
    max-width: 1200px;
    padding-bottom: var(--md-space-4);
  }

  .tr {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--md-space-2) var(--md-space-3);
    padding: var(--md-space-3) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .th {
    display: none;
  }

  .when {
    grid-row: 1;
    grid-column: 1;
    display: flex;
    flex-direction: column;
    gap: var(--md-space-0_5);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .strong {
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-medium);
  }

  .trigger,
  .followup {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .outcome {
    grid-row: 1;
    grid-column: 2;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: var(--md-space-1);
  }

  .mower {
    display: inline-flex;
    grid-row: 2;
    grid-column: 1;
    align-items: center;
    gap: var(--md-space-1);
    font-size: var(--md-font-size-14);
  }

  .details {
    grid-row: 3;
    grid-column: 1 / -1;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .details:empty {
    display: none;
  }

  .run-angle {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  /* Three mown stripes at the run's angle. */
  .stripes {
    display: inline-block;
    width: 12px;
    height: 9px;
    background: repeating-linear-gradient(
      to bottom,
      var(--md-state-mowing) 0 1.5px,
      transparent 1.5px 3.75px
    );
  }

  .command {
    grid-column: 2;
    grid-row: 2;
    justify-self: end;
  }

  .link {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: var(--md-target);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-13);
  }

  .link:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  @media (min-width: 1024px) {
    .tr {
      grid-template-columns: 200px minmax(0, 1fr) 220px minmax(0, 1.5fr) 120px;
      align-items: center;
      gap: var(--md-space-4);
    }

    .th {
      display: grid;
      padding-block: var(--md-space-2);
      background: var(--md-color-bg);
      color: var(--md-color-ink-muted);
      font-size: var(--md-font-size-11);
      font-weight: var(--md-weight-semibold);
      letter-spacing: var(--md-label-tracking);
      text-transform: uppercase;
    }

    .when,
    .outcome,
    .mower,
    .details,
    .command {
      grid-column: auto;
      grid-row: auto;
    }

    .outcome {
      align-items: flex-start;
    }

    .details:empty {
      display: block;
    }

    .command {
      justify-self: start;
    }
  }
</style>
