<script lang="ts">
  import { Button, Icon, ProblemNotice, Sheet, Switch, TopBar } from '@mowdeck/ui';

  import { schedulerApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { clock } from '#lib/clock.svelte.js';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import ToneBadge from '#lib/components/ToneBadge.svelte';
  import { formatAge, problemMessage } from '#lib/i18n.js';
  import FirstTimeHint from '#lib/onboarding/FirstTimeHint.svelte';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import { outcomeLook, verdictLook } from '#lib/schedules/labels.js';
  import { parsePreview } from '#lib/schedules/preview.js';
  import { mowSummary } from '#lib/schedules/mow-options.js';
  import { formatCountdown, formatFiring, recurrenceSummary } from '#lib/schedules/recurrence.js';
  import {
    loadLastRun,
    loadPreview,
    loadTargets,
    rightsOf,
    targetRights,
    type Rights,
    type Targets,
  } from '#lib/schedules/runtime.js';
  import type { Locale, Preview, Run, Schedule } from '#lib/schedules/types.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  let schedules = $state<Schedule[] | null>(null);
  let loadProblem = $state<ApiProblem | null>(null);
  let targets = $state<Targets>({ mowers: [], groups: [], sites: [] });
  let previews = $state<Record<string, Preview | null>>({});
  let lastRuns = $state<Record<string, Run | null>>({});
  let rights = $state<Record<string, Rights>>({});
  let canCreate = $state(false);
  let busy = $state<string | null>(null);

  // Run now: the confirm sheet and its result.
  let runTarget = $state<Schedule | null>(null);
  let respectRain = $state(true);
  let runBusy = $state(false);
  let runResult = $state<Run[] | null>(null);
  let runProblem = $state<ApiProblem | null>(null);

  const locale = $derived(getLocale() as Locale);
  const switchStates = $derived({ on: m.switch_on(), off: m.switch_off() });

  $effect(() => clock.hold(30_000));

  $effect(() => {
    void load();
  });

  async function load(): Promise<void> {
    loadProblem = null;
    try {
      const [list, found] = await Promise.all([
        unwrap(schedulerApi.GET('/api/v1/scheduler/schedules')),
        loadTargets(),
      ]);
      targets = found;
      schedules = list.items;
    } catch (error) {
      loadProblem = asProblem(error);
      return;
    }
    const all = schedules ?? [];
    const keys = new Map<string, { type: 'mower' | 'group'; id: string }>();
    for (const s of all) {
      const type = s.target_type === 'group' ? 'group' : 'mower';
      keys.set(`${type}:${s.target_id}`, { type, id: s.target_id });
    }
    // Create needs both rights on at least one target the user can see.
    for (const mower of targets.mowers)
      keys.set(`mower:${mower.id}`, { type: 'mower', id: mower.id });
    for (const group of targets.groups)
      keys.set(`group:${group.id}`, { type: 'group', id: group.id });
    void targetRights([...keys.values()]).then((found) => {
      rights = found;
      canCreate = Object.entries(found).some(
        ([key, r]) => r.edit && (key.startsWith('mower:') || key.startsWith('group:')),
      );
    });
    for (const s of all) void refreshRow(s.id);
  }

  async function refreshRow(id: string): Promise<void> {
    const [preview, last] = await Promise.allSettled([loadPreview(id, 3), loadLastRun(id)]);
    previews[id] = preview.status === 'fulfilled' ? preview.value : null;
    lastRuns[id] = last.status === 'fulfilled' ? last.value : null;
  }

  function replace(updated: Schedule): void {
    schedules = (schedules ?? []).map((s) => (s.id === updated.id ? updated : s));
  }

  function targetName(schedule: Schedule): string {
    const list = schedule.target_type === 'group' ? targets.groups : targets.mowers;
    return list.find((t) => t.id === schedule.target_id)?.name ?? m.sched_target_removed();
  }

  async function setEnabled(schedule: Schedule, enabled: boolean): Promise<void> {
    busy = schedule.id;
    try {
      replace(
        await unwrap(
          schedulerApi.PATCH('/api/v1/scheduler/schedules/{schedule_id}', {
            params: { path: { schedule_id: schedule.id } },
            body: { enabled },
          }),
        ),
      );
      void refreshRow(schedule.id);
    } catch (error) {
      const problem = asProblem(error);
      toasts.push('error', problemMessage(problem), problem.detail ?? undefined);
      replace({ ...schedule });
    } finally {
      busy = null;
    }
  }

  async function skipNext(schedule: Schedule): Promise<void> {
    busy = schedule.id;
    try {
      replace(
        await unwrap(
          schedulerApi.POST('/api/v1/scheduler/schedules/{schedule_id}/skip-next', {
            params: { path: { schedule_id: schedule.id } },
          }),
        ),
      );
      toasts.push('ok', m.sched_skip_done());
      await refreshRow(schedule.id);
    } catch (error) {
      const problem = asProblem(error);
      toasts.push('error', problemMessage(problem), problem.detail ?? undefined);
    } finally {
      busy = null;
    }
  }

  function openRun(schedule: Schedule): void {
    runTarget = schedule;
    respectRain = schedule.rain_pause.enabled;
    runResult = null;
    runProblem = null;
  }

  async function runNow(): Promise<void> {
    if (!runTarget) return;
    const id = runTarget.id;
    runBusy = true;
    runProblem = null;
    try {
      const result = await unwrap(
        schedulerApi.POST('/api/v1/scheduler/schedules/{schedule_id}/run-now', {
          params: { path: { schedule_id: id }, query: { respect_rain: respectRain } },
        }),
      );
      runResult = result.items;
      void refreshRow(id);
    } catch (error) {
      runProblem = asProblem(error);
    } finally {
      runBusy = false;
    }
  }

  function closeRun(): void {
    runTarget = null;
    runResult = null;
  }

  function mowerName(id: string | null): string {
    return targets.mowers.find((x) => x.id === id)?.name ?? m.notif_mower();
  }
</script>

<svelte:head><title>{m.sched_title()} · {m.app_name()}</title></svelte:head>

<TopBar
  title={m.sched_title()}
  meta={schedules ? m.sched_count({ count: schedules.length }) : undefined}
>
  {#snippet actions()}
    {#if canCreate}
      <Button href="/schedules/new" variant="primary" icon="plus" testid="schedule-new"
        >{m.sched_new()}</Button
      >
    {/if}
  {/snippet}
</TopBar>
<FirstTimeHint hint="schedules" text={m.firsttime_schedules_text()} />

{#if loadProblem}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(loadProblem)}
      detail={loadProblem.detail}
      reference={loadProblem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !schedules}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else if schedules.length === 0}
  <div class="empty" data-testid="schedules-empty">
    <Icon name="calendar" size={32} />
    <h2>{m.sched_empty_title()}</h2>
    <p>{canCreate ? m.sched_empty_body() : m.sched_empty_viewer()}</p>
    {#if canCreate}
      <Button href="/schedules/new" variant="primary" icon="plus">{m.sched_new()}</Button>
    {/if}
  </div>
{:else}
  <ul class="list" data-testid="schedules">
    {#each schedules as schedule (schedule.id)}
      {@const view = parsePreview(previews[schedule.id])}
      {@const right = rightsOf(rights, schedule)}
      {@const last = lastRuns[schedule.id]}
      {@const mow = mowSummary(schedule)}
      <li
        class="row"
        class:off={!schedule.enabled}
        data-testid="schedule-row"
        data-id={schedule.id}
      >
        <div class="head">
          <a class="name" href="/schedules/{schedule.id}" data-testid="schedule-link"
            >{schedule.name}</a
          >
          <span class="target">
            <Icon name={schedule.target_type === 'group' ? 'list' : 'mower'} size={16} />
            {targetName(schedule)}
          </span>
        </div>

        <p class="rule mono" data-testid="schedule-rule">
          {recurrenceSummary(schedule, locale)}
        </p>
        {#if mow.length}
          <ul class="mow mono" aria-label={m.sched_section_pattern()} data-testid="schedule-mow">
            {#each mow as part, i (i)}<li>{part}</li>{/each}
          </ul>
        {/if}

        <dl class="gauges">
          <div class="gauge">
            <dt>{m.sched_next_run()}</dt>
            <dd data-testid="schedule-next">
              {#if !schedule.enabled}
                <span class="muted">{m.sched_disabled()}</span>
              {:else if view.next}
                <span class="mono when"
                  >{formatFiring(view.next.at, schedule.timezone, locale)}</span
                >
                <span class="mono countdown"
                  >{formatCountdown(view.next.at, clock.now, locale)}</span
                >
                {#if view.skipped}
                  <span class="skip" data-testid="schedule-skipped"
                    ><Icon name="skip" size={14} />{m.sched_skipped_one({
                      when: formatFiring(view.skipped.at, schedule.timezone, locale),
                    })}</span
                  >
                {/if}
              {:else}
                <span class="muted"
                  >{previews[schedule.id] === undefined ? m.loading() : m.no_data()}</span
                >
              {/if}
            </dd>
          </div>
          <div class="gauge">
            <dt>{m.sched_rain()}</dt>
            <dd>
              {#if schedule.enabled && previews[schedule.id] !== undefined}
                <ToneBadge
                  {...verdictLook(schedule.rain_pause.enabled ? view.badge : 'disabled')}
                  value={schedule.rain_pause.enabled ? view.badge : 'disabled'}
                  testid="schedule-verdict"
                />
              {:else}
                <span class="muted">–</span>
              {/if}
            </dd>
          </div>
          <div class="gauge">
            <dt>{m.sched_last_run()}</dt>
            <dd data-testid="schedule-last">
              {#if last}
                <ToneBadge
                  {...outcomeLook(last.outcome)}
                  value={last.outcome}
                  testid="schedule-last-outcome"
                />
                <span class="mono age">{formatAge(last.started_at, clock.now)}</span>
              {:else}
                <span class="muted">{last === null ? m.never() : m.loading()}</span>
              {/if}
            </dd>
          </div>
        </dl>

        {#if schedule.disabled_reason || schedule.permission_skips > 0}
          <p class="warn">
            <Icon name="alert" size={16} />
            {schedule.disabled_reason
              ? m.sched_disabled_reason({ reason: schedule.disabled_reason })
              : m.sched_permission_skips({ count: schedule.permission_skips })}
          </p>
        {/if}

        <div class="actions">
          {#if right.edit}
            <div class="toggle">
              <Switch
                id="enabled-{schedule.id}"
                testid="schedule-enabled"
                label={m.sched_enabled()}
                states={switchStates}
                checked={schedule.enabled}
                disabled={busy === schedule.id}
                onchange={(on) => setEnabled(schedule, on)}
              />
            </div>
          {:else}
            <span class="state mono" data-testid="schedule-state"
              >{schedule.enabled ? m.sched_state_on() : m.sched_state_off()}</span
            >
          {/if}
          <div class="buttons">
            {#if right.edit}
              <Button
                variant="ghost"
                icon="skip"
                pending={busy === schedule.id}
                disabled={!schedule.enabled || !view.next}
                testid="schedule-skip"
                onclick={() => skipNext(schedule)}>{m.sched_skip_next()}</Button
              >
            {/if}
            {#if right.control}
              <Button
                variant="secondary"
                icon="play"
                testid="schedule-run"
                onclick={() => openRun(schedule)}>{m.sched_run_now()}</Button
              >
            {/if}
            <Button
              variant="ghost"
              href="/schedules/{schedule.id}/runs"
              icon="list"
              testid="schedule-runs">{m.sched_runs()}</Button
            >
            {#if right.edit}
              <Button
                variant="ghost"
                href="/schedules/{schedule.id}"
                icon="settings"
                label={m.sched_edit()}
                testid="schedule-edit"
              />
            {/if}
          </div>
        </div>
      </li>
    {/each}
  </ul>
{/if}

<Sheet
  open={runTarget !== null}
  title={m.sched_run_title()}
  closeLabel={m.close()}
  onclose={closeRun}
  testid="run-sheet"
>
  {#if runTarget}
    <p class="sheet-lead">
      {m.sched_run_body({ name: runTarget.name, target: targetName(runTarget) })}
    </p>
    {#if runResult}
      <ul class="results" data-testid="run-result">
        {#each runResult as run (run.id)}
          <li>
            <span>{mowerName(run.mower_id)}</span>
            <ToneBadge {...outcomeLook(run.outcome)} value={run.outcome} />
          </li>
        {:else}
          <li class="muted">{m.sched_run_none()}</li>
        {/each}
      </ul>
    {:else}
      <Switch
        id="run-respect-rain"
        testid="run-respect-rain"
        label={m.sched_run_respect_rain()}
        hint={m.sched_run_respect_rain_hint()}
        states={switchStates}
        bind:checked={respectRain}
      />
    {/if}
    {#if runProblem}
      <ProblemNotice
        title={problemMessage(runProblem)}
        detail={runProblem.detail}
        reference={runProblem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {/if}
  {/if}
  {#snippet footer()}
    {#if runResult}
      <Button full onclick={closeRun} testid="run-close">{m.close()}</Button>
    {:else}
      <Button
        variant="primary"
        full
        icon="play"
        pending={runBusy}
        testid="run-confirm"
        onclick={runNow}>{m.sched_run_confirm()}</Button
      >
    {/if}
  {/snippet}
</Sheet>

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet,
  .muted {
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
    margin: 0 0 var(--md-space-2);
  }

  .list {
    max-width: 1200px;
    margin: 0;
    padding: 0 0 var(--md-space-8);
    list-style: none;
  }

  .row {
    position: relative;
    display: grid;
    gap: var(--md-space-3);
    padding: var(--md-space-4) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .row::before {
    content: '';
    position: absolute;
    inset-block: 0;
    inset-inline-start: 0;
    width: 3px;
    background: var(--md-color-ink);
  }

  .row.off::before {
    background: var(--md-color-line-strong);
  }

  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--md-space-1) var(--md-space-3);
  }

  .name {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-semibold);
    line-height: var(--md-line-28);
    text-decoration: none;
  }

  .name:hover {
    text-decoration: underline;
  }

  .name:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .off .name {
    color: var(--md-color-ink-muted);
  }

  .target {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .rule {
    margin: 0;
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-medium);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  /* The start settings the schedule sets, as a ruled strip: only what differs from the mower. */
  .mow {
    display: flex;
    flex-wrap: wrap;
    margin: 0;
    padding: 0;
    list-style: none;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  .mow li + li::before {
    content: '·';
    padding-inline: var(--md-space-1);
  }

  .gauges {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--md-space-0_5);
    margin: 0;
  }

  .gauge {
    display: grid;
    grid-template-columns: 112px minmax(0, 1fr);
    align-items: center;
    gap: var(--md-space-3);
    min-height: 40px;
    padding: var(--md-space-1) var(--md-space-3);
    background: var(--md-color-surface-sunken);
  }

  dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  dd {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-1) var(--md-space-3);
    margin: 0;
    min-width: 0;
  }

  .when {
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-medium);
  }

  .countdown,
  .age {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .skip {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .warn {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    margin: 0;
    color: var(--md-state-error);
    font-size: var(--md-font-size-14);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .toggle {
    min-width: 200px;
  }

  .state {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .buttons {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .sheet-lead {
    margin: 0 0 var(--md-space-3);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .results {
    margin: 0 0 var(--md-space-3);
    padding: 0;
    list-style: none;
  }

  .results li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  @media (min-width: 1024px) {
    .row {
      grid-template-columns: minmax(0, 1.2fr) minmax(0, 2fr);
      column-gap: var(--md-space-6);
      align-items: start;
    }

    .head,
    .rule,
    .mow {
      grid-column: 1;
    }

    .rule {
      grid-row: 2;
    }

    .gauges {
      grid-column: 2;
      grid-row: 1 / span 3;
    }

    .warn,
    .actions {
      grid-column: 1;
    }
  }
</style>
